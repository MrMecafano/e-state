"""Regression cases for the independent send gate and localized service replies."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from estate.agent import handle
from estate.checker import check
from estate.extract import extract
from estate.ingest import is_complete, parse_post
from estate.kb import KnowledgeBase, Listing
from estate.language import detect
from estate.matcher import Criteria, recommend
from estate.replies import NO_PAYMENT, compose, format_listing, mentions_payment, wants_viewing

CONTACT = "@demo_realtor"
CRITERIA = Criteria(600, "Yunusobod", 2)


def listing(**changes):
    return replace(Listing("L-100", "Comfortable flat", "Yunusobod", 2, 62, 500,
                           ["elevator", "parking", "air conditioning"], ["photo.jpg"],
                           "https://t.me/estate_demo/100", "Rustam", "+998977778899"), **changes)


def kb(**changes):
    return KnowledgeBase("safety", [listing(**changes)])


class IndependentCheckerSafetyTest(unittest.TestCase):
    def test_owner_phone_with_uzbek_word_suffix_is_still_private(self):
        for value in ("Call +998977778899ga", "+998 (97) 777-88-99ga yozing"):
            with self.subTest(reply=value):
                verdict = check(value, kb(), "+998977778899")
                self.assertFalse(verdict.ok)
                self.assertIn("owner phone of L-100 leaked", verdict.reasons)
        self.assertFalse(check("Call +9989012345670ga", kb(), "+998901234567").ok)

    def test_form_encoded_phone_query_is_still_private(self):
        for value in ("https://example.test/?phone=%2B998+97+777+88+99",
                      "https://example.test/?owner=Ali+Valiyev"):
            with self.subTest(url=value):
                self.assertFalse(check(value, kb(owner_name="Ali Valiyev")).ok)
        # Form decoding must not erase the '+' needed to recognize short
        # international numbers in ordinary, non-form prose.
        self.assertFalse(check("Call +1234567", kb()).ok)
        self.assertTrue(check("Call +998901234567", kb(), "+998901234567").ok)

    def test_payment_instruction_does_not_require_a_money_noun(self):
        for value in ("Please pay before viewing", "Pay to view it", "Please pay", "Prepay first"):
            with self.subTest(reply=value):
                self.assertFalse(check(value, kb()).ok)
        for value in ("Please do not pay before viewing", "You do not need to pay before viewing",
                      "You don't have to pay", "No need to pay", "We never ask clients to pay",
                      "Pay attention to the room count", "Rent is $500/month"):
            with self.subTest(refusal=value):
                self.assertTrue(check(value, kb()).ok)

    def test_nonstandard_bracketed_listing_ids_are_checked(self):
        for changes in ({"status": "taken"}, {"area_m2": 0}):
            with self.subTest(changes=changes):
                k = kb(id="X-1", **changes)
                self.assertFalse(check(format_listing(k.get("X-1")), k).ok)
        self.assertFalse(check("Try [X-999]", kb()).ok)
        self.assertFalse(check("Try [flat-unknown]", kb()).ok)
        k = kb(id="X-1")
        self.assertTrue(check(format_listing(k.get("X-1")), k).ok)

    def test_owner_phone_format_and_local_number_variations_are_blocked(self):
        for owner in ("+998977778899", "+998 (97) 777-88-99", "97 777 88 99"):
            for value in ("+998 (97) 777-88-99", "998-97-777-88-99", "(97) 777-88-99",
                          "00998 97 777 88 99", "+998 97 777 88 99", "+998 (97)\n777-88-99"):
                with self.subTest(owner=owner, reply=value):
                    verdict = check("Call " + value, kb(owner_phone=owner), value)
                    self.assertFalse(verdict.ok)
                    self.assertIn("owner phone of L-100 leaked", verdict.reasons)

    def test_owner_name_is_casefolded_with_word_boundaries(self):
        for value in ("rustam", "RUSTAM", "RuStAm"):
            with self.subTest(value=value):
                self.assertFalse(check("Owner: " + value, kb()).ok)
        self.assertTrue(check("Rustamov has no relation to this listing", kb()).ok)
        self.assertTrue(check("Balcony and central location", kb(owner_name="Ali")).ok)
        self.assertFalse(check("ALI   VALIYEV", kb(owner_name="Ali Valiyev")).ok)

    def test_phone_allowlist_uses_exact_normalized_numbers_not_substrings(self):
        self.assertTrue(check("Realtor: +998 (90) 123-45-67", kb(), "+998901234567").ok)
        self.assertTrue(check("Realtor: 90 123 45 67", kb(), "+998 (90) 123-45-67").ok)
        for contact in ("+9989012345670", "prefix998901234567suffix"):
            with self.subTest(contact=contact):
                self.assertFalse(check("Call +998901234567", kb(), contact).ok)
        self.assertFalse(check("Call +7 (999) 123-45-67", kb(), CONTACT).ok)
        self.assertFalse(check("Call 90-123-45-67", kb(), CONTACT).ok)

    def test_lowercase_listing_references_are_independently_checked(self):
        self.assertFalse(check("Try [l-999]", kb()).ok)
        self.assertFalse(check("Try [l-100]", kb(status="taken")).ok)
        self.assertTrue(check("Try [l-100]", kb()).ok)

    def test_encoded_contacts_are_not_safe_in_inline_urls(self):
        for value in ("https://example.test/?tel=%2B998%20%2897%29%20777-88-99",
                      "https://example.test/RUSTAM", "https://example.test/?owner=R%75stam"):
            with self.subTest(url=value):
                self.assertFalse(check("Post: " + value, kb()).ok)

    def test_payment_requests_in_all_supported_languages_are_blocked(self):
        for value in (
            "Transfer $100 now", "Please pay 100 USD", "Send me money to reserve it",
            "Please make a deposit", "A deposit is required before viewing",
            "Please pay the rent in advance", "100$ yuboring", "Pulni kartaga o'tkazing",
            "Oldindan to‘lov qiling", "Depozitni to’lang", "Oldindan toʻlov kerak",
            "Олдиндан тўловни амалга оширинг", "100 доллар юборинг", "Пул ўтказинг",
            "Переведите 100 долларов", "Внесите залог", "Пришлите предоплату",
            "Предоплата обязательна", "Оплатите бронь",
            "We never ask for prepayment, but send $100 now",
            "Transfer 100.00 USD now", "Transfer\n$100 now", "Pay us first",
            "Oldindan to'lovni amalga oshiring", "100 dollar to'lab qo'ying",
            "Вам нужно внести депозит", "Оплати бронь", "No kitchen, deposit required",
        ):
            with self.subTest(reply=value):
                self.assertFalse(check(value, kb()).ok)

    def test_legitimate_rents_and_no_prepayment_are_allowed(self):
        for lang, template in NO_PAYMENT.items():
            with self.subTest(lang=lang):
                self.assertTrue(check(template.format(contact=CONTACT), kb(), CONTACT).ok)
        for value in ("Rent is $500/month; deposit: $0", "Monthly rent: 1,200 USD",
                      "Do not pay a deposit", "Don't send money", "Никакой предоплаты не требуется",
                      "Не переводите деньги", "Oldindan to'lov talab qilinmaydi",
                      "Oldindan to'lov kerak emas", "Олдиндан тўлов шарт эмас",
                      "No deposit is required", "A deposit isn't required",
                      "You must discuss payment with your realtor", "Pay attention to the room count"):
            with self.subTest(reply=value):
                self.assertTrue(check(value, kb()).ok, check(value, kb()).reasons)

    def test_untrusted_inline_listing_fields_cannot_escape_the_agent(self):
        for changes in ({"title": "Call RUSTAM"},
                        {"title": "Call +998 (97) 777-88-99"},
                        {"amenities": ["elevator", "+998 (97) 777-88-99"]},
                        {"post_url": "https://example.test/?owner=%52ustam"},
                        {"post_url": "https://example.test/?tel=%2B998977778899"},
                        {"title": "Transfer $100 now"},
                        {"amenities": ["Call +998 (90) 123-45-67"]}):
            with self.subTest(changes=changes):
                k = kb(**changes)
                reply, reasons = handle(k, "2 xonali Yunusobodda 600$ gacha", CRITERIA, CONTACT)
                self.assertTrue(reasons)
                self.assertTrue(check(reply, k, CONTACT).ok)
                self.assertNotIn("[L-100]", reply)
                self.assertIn(CONTACT, reply)


class FallbackSafetyTest(unittest.TestCase):
    def test_owner_contact_cannot_be_reintroduced_by_handover(self):
        contact = "+998 (97) 777-88-99"
        for message in ("I want to view it", "2 rooms in Yunusobod"):
            with self.subTest(message=message):
                k = kb(title="Call RUSTAM")
                reply, reasons = handle(k, message, CRITERIA, contact)
                self.assertTrue(reasons)
                self.assertTrue(check(reply, k, contact).ok)
                self.assertNotIn("777", reply)
        # The payment reply carries no contact at all, so there is nothing to leak.
        reply, reasons = handle(kb(title="Call RUSTAM"), "What is the deposit?", CRITERIA, contact)
        self.assertEqual(reasons, [])
        self.assertNotIn("777", reply)

    def test_invalid_realtor_contact_is_not_returned_in_final_fallback(self):
        for contact in ("RUSTAM", "Transfer $100 now", "+9989012345670"):
            with self.subTest(contact=contact):
                k = kb(title="Call RUSTAM")
                reply, reasons = handle(k, "I need 2 rooms", CRITERIA, contact)
                self.assertTrue(reasons)
                self.assertTrue(check(reply, k, contact).ok)
                self.assertNotIn("RUSTAM", reply)
                self.assertNotIn("Transfer", reply)

    def test_final_no_contact_template_is_also_checked(self):
        # A private name may collide even with a static safe-sounding template.
        k = kb(owner_name="realtor", title="realtor")
        reply, reasons = handle(k, "I need 2 rooms", CRITERIA, "realtor")
        self.assertTrue(reasons)
        self.assertTrue(check(reply, k, "realtor").ok)
        self.assertNotIn("realtor", reply.casefold())

    def test_checker_does_not_trust_the_composer(self):
        k = kb()
        with patch("estate.agent.compose", return_value="Try [l-999]; transfer $100 now"):
            reply, reasons = handle(k, "Hello", CRITERIA, CONTACT)
        self.assertTrue(reasons)
        self.assertTrue(check(reply, k, CONTACT).ok)
        self.assertNotIn("l-999", reply)


class LanguageServiceTest(unittest.TestCase):
    def test_all_uzbek_apostrophes_detect_viewing_and_payment(self):
        for apostrophe in ("'", "‘", "’", "ʻ", "ʼ", "`", "´"):
            for text in (f"Ko{apostrophe}rish mumkinmi?", f"To{apostrophe}lov qilaymi?"):
                with self.subTest(apostrophe=apostrophe, text=text):
                    self.assertEqual(detect(text), "uz-latn")
            self.assertTrue(wants_viewing(f"Ko{apostrophe}rish mumkinmi?"))
            self.assertTrue(mentions_payment(f"To{apostrophe}lov qilaymi?"))
        self.assertTrue(wants_viewing("Korish mumkinmi?"))
        self.assertTrue(mentions_payment("Pul yuboraymi?"))
        self.assertEqual(detect("Пул ўтказайми?"), "uz-cyrl")
        self.assertTrue(mentions_payment("Пул ўтказайми?"))

    def test_actual_matching_cards_are_localized(self):
        cases = (
            ("2 xonali Yunusobodda 600$ gacha", "uz-latn", ("/oy", "xona", "Qulayliklar:", "Rasmlar:", "E'lon:", "lift", "konditsioner")),
            ("Менга Юнусободда 2 хонали уй керак 600$", "uz-cyrl", ("/ой", "хона", "Қулайликлар:", "Расмлар:", "Эълон:", "лифт", "кондиционер")),
            ("Нужна 2-комн квартира в Юнусабаде до 600$", "ru", ("/месяц", "комн.", "Удобства:", "Фото:", "Объявление:", "лифт", "кондиционер")),
            # Clients write Uzbek or Russian; other Latin text is answered in Uzbek.
            ("2 room apartment in Yunusobod for 600$", "uz-latn", ("/oy", "xona", "Qulayliklar:", "Rasmlar:", "lift")),
        )
        for message, lang, expected in cases:
            with self.subTest(lang=lang):
                k = kb()
                reply, reasons = handle(k, message, extract(message), CONTACT)
                self.assertEqual(reasons, [])
                self.assertIn("[L-100]", reply)
                for token in expected:
                    self.assertIn(token, reply)
                self.assertTrue(check(reply, k, CONTACT).ok)
                self.assertEqual(format_listing(k.get("L-100"), lang), reply)

    def test_localized_viewing_routes_to_realtor_and_payment_is_refused_without_contact(self):
        # The contact goes out only when the client wants to see the place or the realtor.
        for message, expected, gets_contact in (("Ko’rish mumkinmi?", "rieltor", True),
                                                ("Кўрсам бўладими?", "риэлтор", True),
                                                ("Можно посмотреть?", "риэлтор", True),
                                                ("Can I see the apartment?", "rieltor", True),
                                                ("Toʻlovni o'tkazaymi?", "so'ramaymiz", False),
                                                ("Олдиндан тўлов керакми?", "сўрамаймиз", False),
                                                ("Нужна предоплата?", "не берём", False),
                                                ("Can I pay a deposit?", "so'ramaymiz", False)):
            with self.subTest(message=message):
                reply, reasons = handle(kb(), message, Criteria(), CONTACT)
                self.assertEqual(reasons, [])
                self.assertEqual(CONTACT in reply, gets_contact)
                self.assertIn(expected, reply)

    def test_no_matches_does_not_promise_an_unperformed_callback(self):
        for lang, forbidden in (("en", "will contact"), ("ru", "свяжется"),
                                ("uz-latn", "bog'lanadi"), ("uz-cyrl", "боғланади")):
            with self.subTest(lang=lang):
                reply = compose(kb(), "", Criteria(100, "Yunusobod", 2), lang, CONTACT)
                self.assertNotIn(forbidden, reply)
                self.assertTrue(check(reply, kb(), CONTACT).ok)

    def test_ordinary_rent_cards_in_all_languages_pass(self):
        k = KnowledgeBase.from_json("demo")
        for lang in ("en", "ru", "uz-latn", "uz-cyrl"):
            for item in k.available():
                with self.subTest(lang=lang, listing=item.id):
                    self.assertTrue(check(format_listing(item, lang), k, CONTACT).ok)


class ListingValidityTest(unittest.TestCase):
    def test_missing_or_nonpositive_mandatory_fields_never_recommended(self):
        for changes in ({"title": " "}, {"district": ""}, {"district": " "},
                        {"rent_usd": 0}, {"rent_usd": -100}, {"rooms": 0},
                        {"rooms": -1}, {"area_m2": 0}, {"area_m2": -1},
                        {"area_m2": float("nan")}, {"area_m2": float("inf")},
                        {"title": None}, {"district": None}, {"rooms": None},
                        {"rent_usd": None}, {"area_m2": None}):
            with self.subTest(changes=changes):
                item = listing(**changes)
                k = KnowledgeBase("safety", [item])
                self.assertFalse(is_complete(item))
                self.assertEqual(k.available(), [])
                self.assertEqual(recommend(k, Criteria(600)), [])
                self.assertFalse(check("Try [L-100]", k).ok)
                reply, _ = handle(k, "2 rooms in Yunusobod", CRITERIA, CONTACT)
                self.assertNotIn("[L-100]", reply)

    def test_incomplete_ingested_post_is_retained_but_not_offered(self):
        item = parse_post("l-100", "Yunusobod 2 xonali 500$")
        k = KnowledgeBase("safety")
        k.add(item)
        self.assertIsNotNone(k.get("L-100"))
        self.assertFalse(is_complete(item))
        self.assertEqual(k.available(), [])

    def test_lowercase_ids_are_canonical_from_constructor_ingest_and_lookup(self):
        k = kb(id="l-100")
        self.assertEqual(k.get("l-100").id, "L-100")
        self.assertEqual(k.get("L-100").id, "L-100")
        self.assertEqual(k.apply_control_message("l-100 taken"), "L-100 band deb belgilandi.")
        self.assertFalse(k.get("L-100").available)
        item = parse_post("l-101", "Yunusobod 2 xonali 62 m2 500$")
        self.assertEqual(item.id, "L-101")

    def test_video_field_is_optional_and_appended_without_positional_breakage(self):
        item = Listing("L-100", "Flat", "Yunusobod", 2, 62, 500, [], [], "", "", "", "taken")
        self.assertEqual(item.status, "taken")
        self.assertEqual(item.videos, [])
        self.assertEqual(replace(item, videos=["video.mp4"]).videos, ["video.mp4"])
        self.assertIsNot(item.videos, listing().videos)


class EverydayLanguageTest(unittest.TestCase):
    def test_everyday_uzbek_is_recognised_and_unclear_text_stays_unknown(self):
        from estate.language import clear_language
        for text, lang in (("Bugun nechta mijoz yozdi?", "uz-latn"), ("Hozir qaysi joylar bor?", "uz-latn"),
                           ("Бугун нечта мижоз ёзди?", "uz-cyrl"), ("Сколько клиентов писали сегодня?", "ru"),
                           ("Hello", None), ("700$", None), ("ok", None)):
            with self.subTest(text=text):
                self.assertEqual(clear_language(text), lang)


class PrivateChannelLinkTest(unittest.TestCase):
    """A private channel's post link holds a long number that is not a phone."""
    LINK = "https://t.me/c/4496013481/5"

    def kb(self, **changes):
        values = dict(id="L-100", title="Yunusobod 2 xonali", district="Yunusobod", rooms=2, area_m2=62,
                      rent_usd=550, post_url=self.LINK, owner_name="Rustam", owner_phone="+998977778899")
        values.update(changes)
        return KnowledgeBase("demo", [Listing(**values)])

    def test_own_post_link_passes_in_every_card_language(self):
        k = self.kb()
        for lang in ("ru", "uz-latn", "uz-cyrl"):
            with self.subTest(lang=lang):
                self.assertTrue(check(format_listing(k.get("L-100"), lang), k).ok)

    def test_other_long_numbers_are_still_unknown_phones(self):
        k = self.kb()
        for text in ("See https://t.me/c/4496013482/5", f"{self.LINK}998901234567",
                     "Tel 4496013481"):
            with self.subTest(text=text):
                self.assertIn("unknown phone", " ".join(check(text, k).reasons))

    def test_taken_listing_link_and_owner_phone_in_a_link_are_not_exempt(self):
        self.assertFalse(check(f"Post: {self.LINK}", self.kb(status="taken")).ok)
        hidden = "https://t.me/c/998977778899/5"
        self.assertIn("owner phone of L-100 leaked", check(f"Post: {hidden}", self.kb(post_url=hidden)).reasons)


if __name__ == "__main__":
    unittest.main()
