"""Offline regressions for final publication, privacy and generated-media gates."""
from dataclasses import replace
from urllib.parse import quote
from unittest.mock import patch
import unittest

from estate.checker import check
from estate.connectors import Attachment
from estate.media import MediaGateway
from tests.test_application import ApplicationFixture, TG_TOKEN, instagram, telegram


class RuntimeRegressionTest(ApplicationFixture):
    def test_publication_cannot_erase_existing_owner_name_or_phone(self):
        instance = self.registry.get("a")
        original = replace(instance.kb.get("L-001"), owner_phone="+998000000000")
        instance.kb.add(original)
        for index, private in enumerate((original.owner_name, original.owner_phone)):
            with self.subTest(private_kind=index):
                self.service.receive("a", "telegram", telegram(
                    f"/publish L-001\n{private} Yunusobod, 2 xonali, 50 m2, 500$",
                    client=900, message=100 + index))
        calls = self.wires["a", "telegram"].calls
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[1]["chat_id"] == "900" for call in calls))
        self.assertTrue(all("chiqarilmadi" in call[1]["text"].lower() for call in calls))
        self.assertEqual(instance.kb.get("L-001"), original)

    def test_publication_rejects_existing_taken_listing(self):
        instance = self.registry.get("a")
        instance.kb.apply_control_message("L-001 taken")
        result, = self.service.receive("a", "telegram", telegram(
            "/publish L-001\nYunusobod, 2 xonali, 50 m2, 500$", client=900))
        self.assertIn("band", result.reply)
        self.assertFalse(instance.kb.get("L-001").available)
        self.assertEqual(len(self.wires["a", "telegram"].calls), 1)
        self.assertEqual(self.wires["a", "telegram"].calls[0][1]["chat_id"], "900")

    def test_safe_existing_publication_keeps_live_privacy_facts(self):
        original = self.registry.get("a").kb.get("L-001")
        result, = self.service.receive("a", "telegram", telegram(
            "/publish L-001\nYunusobod, 2 xonali, 50 m2, 500$", client=900))
        self.assertIn("kanalga joylandi", result.reply)
        self.assertEqual(self.wires["a", "telegram"].calls[0][1]["chat_id"], "@synthetic_a")
        self.assertEqual(self.registry.get("a").kb.get("L-001"), original)

    def test_embedded_credential_urls_are_blocked_in_all_outgoing_text(self):
        instance = self.registry.get("a")
        unsafe = (
            "https://api.telegram.org/file/bot" + TG_TOKEN + "/photos/a.jpg",
            "https://cdn.example.com/a?access_token=synthetic_secret",
            "https://cdn.example.com/a?api_key=synthetic_secret",
            "https://cdn.example.com/a?bot_token=synthetic_secret",
            "https://user:synthetic_secret@cdn.example.com/a",
        )
        for index, url in enumerate(unsafe):
            for encoded in (url, quote(quote(url, safe=""), safe="")):
                with self.subTest(index=index, encoded=encoded != url):
                    verdict = check("Source " + encoded, instance.kb, instance.realtor_contact)
                    self.assertFalse(verdict.ok)
                    self.assertIn("reply contains a credential-bearing URL", verdict.reasons)
        instance.kb.add(replace(instance.kb.get("L-001"), title="Source " + unsafe[0]))
        self.service.receive("a", "telegram", telegram())
        self.assertTrue(self.wires["a", "telegram"].calls)
        self.assertNotIn(TG_TOKEN, str([call[1] for call in self.wires["a", "telegram"].calls]))

    def test_final_nonlisting_send_gate_rejects_credential_url(self):
        with patch("estate.agent.handle", return_value=(
                "Source https://cdn.example.com/a?access_token=synthetic_secret", [])):
            with self.assertRaises(ValueError):
                self.service.receive("a", "telegram", telegram())
        self.assertEqual(self.wires["a", "telegram"].calls, [])
        self.assertEqual(self.latency.summary("a")["failed"], 1)

    def test_publication_text_cannot_expose_credential_urls(self):
        result, = self.service.receive("a", "telegram", telegram(
            "/publish L-007\nYunusobod, 2 xonali, 50 m2, 500$ "
            "https://cdn.example.com/a?access_token=synthetic_secret", client=900))
        self.assertIn("chiqarilmadi", result.reply.lower())
        calls = self.wires["a", "telegram"].calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["chat_id"], "900")
        self.assertNotIn("synthetic_secret", str(calls[0][1]))

    def test_ingested_long_listing_id_is_recommended_not_mistaken_for_phone(self):
        post = {"channel_post": {"message_id": 77, "date": 1000,
                "chat": {"id": -100123456, "type": "channel", "username": "synthetic_a"},
                "text": "Yunusobod, 2 xonali, 50 m2, 500$"}}
        self.service.receive("a", "telegram", post)
        generated = next(item for item in self.registry.get("a").kb.all() if item.id != "L-001")
        self.assertGreater(len(generated.id[2:]), 9)
        result, = self.service.receive("a", "telegram", telegram())
        self.assertEqual(result.blocked, ())
        self.assertIn("[" + generated.id + "]", result.reply)
        self.assertTrue(any(generated.id in call[1].get("text", "")
                            for call in self.wires["a", "telegram"].calls))

    def test_only_valid_listing_tokens_are_exempt_not_plain_or_owner_phones(self):
        instance = self.registry.get("a")
        listing_id = "L-385650130262496726"
        instance.kb.add(replace(instance.kb.get("L-001"), id=listing_id, owner_phone="+998000000000"))
        self.assertTrue(check("[" + listing_id + "]", instance.kb).ok)
        for text in ("385650130262496726", "L-111111111111111111", "[" + listing_id + "] +998000000000",
                     "[" + listing_id + "] +998000000001", "[" + listing_id + "] phone=1700000602"):
            with self.subTest(text=text):
                self.assertFalse(check(text, instance.kb).ok)
        instance.kb.apply_control_message(listing_id + " taken")
        self.assertFalse(check("[" + listing_id + "]", instance.kb).ok)

    def _configure_gateway(self):
        self.now = 1700000002
        instance = self.registry.get("a")
        instance.kb.add(replace(instance.kb.get("L-001"), photos=["synthetic_file_id"]))
        gateway = MediaGateway(self.registry, "https://media.example.com",
                               "synthetic_signing_key_only_123456", clock=lambda: self.now)
        self.service.media_gateway = gateway
        return instance, gateway

    def test_real_epoch_generated_capability_passes_without_scanning_expiry(self):
        _instance, _gateway = self._configure_gateway()
        result, = self.service.receive("a", "instagram", instagram(at=1700000000))
        self.assertEqual(result.status, "sent")
        calls = self.wires["a", "instagram"].calls
        self.assertEqual(len(calls), 2)
        url = calls[1][1]["message"]["attachment"]["payload"]["url"]
        self.assertIn("expires=1700000602", url)
        self.assertNotIn(TG_TOKEN, url)
        self.assertEqual(self.latency.summary("a")["count"], 1)

    def test_arbitrary_public_url_query_is_not_exempt_from_phone_privacy(self):
        instance, gateway = self._configure_gateway()
        instance.kb.add(replace(instance.kb.get("L-001"), photos=[
            "https://cdn.example.com/a.jpg?expires=1700000602&signature=" + "a" * 64]))
        with self.assertRaises(ValueError):
            self.service.receive("a", "instagram", instagram(at=1700000000))
        self.assertEqual(self.wires["a", "instagram"].calls, [])

    def test_generated_capability_still_checks_original_stored_reference(self):
        instance, _gateway = self._configure_gateway()
        instance.kb.add(replace(instance.kb.get("L-001"), photos=["1700000602"]))
        with self.assertRaises(ValueError):
            self.service.receive("a", "instagram", instagram(at=1700000000))
        self.assertEqual(self.wires["a", "instagram"].calls, [])

    def test_gateway_exemption_requires_expected_tenant_path_and_real_signature(self):
        _instance, gateway = self._configure_gateway()
        for url in (
            "https://media.example.com/media/b/L-001/photo/0?expires=1700000602&signature=" + "a" * 64,
            "https://media.example.com/media/a/L-001/photo/0?expires=1700000602&signature=" + "a" * 64,
            "https://cdn.example.com/media/a/L-001/photo/0?expires=1700000602&signature=" + "a" * 64,
        ):
            with self.subTest(url=url), patch.object(gateway, "attachment", return_value=Attachment("photo", url)):
                with self.assertRaises(ValueError):
                    self.service.receive("a", "instagram", instagram(at=1700000000))
        self.assertEqual(self.wires["a", "instagram"].calls, [])


if __name__ == "__main__":
    unittest.main()
