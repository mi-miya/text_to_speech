import unittest

from main import normalize, resolve_voice


class MainHelpersTests(unittest.TestCase):
    def test_normalize_preserves_paragraphs_and_collapses_spacing(self):
        self.assertEqual("一 二\n\n三", normalize("  一　　二\r\n\r\n三  "))

    def test_existing_voice_is_kept(self):
        voices = ["Microsoft Haruka Desktop - Japanese",
                  "Microsoft Nanami Online (Natural) - Japanese (Japan)"]
        self.assertEqual(voices[1], resolve_voice(voices[1].lower(), voices))

    def test_missing_voice_falls_back_to_an_offline_voice(self):
        voices = ["Microsoft Nanami Online (Natural) - Japanese (Japan)",
                  "Microsoft Haruka Desktop - Japanese"]
        self.assertEqual(voices[1], resolve_voice("Missing Natural Voice", voices))

    def test_no_saved_voice_leaves_the_system_default_untouched(self):
        self.assertIsNone(resolve_voice(None, ["Microsoft Haruka Desktop - Japanese"]))


if __name__ == "__main__":
    unittest.main()
