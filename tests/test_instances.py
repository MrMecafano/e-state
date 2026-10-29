import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from estate.instances import Instance, InstanceRegistry
from estate.kb import Listing
from estate.storage import TenantStore


class InstanceRegistryTest(unittest.TestCase):
    def test_f14_creates_instance_with_connected_account(self):
        reg = InstanceRegistry()
        inst = reg.create("demo_realtor", "Aziz", telegram_channel="@demo_realtor_channel")
        self.assertEqual(inst.realtor_name, "Aziz")
        self.assertIs(reg.get("demo_realtor"), inst)

    def test_f14_needs_at_least_one_connected_account(self):
        reg = InstanceRegistry()
        with self.assertRaises(ValueError):
            reg.create("demo_realtor", "Aziz")

    def test_f14_rejects_duplicate_id(self):
        reg = InstanceRegistry()
        reg.create("demo_realtor", "Aziz", telegram_channel="@demo_realtor_channel")
        with self.assertRaises(ValueError):
            reg.create("demo_realtor", "Someone else", telegram_channel="@other_channel")

    def test_n03_rejects_a_scraped_url_instead_of_an_official_api_handle(self):
        reg = InstanceRegistry()
        with self.assertRaises(ValueError):
            reg.create("demo_realtor", "Aziz", telegram_channel="https://t.me/demo_realtor_channel")
        with self.assertRaises(ValueError):
            reg.create("demo_realtor", "Aziz", instagram_account="https://instagram.com/demo_realtor")

    def test_n03_accepts_bot_api_channel_and_numeric_chat_id(self):
        reg = InstanceRegistry()
        a = reg.create("a", "Aziz", telegram_channel="@demo_realtor_channel")
        b = reg.create("b", "Bekzod", telegram_channel="-1001234567890")
        self.assertTrue(a.telegram_channel.startswith("@"))
        self.assertTrue(b.telegram_channel.lstrip("-").isdigit())

    def test_n02_each_instance_gets_its_own_isolated_kb(self):
        reg = InstanceRegistry()
        a = reg.create("a", "Aziz", telegram_channel="@a_channel")
        b = reg.create("b", "Bekzod", telegram_channel="@b_channel")
        a.kb.add(Listing("L-001", "Flat", "Chilonzor", 2, 60, 500))
        self.assertIsNot(a.kb, b.kb)
        self.assertEqual([l.id for l in a.kb.all()], ["L-001"])
        self.assertEqual(b.kb.all(), [])

    def test_instance_ids_and_names_must_be_nonempty_single_line_text(self):
        for instance_id in ("", " ", "a\n", "a\r", "a\x00", " a", None, 123):
            with self.subTest(instance_id=instance_id), self.assertRaises(ValueError):
                InstanceRegistry().create(instance_id, "Aziz", telegram_channel="@estate_channel")
        for name in ("", " ", "Aziz\n", "Aziz\r", "Aziz\x00", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                InstanceRegistry().create("a", name, telegram_channel="@estate_channel")

    def test_instagram_accepts_only_ascii_numeric_official_account_ids(self):
        for account in ("aziz", "aziz.realtor", "@aziz", "12345abc", "123456\n", "１２３４５６"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                InstanceRegistry().create("a", "Aziz", instagram_account=account)
        instance = InstanceRegistry().create("a", "Aziz", instagram_account="17841400000001")
        self.assertEqual(instance.instagram_account, "17841400000001")

    def test_telegram_requires_full_ascii_username_or_numeric_id(self):
        for channel in ("@каналканал", "@estate_channel\n", "123456\n", "-100123456\r", "１２３４５６", "@estate-channel", "@12345", "@_estate", "@tiny"):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                InstanceRegistry().create("a", "Aziz", telegram_channel=channel)
        for channel in ("@estate_channel", "@Estate123", "123456", "-1001234567890"):
            with self.subTest(channel=channel):
                instance = InstanceRegistry().create("a", "Aziz", telegram_channel=channel)
                self.assertEqual(instance.telegram_channel, channel)

    def test_new_fields_default_to_blank_and_no_store_remains_supported(self):
        registry = InstanceRegistry()
        instance = registry.create("a", "Aziz", telegram_channel="@estate_channel")
        self.assertIsNone(registry.store)
        for key in ("realtor_contact", "telegram_token_env", "instagram_token_env", "webhook_secret_env", "instagram_app_secret_env"):
            self.assertEqual(getattr(instance, key), "")
        self.assertEqual(instance.telegram_admin_ids, ())
        self.assertEqual(registry.all(), [instance])

    def test_direct_instance_creation_also_validates_accounts_and_secret_references(self):
        with self.assertRaises(ValueError):
            Instance("a", "Aziz", instagram_account="username")
        with self.assertRaises(ValueError) as exc:
            Instance("a", "Aziz", telegram_channel="@estate_channel", telegram_token_env="12345:secret-token")
        self.assertNotIn("12345:secret-token", str(exc.exception))
        for admins in (("@username",), ("-12345",), ("123\n",), (123,), "12345"):
            with self.subTest(admins=admins), self.assertRaises(ValueError):
                Instance("a", "Aziz", telegram_channel="@estate_channel", telegram_admin_ids=admins)


class PersistentInstanceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tenants.sqlite3"
        self.store = TenantStore(self.path)
        self.addCleanup(self.store.close)
        self.registry = InstanceRegistry(self.store)

    def restart(self):
        self.store.close()
        self.store = TenantStore(self.path)
        self.addCleanup(self.store.close)
        self.registry = InstanceRegistry(self.store)

    def create(self, instance_id="a", **config):
        return self.registry.create(instance_id, "Aziz", telegram_channel="@estate_channel", **config)

    def test_registry_restores_metadata_and_ingested_listings_after_restart(self):
        instance = self.create(
            realtor_contact="@aziz", listing_source="posts.json",
            telegram_token_env="ESTATE_TELEGRAM_TOKEN",
            instagram_token_env="ESTATE_INSTAGRAM_TOKEN",
            webhook_secret_env="ESTATE_WEBHOOK_SECRET",
            instagram_app_secret_env="ESTATE_INSTAGRAM_APP_SECRET",
            telegram_admin_ids=("12345", "67890"),
        )
        listing = Listing("L-001", "Flat", "Chilonzor", 2, 60, 500, owner_phone="private")
        instance.kb.add(listing)
        self.assertIs(self.registry.store, self.store)
        self.restart()
        restored = self.registry.get("a")
        self.assertEqual(restored.realtor_name, "Aziz")
        self.assertEqual(restored.listing_source, "posts.json")
        self.assertEqual(restored.realtor_contact, "@aziz")
        self.assertEqual(restored.telegram_token_env, "ESTATE_TELEGRAM_TOKEN")
        self.assertEqual(restored.instagram_token_env, "ESTATE_INSTAGRAM_TOKEN")
        self.assertEqual(restored.webhook_secret_env, "ESTATE_WEBHOOK_SECRET")
        self.assertEqual(restored.instagram_app_secret_env, "ESTATE_INSTAGRAM_APP_SECRET")
        self.assertEqual(restored.telegram_admin_ids, ("12345", "67890"))
        self.assertEqual(restored.kb.all(), [listing])
        self.assertEqual(self.registry.all(), [restored])
        self.assertIs(self.registry.get("a"), restored)

    def test_all_restores_instances_without_prior_get_and_unknown_get_does_not_create(self):
        self.create("a").kb.add(Listing("L-001", "A", "A", 2, 60, 500))
        self.create("b").kb.add(Listing("L-001", "B", "B", 2, 60, 500))
        self.restart()
        self.assertIsNone(self.registry.get("unknown"))
        instances = self.registry.all()
        self.assertEqual([instance.id for instance in instances], ["a", "b"])
        self.assertEqual([instance.kb.get("L-001").title for instance in instances], ["A", "B"])
        self.assertIsNot(instances[0].kb, instances[1].kb)

    def test_duplicate_rejected_after_restart_without_having_loaded_registry(self):
        self.create()
        self.restart()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(len(self.registry.all()), 1)

    def test_control_status_and_conversations_are_separate_after_restart(self):
        a, b = self.create("a"), self.create("b")
        a.kb.add(Listing("L-001", "A", "A", 2, 60, 500))
        b.kb.add(Listing("L-001", "B", "B", 2, 60, 500))
        self.store.scope("a").record_message("telegram", "client", "user", "Only A", "1", 1)
        self.store.scope("b").record_message("telegram", "client", "user", "Only B", "1", 1)
        self.assertEqual(a.kb.apply_control_message("L-001 taken"), "L-001 band deb belgilandi.")
        self.restart()
        a, b = self.registry.get("a"), self.registry.get("b")
        self.assertEqual(a.kb.available(), [])
        self.assertEqual(b.kb.get("L-001").status, "available")
        self.assertEqual(self.store.scope("a").history("telegram", "client")[0]["text"], "Only A")
        self.assertEqual(self.store.scope("b").history("telegram", "client")[0]["text"], "Only B")
        self.assertEqual(a.kb.apply_control_message("bo'sh L-001"), "L-001 bo'sh deb belgilandi.")
        self.restart()
        self.assertTrue(self.registry.get("a").kb.get("L-001").available)

    def test_invalid_or_missing_control_command_never_changes_store(self):
        instance = self.create()
        listing = Listing("L-001", "Flat", "A", 2, 60, 500)
        instance.kb.add(listing)
        self.assertIn("Yozing:", instance.kb.apply_control_message("hello"))
        self.assertIn("topilmadi", instance.kb.apply_control_message("L-999 taken"))
        self.assertEqual(self.store.scope("a").listings(), [listing])

    def test_get_and_all_refresh_listings_written_through_another_connection(self):
        instance = self.create()
        other_store = TenantStore(self.path)
        self.addCleanup(other_store.close)
        other_registry = InstanceRegistry(other_store)
        other = other_registry.get("a")
        listing = Listing("L-001", "New listing", "A", 2, 60, 500)
        other.kb.add(listing)
        self.assertIs(self.registry.get("a"), instance)
        self.assertEqual(instance.kb.all(), [listing])
        other.kb.apply_control_message("L-001 taken")
        self.assertEqual(self.registry.all()[0].kb.get("L-001").status, "taken")

    def _overlap_writes(self, instance, operation, first_action, second_action):
        entered, release = threading.Barrier(2), threading.Barrier(2)
        attempted = threading.Event()
        errors = []
        original = getattr(instance.kb._scope, operation)

        def pause_after_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            if threading.current_thread().name == "first":
                entered.wait(timeout=5)
                release.wait(timeout=5)
            return result

        def run(action, second=False):
            try:
                if second:
                    attempted.set()
                action()
            except Exception as exc:
                errors.append(exc)

        with patch.object(instance.kb._scope, operation, side_effect=pause_after_commit):
            first = threading.Thread(target=run, args=(first_action,), name="first")
            second = threading.Thread(target=run, args=(second_action, True), name="second")
            first.start()
            entered.wait(timeout=5)
            second.start()
            self.assertTrue(attempted.wait(timeout=5))
            # While the first commit is paused, its entire cache update/control
            # snapshot must remain serialized against every other KB mutation.
            acquired = instance.kb._lock.acquire(blocking=False)
            if acquired:
                instance.kb._lock.release()
            release.wait(timeout=5)
            first.join(timeout=5)
            second.join(timeout=5)
            self.assertFalse(acquired)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])

    def test_concurrent_controls_do_not_undo_an_unrelated_listing_status(self):
        instance = self.create()
        instance.kb.add(Listing("L-001", "One", "A", 2, 60, 500))
        instance.kb.add(Listing("L-002", "Two", "A", 2, 60, 500))
        self._overlap_writes(
            instance, "set_listing_status",
            lambda: instance.kb.apply_control_message("L-001 taken"),
            lambda: instance.kb.apply_control_message("L-002 taken"),
        )
        self.assertEqual([listing.status for listing in instance.kb.all()], ["taken", "taken"])
        self.assertEqual([listing.status for listing in self.store.scope("a").listings()], ["taken", "taken"])

    def test_reader_waits_for_status_commit_and_cache_publication(self):
        instance = self.create()
        instance.kb.add(Listing("L-001", "One", "A", 2, 60, 500))
        committed, publish = threading.Barrier(2), threading.Barrier(2)
        reader_waiting, read_done = threading.Event(), threading.Event()
        original_lock = instance.kb._lock
        original_update = instance.kb._scope.set_listing_status
        observed, errors = [], []

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "reader":
                    reader_waiting.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        def pause_after_commit(*args):
            original_update(*args)
            committed.wait(timeout=5)
            publish.wait(timeout=5)

        def write():
            try:
                instance.kb.apply_control_message("L-001 taken")
            except Exception as exc:
                errors.append(exc)

        def read():
            try:
                observed.append(instance.kb.get("L-001").status)
                read_done.set()
            except Exception as exc:
                errors.append(exc)

        with patch.object(instance.kb, "_lock", ObservedLock()), \
             patch.object(instance.kb._scope, "set_listing_status", side_effect=pause_after_commit):
            writer = threading.Thread(target=write, name="writer")
            reader = threading.Thread(target=read, name="reader")
            writer.start()
            committed.wait(timeout=5)
            reader.start()
            waiting = reader_waiting.wait(timeout=5)
            premature_read = read_done.is_set()
            publish.wait(timeout=5)
            writer.join(timeout=5)
            reader.join(timeout=5)
            self.assertTrue(waiting)
            self.assertFalse(premature_read)
            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, ["taken"])

    def test_held_kb_readers_refresh_external_store_changes(self):
        instance = self.create()
        self.store.scope("a").upsert_listing(Listing("L-001", "One", "A", 2, 60, 500))
        self.assertEqual(instance.kb.get("L-001").status, "available")
        self.store.scope("a").set_listing_status("L-001", "taken")
        self.assertEqual(instance.kb.all()[0].status, "taken")
        self.assertEqual(instance.kb.available(), [])
        self.store.scope("a").set_listing_status("L-001", "available")
        self.assertEqual(len(instance.kb.available()), 1)

    def test_delayed_ingestion_cache_write_cannot_overwrite_a_control_update(self):
        instance = self.create()
        listing = Listing("L-001", "One", "A", 2, 60, 500)
        instance.kb.add(listing)
        self._overlap_writes(
            instance, "upsert_listing", lambda: instance.kb.add(listing),
            lambda: instance.kb.apply_control_message("L-001 taken"),
        )
        self.assertEqual(instance.kb.get("L-001").status, "taken")
        self.assertEqual(self.store.scope("a").listings()[0].status, "taken")

    def test_control_changes_only_status_and_preserves_stored_video_media(self):
        instance = self.create()
        instance.kb.add(Listing("L-001", "Flat", "A", 2, 60, 500))
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE listings SET videos = ? WHERE instance_id = ? AND listing_id = ?",
                ('["https://example.com/private-tour.mp4"]', "a", "L-001"),
            )
        instance.kb.apply_control_message("L-001 taken")
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT status, videos FROM listings WHERE instance_id = ? AND listing_id = ?", ("a", "L-001"),
            ).fetchone()
        self.assertEqual(row, ("taken", '["https://example.com/private-tour.mp4"]'))

    def test_reingestion_does_not_reactivate_taken_listing_but_control_can(self):
        instance = self.create()
        instance.kb.add(Listing("L-001", "Old title", "A", 2, 60, 500))
        instance.kb.apply_control_message("L-001 taken")
        instance.kb.add(Listing("L-001", "Updated title", "A", 2, 60, 600))
        self.assertEqual(instance.kb.get("L-001").status, "taken")
        self.assertEqual(instance.kb.get("L-001").title, "Updated title")
        instance.kb.apply_control_message("L-001 available")
        self.assertEqual(instance.kb.get("L-001").status, "available")

    def test_failed_commits_leave_in_memory_listing_and_status_unchanged(self):
        instance = self.create()
        listing = Listing("L-001", "Flat", "A", 2, 60, 500)
        instance.kb.add(listing)
        with patch("estate.storage.TenantData.upsert_listing", side_effect=sqlite3.OperationalError("write failed")), \
             patch("estate.storage.TenantData.set_listing_status", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                instance.kb.add(Listing("L-002", "Other", "A", 2, 60, 500))
            with self.assertRaises(sqlite3.OperationalError):
                instance.kb.apply_control_message("L-001 taken")
        self.assertEqual(instance.kb.all(), [listing])
        self.assertEqual(self.store.scope("a").listings(), [listing])


if __name__ == "__main__":
    unittest.main()
