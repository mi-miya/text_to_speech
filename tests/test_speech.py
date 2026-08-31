import unittest

from speech import _split_first, sanitize_for_sapi


class SpeechTextTests(unittest.TestCase):
    def test_invalid_xml_characters_are_removed(self):
        self.assertEqual("前 後", sanitize_for_sapi("前\x00後"))

    def test_invisible_web_characters_are_removed(self):
        self.assertEqual("読み上げ", sanitize_for_sapi("読み\u200b上げ\ufffc"))

    def test_first_chunk_ends_at_a_sentence_boundary(self):
        first, rest = _split_first("短い文です。" * 20)
        self.assertGreaterEqual(len(first), 60)
        self.assertTrue(first.endswith("。"))
        self.assertTrue(rest)


if __name__ == "__main__":
    unittest.main()
