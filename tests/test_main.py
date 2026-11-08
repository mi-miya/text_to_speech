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


class AutoRestartTests(unittest.TestCase):
    """長く動いた後の空き時間にだけ自動で再起動する判定。"""

    def make_app(self, uptime, idle, speaking=False):
        now = main.time.monotonic()
        app = types.SimpleNamespace(
            _started=now - uptime, _last_active=now - idle,
            speech=types.SimpleNamespace(is_speaking=speaking))
        app._mark_active = lambda: main.App._mark_active(app)
        return app

    def due(self, app, busy=False):
        return main.App.auto_restart_due(app, busy=busy)

    def test_restarts_only_after_long_uptime_and_long_idle(self):
        long_up, long_idle = main.AUTO_RESTART_AFTER + 1, main.AUTO_RESTART_IDLE + 1
        self.assertTrue(self.due(self.make_app(long_up, long_idle)))
        self.assertFalse(self.due(self.make_app(long_up, 5)))         # さっき使った
        self.assertFalse(self.due(self.make_app(60, long_idle)))      # 起動したばかり

    def test_never_restarts_while_speaking_or_a_dialog_is_open(self):
        long_up, long_idle = main.AUTO_RESTART_AFTER + 1, main.AUTO_RESTART_IDLE + 1
        app = self.make_app(long_up, long_idle, speaking=True)
        self.assertFalse(self.due(app))
        app.speech.is_speaking = False
        self.assertFalse(self.due(app))  # 読み終えた直後から、また空き時間を数え直す
        self.assertFalse(self.due(self.make_app(long_up, long_idle), busy=True))


class _Suppressed(Exception):
    pass


class MouseFilterTests(unittest.TestCase):
    """低レベルフックの抑制ロジック。App 本体は組み立てず必要な属性だけ持つ。"""

    def setUp(self):
        self.requests = []
        self.keys_down = False
        self.app = types.SimpleNamespace(
            _swallow_up=False,
            _mark_active=lambda: None,
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
