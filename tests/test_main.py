import types
import unittest

import main
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

    def test_normalize_treats_crlf_and_lf_paragraphs_alike(self):
        self.assertEqual(normalize("一\r\n\r\n二"), normalize("一\n\n二"))
        self.assertEqual("一\n\n二", normalize("一\n\n\n\n二"))
        self.assertEqual("", normalize(" \t　\r\n "))

    def test_partial_voice_name_never_picks_an_online_voice(self):
        voices = ["Microsoft Nanami Online (Natural) - Japanese (Japan)",
                  "Microsoft Nanami (Natural) - Japanese (Japan)"]
        self.assertEqual(voices[1], resolve_voice("Nanami", voices))

    def test_only_online_voices_means_no_fallback(self):
        voices = ["Microsoft Keita Online (Natural) - Japanese (Japan)"]
        self.assertIsNone(resolve_voice("Missing", voices))
        self.assertIsNone(resolve_voice("Missing", []))


class _Suppressed(Exception):
    pass


class MouseFilterTests(unittest.TestCase):
    """低レベルフックの抑制ロジック。App 本体は組み立てず必要な属性だけ持つ。"""

    def setUp(self):
        self.requests = []
        self.keys_down = False
        self.app = types.SimpleNamespace(
            _swallow_up=False,
            capturer=types.SimpleNamespace(
                request=lambda x, y: self.requests.append((x, y))),
            mouse_listener=types.SimpleNamespace(suppress_event=self._suppress),
        )
        self._orig = main.hotkey_down
        main.hotkey_down = lambda: self.keys_down

    def tearDown(self):
        main.hotkey_down = self._orig

    @staticmethod
    def _suppress():
        raise _Suppressed()

    def fire(self, msg):
        data = types.SimpleNamespace(pt=types.SimpleNamespace(x=10, y=20))
        try:
            main.App._mouse_filter(self.app, msg, data)
        except _Suppressed:
            return True
        return False

    def test_hotkey_click_suppresses_down_and_up(self):
        self.keys_down = True
        self.assertTrue(self.fire(main.WM_LBUTTONDOWN))
        self.keys_down = False  # 先にキーを離しても up は抑制する
        self.assertTrue(self.fire(main.WM_LBUTTONUP))
        self.assertEqual([(10, 20)], self.requests)

    def test_plain_click_is_passed_through(self):
        self.assertFalse(self.fire(main.WM_LBUTTONDOWN))
        self.keys_down = True  # 押下後にキーを押しても up は対象アプリへ渡す
        self.assertFalse(self.fire(main.WM_LBUTTONUP))
        self.assertEqual([], self.requests)

    def test_lost_up_does_not_swallow_the_next_plain_click(self):
        self.keys_down = True
        self.assertTrue(self.fire(main.WM_LBUTTONDOWN))  # up は届かなかった
        self.keys_down = False
        self.assertFalse(self.fire(main.WM_LBUTTONDOWN))
        self.assertFalse(self.fire(main.WM_LBUTTONUP))


if __name__ == "__main__":
    unittest.main()
