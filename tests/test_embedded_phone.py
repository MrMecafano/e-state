"""Known owner contacts cannot hide behind a word prefix."""
import unittest
from estate.checker import check
from estate.kb import KnowledgeBase, Listing


class EmbeddedOwnerPhoneTest(unittest.TestCase):
    def test_known_owner_phone_with_letter_prefix_is_private(self):
        kb = KnowledgeBase('tenant', [Listing('L-001', 'Synthetic', 'Yunusobod', 2, 50, 500,
                                             owner_phone='998977778899')])
        for text in ('Call tel998977778899', 'tel977778899ga yozing',
                     'https://example.test/tel998977778899',
                     'tel998%2097%20777%2088%2099'):
            with self.subTest(text=text):
                verdict = check(text, kb, '998977778899')
                self.assertFalse(verdict.ok)
                self.assertIn('owner phone of L-001 leaked', verdict.reasons)

    def test_known_structural_long_listing_id_still_allowed(self):
        kb = KnowledgeBase('tenant', [Listing('L-385650130262496726', 'Synthetic', 'Yunusobod', 2, 50, 500,
                                             owner_phone='998977778899')])
        self.assertTrue(check('Try [L-385650130262496726]', kb).ok)
