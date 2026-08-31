import threading
import time
import unittest

from capture import (
    TextCapturer,
    _anchor_near_start,
    _find_anchor,
    _is_strong_anchor,
    _trim_trailing_ui,
)


class _BlockingCapturer(TextCapturer):
    def __init__(self, on_text, on_error):
        super().__init__(on_text, on_error, debug=False)
        self.started = threading.Event()
        self.release = threading.Event()

    def _extract(self, x, y, log):
        if x == 1:
            self.started.set()
            self.release.wait(2)
        return "最初の結果" if x == 1 else "最後の結果"


class CaptureGenerationTests(unittest.TestCase):
    def test_cancel_discards_an_inflight_result(self):
        spoken = []
        errors = []
        capturer = _BlockingCapturer(spoken.append, lambda: errors.append(True))
        capturer.start()

        capturer.request(1, 1)
        self.assertTrue(capturer.started.wait(1))
        capturer.cancel()
        capturer.release.set()
        time.sleep(0.1)

        self.assertEqual([], spoken)
        self.assertEqual([], errors)

    def test_a_new_click_discards_the_previous_result(self):
        spoken = []
        delivered = threading.Event()

        def on_text(text):
            spoken.append(text)
            delivered.set()

        capturer = _BlockingCapturer(on_text, self.fail)
        capturer.start()

        capturer.request(1, 1)
        self.assertTrue(capturer.started.wait(1))
        capturer.request(2, 2)
        capturer.release.set()
        self.assertTrue(delivered.wait(1))

        self.assertEqual(["最後の結果"], spoken)


class CaptureValidationTests(unittest.TestCase):
    def test_anchor_must_correspond_to_the_start_of_the_result(self):
        anchor = "平素より当社サービスをご利用いただき、ありがとうございます。"
        self.assertTrue(_anchor_near_start(anchor + "\n本文", anchor))
        self.assertTrue(_anchor_near_start("当社サービスをご利用いただき、本文", anchor))
        self.assertFalse(_anchor_near_start("候補が更新されました", anchor))

    def test_anchor_matching_ignores_whitespace(self):
        self.assertEqual(2, _find_anchor("前\n契 約 本文", "契約本文"))

    def test_short_or_symbol_only_names_are_not_strong_anchors(self):
        self.assertFalse(_is_strong_anchor("OK"))
        self.assertFalse(_is_strong_anchor("1234"))
        self.assertTrue(_is_strong_anchor("読み上げ対象"))

    def test_trailing_ui_counters_are_removed(self):
        self.assertEqual("本文です", _trim_trailing_ui("本文です\n12\n3.5万\n￼"))


if __name__ == "__main__":
    unittest.main()
