import time
import unittest
from unittest import mock

import speech
from speech import SpeechEngine, _split_first, sanitize_for_sapi, strip_unspoken


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

    def test_vertical_tab_and_noncharacters_do_not_reach_ssml(self):
        # 実機の Nanami は VT / FF / U+FFFE で合成が失敗する(XML 1.0 で不正な文字)。
        self.assertEqual("前\n後", sanitize_for_sapi("前\x0b後"))
        self.assertEqual("前 後", sanitize_for_sapi("前\ufffe後"))
        self.assertEqual("前 後", sanitize_for_sapi("前\uffff後"))
        self.assertEqual("前 後", sanitize_for_sapi("前\ud800後"))  # 孤立サロゲート
        self.assertEqual("前😀後", sanitize_for_sapi("前😀後"))  # BMP 外の絵文字は残す

    def test_split_first_never_loses_or_duplicates_text(self):
        samples = [
            "", "   \n ", "句点なし" * 40, "短い。" * 5, "行1\n行2\n", "a" * 5000 + "。",
            "本当？" * 30, "Really? " * 30, "すごい！" * 30, "あ。" * 100,
        ]
        for text in samples:
            first, rest = _split_first(text)
            self.assertEqual(text, first + rest)

    def test_fullwidth_question_mark_is_a_sentence_boundary(self):
        first, rest = _split_first("本当？" * 30)
        self.assertTrue(first.endswith("？"))
        self.assertEqual(60, len(first))
        self.assertTrue(rest)


class StripUnspokenTests(unittest.TestCase):
    def test_urls_are_skipped_without_eating_following_text(self):
        self.assertEqual("詳細は をご覧ください",
                         strip_unspoken("詳細はhttps://x.com/a/status/1?s=20をご覧ください"))
        # X はスキームを省き、末尾を … で切って表示する
        self.assertEqual("記事 です", strip_unspoken("記事 example.com/news/2026… です"))
        self.assertEqual("", strip_unspoken("pic.x.com/AbCd").strip())

    def test_text_that_only_looks_like_a_url_is_kept(self):
        for text in ("Node.js 3.5万 50% 1+1=2 ¥100", "and/or 2026/10/10", "a@b.com"):
            self.assertEqual(text, strip_unspoken(text))

    def test_emoji_and_decorations_are_skipped(self):
        self.assertEqual("すごい ！ 拡散希望",
                         strip_unspoken("すごい\U0001F600\U0001F44D\U0001F3FD！\U0001F1EF\U0001F1F5 ★♪→ #拡散希望"))
        self.assertEqual("本文", strip_unspoken("━━━━━\n本文\n-----").strip())

    def test_punctuation_and_long_vowel_marks_survive(self):
        text = "わーーーい……本当に――そう。「はい」、いいえ!?"
        self.assertEqual(text, strip_unspoken(text))


class PickVoiceTests(unittest.TestCase):
    VOICES = [
        "Microsoft Haruka Desktop - Japanese",
        "Microsoft Nanami Online (Natural) - Japanese (Japan)",  # 列挙順で先に来ても
        "Microsoft Nanami (Natural) - Japanese (Japan)",
    ]

    def test_partial_name_prefers_local_voice_over_online(self):
        self.assertEqual(2, SpeechEngine._pick_voice(self.VOICES, "Microsoft Nanami"))

    def test_online_voice_is_selectable_when_named_explicitly(self):
        self.assertEqual(1, SpeechEngine._pick_voice(self.VOICES, "Nanami Online"))

    def test_exact_match_wins_and_bad_names_select_nothing(self):
        self.assertEqual(0, SpeechEngine._pick_voice(self.VOICES, self.VOICES[0].upper()))
        for name in ("", "  ", None, 123, "存在しない音声"):
            self.assertIsNone(SpeechEngine._pick_voice(self.VOICES, name))


class EngineRobustnessTests(unittest.TestCase):
    def test_invalid_rate_does_not_kill_worker_and_text_is_not_xml(self):
        calls = []

        class FakeVoice:
            Rate = 0

            def Speak(self, text, flags):
                calls.append((text, flags))

        with mock.patch.object(speech.win32com.client, "Dispatch",
                               return_value=FakeVoice()):
            engine = SpeechEngine()
            for bad in ("abc", None, float("inf"), [1]):
                engine.set_rate(bad)
            engine.speak("<speak>こんにちは</speak>")
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                time.sleep(0.01)

        self.assertTrue(engine._thread.is_alive())
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0][1] & speech.SVSF_IS_NOT_XML)


if __name__ == "__main__":
    unittest.main()
