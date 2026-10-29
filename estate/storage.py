"""SQLite-backed tenant metadata, listings, conversations, and event receipts.

Only the registry's metadata enumeration is global. All listing, conversation,
and receipt queries bind an explicit tenant key; channels also namespace client
and event IDs. Database paths are supplied by the application, never by tenants.
Credential fields contain environment-variable *names*, never resolved secrets.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path

from .kb import Listing

_TELEGRAM_CHANNEL = re.compile(r"(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-?[0-9]{5,})")
_INSTAGRAM_ACCOUNT = re.compile(r"[0-9]{5,}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONFIG_DEFAULTS = {
    "realtor_name": "",
    "telegram_channel": "",
    "instagram_account": "",
    "listing_source": "",
    "realtor_contact": "",
    "telegram_token_env": "",
    "instagram_token_env": "",
    "webhook_secret_env": "",
    "instagram_app_secret_env": "",
    "telegram_admin_ids": (),
}


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise ValueError(f"{name} must be nonempty, single-line text without outer whitespace")
    return value


def _validated_config(instance_id: str, config: dict) -> dict:
    """Whitelist metadata and validate references without inspecting the environment."""
    _identifier(instance_id, "instance_id")
    if not isinstance(config, dict):
        raise ValueError("instance config must be a dictionary")
    if config.keys() - _CONFIG_DEFAULTS.keys() - {"id"}:
        # Do not echo rejected values: they may contain an accidentally supplied token.
        raise ValueError("unsupported instance config fields; use credential environment-variable names")
    if "id" in config and config["id"] != instance_id:
        raise ValueError("config id does not match instance_id")
    result = {**_CONFIG_DEFAULTS, **{key: value for key, value in config.items() if key != "id"}}
    admins = result["telegram_admin_ids"]
    if not isinstance(admins, (tuple, list)) or any(
        not isinstance(admin, str) or not re.fullmatch(r"[1-9][0-9]*", admin) for admin in admins
    ):
        raise ValueError("telegram_admin_ids must contain positive numeric Telegram user IDs")
    result["telegram_admin_ids"] = tuple(admins)
    for key, value in result.items():
        if key != "telegram_admin_ids" and (not isinstance(value, str) or (value and not value.isprintable())):
            raise ValueError(f"{key} must be single-line text")
    _identifier(result["realtor_name"], "realtor_name")
    telegram = result["telegram_channel"]
    instagram = result["instagram_account"]
    if not telegram and not instagram:
        raise ValueError("connect a Telegram channel and/or an Instagram account (F-14)")
    if telegram and not _TELEGRAM_CHANNEL.fullmatch(telegram):
        raise ValueError("telegram_channel must be a Telegram Bot API chat (@username or numeric id)")
    if instagram and not _INSTAGRAM_ACCOUNT.fullmatch(instagram):
        raise ValueError("instagram_account must be a numeric Instagram Messaging API account id")
    for key, value in result.items():
        if key.endswith("_env") and value and not _ENV_NAME.fullmatch(value):
            raise ValueError(f"{key} must be an environment-variable name, not a secret value")
    return result


_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(instance_id)) > 0),
    realtor_name TEXT NOT NULL CHECK(length(trim(realtor_name)) > 0),
    telegram_channel TEXT NOT NULL DEFAULT '',
    instagram_account TEXT NOT NULL DEFAULT '',
    listing_source TEXT NOT NULL DEFAULT '',
    realtor_contact TEXT NOT NULL DEFAULT '',
    telegram_token_env TEXT NOT NULL DEFAULT '',
    instagram_token_env TEXT NOT NULL DEFAULT '',
    webhook_secret_env TEXT NOT NULL DEFAULT '',
    instagram_app_secret_env TEXT NOT NULL DEFAULT '',
    telegram_admin_ids TEXT NOT NULL DEFAULT '[]',
    CHECK(telegram_channel <> '' OR instagram_account <> '')
);
CREATE TABLE IF NOT EXISTS listings (
    instance_id TEXT NOT NULL REFERENCES instances(instance_id),
    listing_id TEXT NOT NULL CHECK(length(trim(listing_id)) > 0),
    title TEXT NOT NULL,
    district TEXT NOT NULL,
    rooms INTEGER NOT NULL CHECK(rooms >= 0),
    area_m2 REAL NOT NULL CHECK(area_m2 >= 0),
    rent_usd INTEGER NOT NULL CHECK(rent_usd >= 0),
    amenities TEXT NOT NULL,
    photos TEXT NOT NULL,
    post_url TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    owner_phone TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available', 'taken')),
    videos TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(instance_id, listing_id)
);
CREATE TABLE IF NOT EXISTS messages (
    sequence INTEGER PRIMARY KEY,
    instance_id TEXT NOT NULL REFERENCES instances(instance_id),
    channel TEXT NOT NULL CHECK(length(trim(channel)) > 0),
    client_id TEXT NOT NULL CHECK(length(trim(client_id)) > 0),
    role TEXT NOT NULL CHECK(length(trim(role)) > 0),
    text TEXT NOT NULL,
    timestamp REAL NOT NULL CHECK(typeof(timestamp) IN ('integer', 'real')),
    message_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_scope ON messages(instance_id, channel, client_id, timestamp, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS messages_receipt
    ON messages(instance_id, channel, client_id, role, message_id) WHERE message_id <> '';
CREATE TABLE IF NOT EXISTS processed_events (
    instance_id TEXT NOT NULL REFERENCES instances(instance_id),
    channel TEXT NOT NULL CHECK(length(trim(channel)) > 0),
    event_id TEXT NOT NULL CHECK(length(trim(event_id)) > 0),
    PRIMARY KEY(instance_id, channel, event_id)
);
"""


class TenantStore:
    """A single application-owned SQLite database, with durable per-tenant scopes.

    Call ``close`` when the application stops. Each write commits before returning.
    A connection lock also permits a store to be shared by webhook worker threads.
    """

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            # Avoid a process-wide umask change, including in threaded servers.
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(_SCHEMA)
            # Upgrade stores created before Listing gained video attachments.
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(listings)")}
            if "videos" not in columns:
                with self._connection:
                    self._connection.execute("ALTER TABLE listings ADD COLUMN videos TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            self._connection.close()
            raise

    def create_instance(self, instance_id: str, config: dict) -> None:
        config = _validated_config(instance_id, config)
        config["telegram_admin_ids"] = json.dumps(config["telegram_admin_ids"])
        columns = ", ".join(_CONFIG_DEFAULTS)
        placeholders = ", ".join("?" for _ in _CONFIG_DEFAULTS)
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    f"INSERT INTO instances (instance_id, {columns}) VALUES (?, {placeholders})",
                    (instance_id, *(config[key] for key in _CONFIG_DEFAULTS)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"instance {instance_id!r} already exists or has invalid metadata") from exc

    def instance(self, instance_id: str) -> dict | None:
        _identifier(instance_id, "instance_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return self._metadata(row) if row is not None else None

    def instances(self) -> list[dict]:
        # Administrative discovery is intentionally global; tenant data is not.
        with self._lock:
            rows = self._connection.execute("SELECT * FROM instances ORDER BY rowid").fetchall()
        return [self._metadata(row) for row in rows]

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict:
        result = {"id": row["instance_id"], **{key: row[key] for key in _CONFIG_DEFAULTS}}
        result["telegram_admin_ids"] = tuple(json.loads(result["telegram_admin_ids"]))
        return result

    def scope(self, instance_id: str) -> TenantData:
        if self.instance(instance_id) is None:
            raise KeyError(instance_id)
        return TenantData(self, instance_id)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class TenantData:
    """Data access bound to one existing tenant; obtain through ``store.scope``."""

    def __init__(self, store: TenantStore, instance_id: str) -> None:
        self._store = store
        self._instance_id = instance_id

    def upsert_listing(self, listing: Listing) -> None:
        if not isinstance(listing, Listing):
            raise TypeError("listing must be a Listing")
        _identifier(listing.id, "listing id")
        values = asdict(listing)
        values["amenities"] = json.dumps(values["amenities"], ensure_ascii=False)
        values["photos"] = json.dumps(values["photos"], ensure_ascii=False)
        values["videos"] = json.dumps(getattr(listing, "videos", []), ensure_ascii=False)
        with self._store._lock, self._store._connection:
            self._store._connection.execute(
                """INSERT INTO listings
                   (instance_id, listing_id, title, district, rooms, area_m2, rent_usd,
                    amenities, photos, post_url, owner_name, owner_phone, status, videos)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(instance_id, listing_id) DO UPDATE SET
                       title = excluded.title, district = excluded.district,
                       rooms = excluded.rooms, area_m2 = excluded.area_m2, rent_usd = excluded.rent_usd,
                       amenities = excluded.amenities, photos = excluded.photos,
                       post_url = excluded.post_url, owner_name = excluded.owner_name,
                       owner_phone = excluded.owner_phone, status = excluded.status,
                       videos = excluded.videos""",
                (self._instance_id, *(values[key] for key in (
                    "id", "title", "district", "rooms", "area_m2", "rent_usd", "amenities", "photos",
                    "post_url", "owner_name", "owner_phone", "status", "videos",
                ))),
            )

    def listings(self) -> list[Listing]:
        with self._store._lock:
            rows = self._store._connection.execute(
                "SELECT * FROM listings WHERE instance_id = ? ORDER BY rowid", (self._instance_id,)
            ).fetchall()
        listings = []
        listing_fields = {item.name for item in fields(Listing)}
        for row in rows:
            values = dict(row)
            values.pop("instance_id")
            values["id"] = values.pop("listing_id")
            values["amenities"] = json.loads(values["amenities"])
            values["photos"] = json.loads(values["photos"])
            values["videos"] = json.loads(values["videos"])
            # Keep stores usable with the pre-video Listing dataclass as well.
            listings.append(Listing(**{key: value for key, value in values.items() if key in listing_fields}))
        return listings

    def history(self, channel: str, client_id: str) -> list[dict]:
        _identifier(channel, "channel")
        _identifier(client_id, "client_id")
        with self._store._lock:
            rows = self._store._connection.execute(
                """SELECT role, text, timestamp, message_id FROM messages
                   WHERE instance_id = ? AND channel = ? AND client_id = ?
                   ORDER BY timestamp, sequence""",
                (self._instance_id, channel, client_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_message(
        self,
        channel: str,
        client_id: str,
        role: str,
        text: str,
        message_id: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Append a turn; a nonblank ID is first-write-wins within client/role scope."""
        _identifier(channel, "channel")
        _identifier(client_id, "client_id")
        _identifier(role, "role")
        if not isinstance(text, str) or not isinstance(message_id, str):
            raise ValueError("message text and message_id must be strings")
        if message_id:
            _identifier(message_id, "message_id")
        if timestamp is None:
            timestamp = time.time()
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError("timestamp must be a finite number")
        with self._store._lock, self._store._connection:
            self._store._connection.execute(
                """INSERT INTO messages (instance_id, channel, client_id, role, text, timestamp, message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (self._instance_id, channel, client_id, role, text, timestamp, message_id),
            )

    def event_processed(self, channel: str, event_id: str) -> bool:
        _identifier(channel, "channel")
        _identifier(event_id, "event_id")
        with self._store._lock:
            row = self._store._connection.execute(
                """SELECT 1 FROM processed_events
                   WHERE instance_id = ? AND channel = ? AND event_id = ?""",
                (self._instance_id, channel, event_id),
            ).fetchone()
        return row is not None

    def mark_event_processed(self, channel: str, event_id: str) -> None:
        _identifier(channel, "channel")
        _identifier(event_id, "event_id")
        with self._store._lock, self._store._connection:
            self._store._connection.execute(
                """INSERT INTO processed_events (instance_id, channel, event_id) VALUES (?, ?, ?)
                   ON CONFLICT(instance_id, channel, event_id) DO NOTHING""",
                (self._instance_id, channel, event_id),
            )
