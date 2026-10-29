"""Laptop demo: run one realtor's Telegram bot with long polling (no server, no webhook).

    python3 -m estate.polling

Settings come from the environment or from the repository's local ``.env`` file
(never committed; see ``.env.example``). Values already in the environment win.
The token is only ever passed to the connector; it is never printed.

Each run starts fresh from data/listings.json in memory, so a rehearsal and the
live demo begin from the same state and no client chat is written to disk.
This is not the deployment path: production uses the webhooks in estate.server.
"""
from __future__ import annotations

import argparse
import os
import re
import ssl
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .admin import StatsLog
from .ai import ClaudeResponder
from .connectors import ConnectorError, TelegramConnector, _FILE_ID, _https_media
from .instances import InstanceRegistry
from .kb import DEMO_DATA, KnowledgeBase
from .service import ConfigurationError, EstateService
from .storage import TenantStore
from .telemetry import LatencyLog

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
LOCK_FILE = ENV_FILE.parent / "var" / "demo-bot.lock"
INSTANCE_ID = "demo"
TOKEN_ENV = "ESTATE_DEMO_BOT_TOKEN"
REALTOR_IDS_ENV = "ESTATE_DEMO_REALTOR_IDS"
CONTACT_ENV = "ESTATE_DEMO_REALTOR_CONTACT"
CHANNEL_ENV = "ESTATE_DEMO_CHANNEL"
DEFAULT_CONTACT = "@demo_realtor"
# An instance must name a channel. Telegram never issues this ID, so with no
# channel configured every channel post is ignored.
NO_CHANNEL = "-1000000000000"


def single_copy(path: str | Path = LOCK_FILE):
    """Lock held while this process runs; returns (lock, None) or (None, the running copy's PID).

    Two copies polling one bot both answer every message (each with its own
    code), so a second copy on this computer refuses to start.
    """
    try:
        import fcntl
    except ImportError:  # Windows has no fcntl; Telegram's conflict warning still shows
        return object(), None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        other = handle.read().strip()
        handle.close()
        return None, other or "?"
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle, None


def load_env_file(path: str | Path = ENV_FILE) -> None:
    """Copy KEY=VALUE lines into os.environ without overriding existing values."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if line.startswith("#") or not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _sendable(reference: str) -> bool:
    """Telegram accepts public HTTPS media or its own file_id, not local file names."""
    return _https_media(reference) or bool(_FILE_ID.fullmatch(reference))


def build_service(connector: TelegramConnector, *, listings: str | Path = DEMO_DATA,
                  realtor_ids: tuple[str, ...] = (), contact: str = DEFAULT_CONTACT,
                  channel: str = "", responder=None) -> tuple[EstateService, list[str]]:
    """An in-memory service with one demo instance; also returns IDs that lost media."""
    store, stats, latency = TenantStore(":memory:"), StatsLog(), LatencyLog()
    registry = InstanceRegistry(store=store)
    instance = registry.create(
        INSTANCE_ID, "Demo realtor", telegram_channel=channel or NO_CHANNEL,
        realtor_contact=contact, telegram_token_env=TOKEN_ENV, telegram_admin_ids=realtor_ids)
    stripped = []
    for listing in KnowledgeBase.from_json(INSTANCE_ID, listings).all():
        photos = [p for p in listing.photos if _sendable(p)]
        videos = [v for v in listing.videos if _sendable(v)]
        if (photos, videos) != (listing.photos, listing.videos):
            stripped.append(listing.id)
        instance.kb.add(replace(listing, photos=photos, videos=videos))
    service = EstateService(registry, stats, latency, {(INSTANCE_ID, "telegram"): connector},
                            responder=responder)
    return service, stripped


def _describe(update: dict, result) -> str:
    message = update.get("message")
    sender_info = message.get("from", {}) if isinstance(message, dict) else {}
    sender = sender_info.get("id", "?")
    if sender_info.get("username"):
        sender = f"{sender} @{sender_info['username']}"  # helps tell test accounts apart
    if result.status == "control":
        return f"realtor {sender}: {'AI: ' if result.ai else ''}{result.reply.splitlines()[0] if result.reply else ''}"
    if result.status == "sent":
        cards = re.findall(r"\[(L-\d+)\]", result.reply)
        return (f"client {sender}: {'AI' if result.ai else 'rules'} replied"
                + (f" with {', '.join(cards)}" if cards else "")
                + (f" ({result.note})" if result.note else ""))
    if result.status == "ingested":
        return "channel post saved as a new listing"
    if result.status == "incomplete_listing":
        return "channel post skipped: it needs a district, rooms, area (m2) and a $ price"
    if result.status == "blocked":
        return f"client {sender}: reply blocked by the checker ({'; '.join(result.blocked)})"
    return f"update {result.message_id}: {result.status}"


def poll_once(service: EstateService, connector: TelegramConnector, offset: int | None,
              *, timeout: int = 25, log: Callable[[str], None] = print) -> int | None:
    """Fetch one batch, answer every message in it, and return the next offset.

    A message that fails is logged and skipped rather than retried forever; the
    client can simply send it again. Connector errors never contain the token.
    """
    for update in connector.get_updates(offset, timeout=timeout):
        update_id = update.get("update_id")
        if type(update_id) is not int:
            continue
        offset = update_id + 1
        try:
            results = service.receive(INSTANCE_ID, "telegram", update)
        except (ConnectorError, ConfigurationError, ValueError) as error:
            log(f"update {update_id}: not answered ({error})")
            continue
        except Exception as error:
            # Keep the demo alive; only the type is shown in case a value is private.
            log(f"update {update_id}: not answered ({type(error).__name__})")
            continue
        for result in results:
            log(_describe(update, result))
    return offset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m estate.polling",
                                     description="Run the E-state demo bot on this laptop.")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="local settings file (default: .env)")
    parser.add_argument("--listings", default=str(DEMO_DATA), help="listings JSON to start from")
    args = parser.parse_args(argv)
    lock, other = single_copy(LOCK_FILE)
    if lock is None:
        print(f"Another copy of this bot is already running (process {other}). Stop it first with "
              f"Ctrl+C in its terminal, or run: kill {other}")
        return 1
    load_env_file(args.env_file)

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        parser.error(f"set {TOKEN_ENV} in .env (the token @BotFather gave you)")
    realtor_ids = tuple(x.strip() for x in os.environ.get(REALTOR_IDS_ENV, "").split(",") if x.strip())
    contact = os.environ.get(CONTACT_ENV, "").strip() or DEFAULT_CONTACT
    channel = os.environ.get(CHANNEL_ENV, "").strip()
    try:
        responder = ClaudeResponder.from_environment()
        ai_status = f"on ({responder.model})" if responder else "off (set ANTHROPIC_API_KEY in .env to turn it on)"
    except ImportError:
        responder = None
        ai_status = "off: ANTHROPIC_API_KEY is set but the anthropic package is missing (see README)"

    connector = TelegramConnector(token, channel=channel)
    try:
        bot = connector.get_me()
    except ValueError:
        print(f"{TOKEN_ENV} does not look like a bot token (123456789:AA...). Is .env saved?")
        return 1
    except ConnectorError as error:
        # The connector hides details; only a missing certificate store needs its own advice.
        if isinstance(getattr(error.__context__, "reason", None), ssl.SSLCertVerificationError):
            print("This Python cannot check Telegram's security certificate (its certificates are not "
                  "installed). Use another Python, e.g. python3.12 -m estate.polling, or run "
                  "'Install Certificates.command' in that Python's Applications folder.")
        else:
            print(f"Telegram refused {TOKEN_ENV} or could not be reached. Check the token and the internet.")
        return 1
    try:
        if channel.startswith("@"):
            connector.resolve_channel()
    except (ConnectorError, ValueError):
        print(f"Could not use {CHANNEL_ENV}={channel}. Add the bot to that channel as an admin.")
        return 1
    try:
        service, stripped = build_service(connector, listings=args.listings,
                                          realtor_ids=realtor_ids, contact=contact, channel=channel,
                                          responder=responder)
    except ValueError as error:
        parser.error(f"check {REALTOR_IDS_ENV} and {CONTACT_ENV}: {error}")

    username = bot.get("username", "")
    print(f"E-state demo bot @{username} is running: https://t.me/{username}")
    print(f"{len(service.registry.get(INSTANCE_ID).kb.available())} free listings loaded. Handover contact: {contact}")
    print(f"AI replies: {ai_status}")
    if stripped:
        print(f"Photos for {', '.join(stripped)} are local file names, so those cards are sent as text.")
    if realtor_ids:
        print(f"Realtor commands (e.g. 'L-003 taken') accepted from user ID {', '.join(realtor_ids)}.")
    else:
        print(f"No realtor set: message the bot from the realtor's account, copy the ID shown "
              f"below into {REALTOR_IDS_ENV}, then restart.")
    if channel:
        print(f"New posts in {channel} are added as listings.")
    print("Press Ctrl+C to stop.\n")

    offset = None
    try:
        while True:
            try:
                offset = poll_once(service, connector, offset,
                                   log=lambda line: print(time.strftime("%H:%M:%S"), line, flush=True))
            except (ConnectorError, ValueError):
                print("Telegram is unreachable, or another copy of this bot is running. Retrying in 3 s.",
                      flush=True)
                time.sleep(3)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
