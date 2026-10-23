import unittest

from estate.instances import InstanceRegistry
from estate.kb import Listing


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


if __name__ == "__main__":
    unittest.main()
