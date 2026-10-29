import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import field, fields, make_dataclass, replace
from pathlib import Path
from unittest.mock import patch

from estate.kb import Listing
from estate.storage import TenantStore


class TenantStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tenants.sqlite3"
        self.store = TenantStore(self.path)
        self.addCleanup(self.store.close)

    def create(self, instance_id="a", **extra):
        self.store.create_instance(instance_id, {
            "realtor_name": "Aziz",
            "telegram_channel": "@estate_channel",
            **extra,
        })
        return self.store.scope(instance_id)

    def reopen(self):
        self.store.close()
        self.store = TenantStore(self.path)
        self.addCleanup(self.store.close)

    def test_empty_store_and_unknown_tenant(self):
        self.assertEqual(self.store.instances(), [])
        self.assertIsNone(self.store.instance("unknown"))
        with self.assertRaises(KeyError):
            self.store.scope("unknown")
        self.assertEqual(self.store.instances(), [])

    def test_metadata_is_restored_and_duplicate_creation_does_not_replace_it(self):
        self.create(telegram_admin_ids=("12345", "67890"), realtor_contact="@aziz")
        with self.assertRaises(ValueError):
            self.create(realtor_name="Someone else")
        self.reopen()
        metadata = self.store.instance("a")
        self.assertEqual(metadata["id"], "a")
        self.assertEqual(metadata["realtor_name"], "Aziz")
        self.assertEqual(metadata["realtor_contact"], "@aziz")
        self.assertEqual(metadata["telegram_admin_ids"], ("12345", "67890"))
        self.assertEqual(self.store.instances(), [metadata])
        metadata["realtor_name"] = "mutated outside the store"
        self.assertEqual(self.store.instance("a")["realtor_name"], "Aziz")

    def test_duplicate_rejected_by_another_store(self):
        self.create()
        other = TenantStore(self.path)
        self.addCleanup(other.close)
        with self.assertRaises(ValueError):
            other.create_instance("a", {"realtor_name": "Other", "instagram_account": "17841400000001"})
        self.assertEqual(other.instance("a")["realtor_name"], "Aziz")

    def test_listing_roundtrip_preserves_all_fields(self):
        scope = self.create()
        listing = Listing(
            "L-001", "Квартира", "Chilonzor", 2, 60.5, 500,
            amenities=["lift", "балкон"], photos=["https://example.com/a.jpg"],
            post_url="https://t.me/estate_channel/1", owner_name="Private Owner",
            owner_phone="+998901234567", status="taken",
        )
        scope.upsert_listing(listing)
        self.reopen()
        self.assertEqual(self.store.scope("a").listings(), [listing])

    def test_photo_and_video_roundtrip_across_restart_and_tenant_collision(self):
        listing_type = Listing
        if "videos" not in {item.name for item in fields(Listing)}:
            listing_type = make_dataclass(
                "VideoListing", [("videos", list[str], field(default_factory=list))],
                bases=(Listing,), frozen=True,
            )
        listing = listing_type(
            "L-001", "Flat", "A", 2, 60, 500,
            photos=["https://example.com/photo.jpg"], videos=["https://example.com/video.mp4"],
        )
        other = replace(listing, photos=[], videos=["https://example.com/other.mp4"])
        with patch("estate.storage.Listing", listing_type):
            self.create("a").upsert_listing(listing)
            self.create("b").upsert_listing(other)
            self.reopen()
            self.assertEqual(self.store.scope("a").listings(), [listing])
            self.assertEqual(self.store.scope("b").listings(), [other])
            self.store.scope("a").upsert_listing(replace(listing, status="taken"))
            self.reopen()
            self.assertEqual(self.store.scope("a").listings(), [replace(listing, status="taken")])
            self.assertEqual(self.store.scope("b").listings(), [other])

    def test_pre_video_schema_is_migrated_without_losing_listings(self):
        listing = Listing("L-001", "Flat", "A", 2, 60, 500, photos=["https://example.com/photo.jpg"])
        self.create().upsert_listing(listing)
        self.store.close()
        connection = sqlite3.connect(self.path)
        try:
            schema = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'listings'").fetchone()[0]
            legacy_schema = schema.replace("    videos TEXT NOT NULL DEFAULT '[]',\n", "")
            self.assertNotEqual(legacy_schema, schema)
            connection.execute("ALTER TABLE listings RENAME TO listings_with_videos")
            connection.execute(legacy_schema)
            columns = (
                "instance_id, listing_id, title, district, rooms, area_m2, rent_usd, "
                "amenities, photos, post_url, owner_name, owner_phone, status"
            )
            connection.execute(f"INSERT INTO listings ({columns}) SELECT {columns} FROM listings_with_videos")
            connection.execute("DROP TABLE listings_with_videos")
            connection.commit()
        finally:
            connection.close()
        self.reopen()
        self.assertEqual(self.store.scope("a").listings(), [listing])
        self.store.scope("a").upsert_listing(replace(listing, status="taken"))
        self.reopen()
        self.assertEqual(self.store.scope("a").listings(), [replace(listing, status="taken")])
        with sqlite3.connect(self.path) as connection:
            self.assertIn("videos", {row[1] for row in connection.execute("PRAGMA table_info(listings)")})
            self.assertEqual(connection.execute("SELECT videos FROM listings").fetchone()[0], "[]")

    def test_fresh_process_restores_registry_and_commits_its_own_changes(self):
        self.create().upsert_listing(Listing("L-001", "Flat", "A", 2, 60, 500))
        self.store.close()
        code = (
            "import sys; from estate.storage import TenantStore; "
            "from estate.instances import InstanceRegistry; "
            "store = TenantStore(sys.argv[1]); registry = InstanceRegistry(store); "
            "assert registry.get('a').kb.get('L-001').status == 'available'; "
            "registry.get('a').kb.apply_control_message('L-001 taken'); "
            "store.scope('a').record_message('telegram', 'client', 'user', 'child process', 'm1', 1); "
            "store.scope('a').mark_event_processed('telegram', 'event1'); store.close()"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(self.path)],
            cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, timeout=10,
        )
        self.reopen()
        scope = self.store.scope("a")
        self.assertEqual(scope.listings()[0].status, "taken")
        self.assertEqual(scope.history("telegram", "client")[0]["text"], "child process")
        self.assertTrue(scope.event_processed("telegram", "event1"))

    def test_colliding_listing_ids_remain_separate_after_update_and_restart(self):
        a, b = self.create("a"), self.create("b")
        first = Listing("L-001", "Tenant A", "A", 2, 60, 500)
        second = Listing("L-001", "Tenant B", "B", 3, 70, 600)
        a.upsert_listing(first)
        b.upsert_listing(second)
        a.upsert_listing(replace(first, status="taken"))
        self.reopen()
        self.assertEqual(self.store.scope("a").listings(), [replace(first, status="taken")])
        self.assertEqual(self.store.scope("b").listings(), [second])

    def test_conversations_are_durably_scoped_by_tenant_channel_and_client(self):
        a, b = self.create("a"), self.create("b")
        a.record_message("telegram", "same-client", "user", "A TG", "same-message", 1.0)
        a.record_message("instagram", "same-client", "user", "A IG", "same-message", 2.0)
        b.record_message("telegram", "same-client", "user", "B TG", "same-message", 3.0)
        a.record_message("telegram", "other-client", "user", "Other client", "same-message", 4.0)
        self.reopen()
        for tenant, channel, client, text, timestamp in [
            ("a", "telegram", "same-client", "A TG", 1.0),
            ("a", "instagram", "same-client", "A IG", 2.0),
            ("b", "telegram", "same-client", "B TG", 3.0),
            ("a", "telegram", "other-client", "Other client", 4.0),
        ]:
            with self.subTest(tenant=tenant, channel=channel, client=client):
                self.assertEqual(self.store.scope(tenant).history(channel, client), [{
                    "role": "user", "text": text, "timestamp": timestamp, "message_id": "same-message",
                }])
        self.assertEqual(self.store.scope("b").history("instagram", "same-client"), [])

    def test_messages_order_by_timestamp_and_insertion_order_for_ties(self):
        scope = self.create()
        scope.record_message("telegram", "client", "assistant", "later", timestamp=3)
        scope.record_message("telegram", "client", "user", "first", timestamp=1)
        scope.record_message("telegram", "client", "assistant", "second", timestamp=1)
        self.assertEqual([row["text"] for row in scope.history("telegram", "client")], ["first", "second", "later"])

    def test_default_timestamp_and_multiline_message_text(self):
        scope = self.create()
        with patch("estate.storage.time.time", return_value=123.5):
            scope.record_message("telegram", "client", "user", "first line\nsecond line")
        self.assertEqual(scope.history("telegram", "client"), [{
            "role": "user", "text": "first line\nsecond line", "timestamp": 123.5, "message_id": "",
        }])

    def test_receipts_deduplicate_only_nonempty_message_ids_in_the_exact_scope(self):
        scope = self.create()
        scope.record_message("telegram", "client", "user", "original", "m1", 1)
        scope.record_message("telegram", "client", "user", "retry", "m1", 2)
        scope.record_message("telegram", "client", "assistant", "response", "m1", 3)
        scope.record_message("telegram", "client", "user", "legacy", timestamp=4)
        scope.record_message("telegram", "client", "user", "legacy", timestamp=5)
        self.reopen()
        self.store.scope("a").record_message("telegram", "client", "user", "retry after restart", "m1", 6)
        rows = self.store.scope("a").history("telegram", "client")
        self.assertEqual([row["text"] for row in rows], ["original", "response", "legacy", "legacy"])

    def test_processed_event_ids_are_durable_and_scoped_by_tenant_and_channel(self):
        a, b = self.create("a"), self.create("b")
        self.assertFalse(a.event_processed("telegram", "same-event"))
        a.mark_event_processed("telegram", "same-event")
        a.mark_event_processed("telegram", "same-event")
        self.assertTrue(a.event_processed("telegram", "same-event"))
        self.assertFalse(a.event_processed("instagram", "same-event"))
        self.assertFalse(b.event_processed("telegram", "same-event"))
        self.reopen()
        self.assertTrue(self.store.scope("a").event_processed("telegram", "same-event"))
        self.assertFalse(self.store.scope("a").event_processed("instagram", "same-event"))
        self.assertFalse(self.store.scope("b").event_processed("telegram", "same-event"))

    def test_sql_parameters_handle_quotes_and_never_derive_tenant_paths(self):
        tenant = "../../other'); DROP TABLE instances;--"
        scope = self.create(tenant)
        key = "x'); DROP TABLE listings;--"
        listing = Listing(key, "Tenant listing", "D", 1, 20, 200)
        scope.upsert_listing(listing)
        scope.record_message("telegram", key, "user", "'quoted'", key, 1)
        scope.mark_event_processed("telegram", key)
        self.reopen()
        scope = self.store.scope(tenant)
        self.assertEqual(scope.listings(), [listing])
        self.assertEqual(scope.history("telegram", key)[0]["text"], "'quoted'")
        self.assertTrue(scope.event_processed("telegram", key))
        self.assertEqual({path.name for path in Path(self.directory.name).iterdir()}, {"tenants.sqlite3"})

    def test_foreign_keys_and_database_checks_reject_orphans_and_bad_status(self):
        self.create()
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO processed_events VALUES (?, ?, ?)", ("missing", "telegram", "event"))
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.scope("a").upsert_listing(Listing("L-001", "Flat", "D", 1, 20, 200, status="unknown"))
        self.assertEqual(self.store.scope("a").listings(), [])
        self.store.scope("a").upsert_listing(Listing("L-001", "Flat", "D", 1, 20, 200))
        self.assertEqual(len(self.store.scope("a").listings()), 1)

    def test_only_environment_names_are_stored_never_resolved_values(self):
        secrets = {
            "ESTATE_TELEGRAM_TOKEN": "123456789:actual-secret-token",
            "ESTATE_INSTAGRAM_TOKEN": "IG-secret-token.value",
            "ESTATE_WEBHOOK_SECRET": "webhook-secret-value",
            "ESTATE_INSTAGRAM_APP_SECRET": "instagram-app-secret-value",
        }
        config = dict(zip(
            ("telegram_token_env", "instagram_token_env", "webhook_secret_env", "instagram_app_secret_env"),
            secrets,
        ))
        with patch.dict(os.environ, secrets):
            self.create(**config)
        self.reopen()
        for key, reference in config.items():
            self.assertEqual(self.store.instance("a")[key], reference)
        contents = self.path.read_bytes()
        for secret in secrets.values():
            self.assertNotIn(secret.encode(), contents)

    def test_rejects_raw_secret_keys_values_and_errors_do_not_echo_them(self):
        secret = "123456789:super-secret-token"
        for config in [
            {"telegram_token": secret},
            {"instagram_token": secret},
            {"webhook_secret": secret},
            {"instagram_app_secret": secret},
            {"telegram_token_env": secret},
            {"instagram_token_env": secret},
            {"webhook_secret_env": "has-a-dash"},
            {"instagram_app_secret_env": "ENV\n"},
        ]:
            with self.subTest(fields=list(config)):
                with self.assertRaises(ValueError) as exc:
                    self.create(**config)
                self.assertNotIn(secret, str(exc.exception))
        self.assertEqual(self.store.instances(), [])
        self.assertNotIn(secret.encode(), self.path.read_bytes())

    def test_invalid_identifiers_and_admin_ids_never_create_metadata(self):
        for instance_id in ("", " ", "x\n", "x\r", "x\x00", " x", None, 123):
            with self.subTest(instance_id=instance_id), self.assertRaises(ValueError):
                self.create(instance_id)
        for name in ("", " ", "Aziz\n", "\tAziz", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.create(realtor_name=name)
        for admins in (("@username",), ("-10012345",), ("123\n",), ("１２３",), ("0",), (123,), "123"):
            with self.subTest(admins=admins), self.assertRaises(ValueError):
                self.create(telegram_admin_ids=admins)
        self.assertEqual(self.store.instances(), [])

    def test_invalid_conversation_keys_and_timestamps_do_not_insert(self):
        scope = self.create()
        for invalid in ("", " ", "bad\n", "bad\r", "bad\x00", None):
            for method, arguments in [
                (scope.history, (invalid, "client")),
                (scope.history, ("telegram", invalid)),
                (scope.record_message, (invalid, "client", "user", "text")),
                (scope.record_message, ("telegram", invalid, "user", "text")),
                (scope.record_message, ("telegram", "client", invalid, "text")),
                (scope.event_processed, ("telegram", invalid)),
                (scope.mark_event_processed, (invalid, "event")),
            ]:
                with self.subTest(method=method.__name__, invalid=invalid), self.assertRaises(ValueError):
                    method(*arguments)
        for timestamp in (float("nan"), float("inf"), float("-inf"), "123", True):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                scope.record_message("telegram", "client", "user", "text", timestamp=timestamp)
        with self.assertRaises(ValueError):
            scope.record_message("telegram", "client", "user", "text", "message\n")
        self.assertEqual(scope.history("telegram", "client"), [])

    def test_database_file_permissions_are_restricted_even_for_existing_file(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.store.close()
        self.path.chmod(0o666)
        self.reopen()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_in_memory_store(self):
        memory = TenantStore(":memory:")
        self.addCleanup(memory.close)
        memory.create_instance("a", {"realtor_name": "Aziz", "telegram_channel": "@estate_channel"})
        self.assertEqual(memory.scope("a").listings(), [])

    def test_store_can_be_used_by_worker_threads(self):
        scope = self.create()
        errors = []

        def record(index):
            try:
                scope.record_message("telegram", "client", "user", str(index), str(index), index)
                scope.mark_event_processed("telegram", str(index))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(scope.history("telegram", "client")), 10)
        self.assertTrue(all(scope.event_processed("telegram", str(index)) for index in range(10)))


if __name__ == "__main__":
    unittest.main()
