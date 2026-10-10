import os
import tempfile
import threading
import time
import unittest

import capture
from capture import (
    Start,
    TextCapturer,
    _anchor_consistent,
    _anchor_near_start,
    _find_anchor,
    _has_content,
    _is_strong_anchor,
    _trim_to_anchor,
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
        self.assertEqual("本文です", _trim_trailing_ui("本文です\n12\n3.5万\n\ufffc"))

    def test_trailing_reaction_labels_are_removed(self):
        text = ("本文です。\n\ufffc\n返信\n24\n件\n"
                "共感した\n4174\nなるほど\n227\nうーん")
        self.assertEqual("本文です。", _trim_trailing_ui(text))

    def test_short_closing_lines_of_the_body_are_kept(self):
        # 件数だけの行が伴わない短い行は本文の一部として残す
        self.assertEqual("本文です。\nおわり", _trim_trailing_ui("本文です。\nおわり"))
        self.assertEqual("ではまた\n了解\nはい",
                         _trim_trailing_ui("ではまた\n了解\nはい"))


class AnchorConsistencyTests(unittest.TestCase):
    ELEMENT = "クリックした段落の本文がここに入ります。"
    BODY = "前置きの段落。\n" + ELEMENT + "\n後続の段落。"
    BASE = BODY.index(ELEMENT)

    def test_a_position_inside_the_clicked_element_is_accepted(self):
        idx = self.BODY.index("ここに入ります")
        self.assertTrue(_anchor_consistent(idx, self.BASE, self.ELEMENT))

    def test_a_position_outside_the_clicked_element_is_rejected(self):
        idx = self.BODY.index("後続の段落")
        self.assertFalse(_anchor_consistent(idx, self.BASE, self.ELEMENT))

    def test_it_passes_when_the_element_text_cannot_be_verified(self):
        # 要素テキストが弱い / 本文中に見つからない (-1) ときは検証できないので通す
        self.assertTrue(_anchor_consistent(0, 0, "OK"))
        self.assertTrue(_anchor_consistent(0, -1, self.ELEMENT))


class _FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeRange:
    """等幅・折り返しありのテキストを模した TextRange。

    文字 i は line = i // LINE_LEN 行目の col = i % LINE_LEN 列目にあり、
    矩形は (col*W, line*H)-((col+1)*W, (line+1)*H)。UI Automation を起動せずに
    「矩形だけでクリック位置の文字を割り出す」ロジックを検証するために使う。
    """

    LINE_LEN = 10
    W = 8
    H = 20

    def __init__(self, text, start=0, end=None):
        self.text = text
        self.start = start
        self.end = len(text) if end is None else end

    def Clone(self):
        return _FakeRange(self.text, self.start, self.end)

    def GetText(self, limit=-1):
        chunk = self.text[self.start:self.end]
        return chunk if limit < 0 else chunk[:limit]

    def MoveEndpointByUnit(self, endpoint, unit, count, waitTime=0):
        if endpoint == Start:
            target = max(0, min(len(self.text), self.start + count))
            moved = target - self.start
            self.start = target
            self.end = max(self.end, self.start)
        else:
            target = max(self.start, min(len(self.text), self.end + count))
            moved = target - self.end
            self.end = target
        return moved

    def MoveEndpointByRange(self, endpoint, other, other_endpoint, waitTime=0):
        # 本番コードは End を自身の Start へ畳む用途にしか使わない
        self.end = other.start if other_endpoint == Start else other.end

    def GetBoundingRectangles(self):
        rects = []
        for i in range(self.start, self.end):
            line, col = divmod(i, self.LINE_LEN)
            rects.append(_FakeRect(col * self.W, line * self.H,
                                   (col + 1) * self.W, (line + 1) * self.H))
        return rects


class ClickCharacterTests(unittest.TestCase):
    """クリックした「文字」から読み始められるか(行頭からではなく)。"""

    TEXT = "0123456789" * 5  # 5 行 x 10 文字

    def setUp(self):
        self.capturer = TextCapturer(lambda t: None, lambda: None, debug=False)

    def _refine(self, x, y):
        return self.capturer._refine_to_click_char(_FakeRange(self.TEXT), x, y)

    def test_reading_starts_at_the_clicked_character(self):
        # 3 行目 (y=40..60) の 5 文字目 (x=32..40) をクリック
        self.assertEqual("456789" + self.TEXT[30:], self._refine(35, 50).GetText())

    def test_clicking_the_left_margin_starts_at_the_head_of_that_line(self):
        self.assertEqual(self.TEXT[20:], self._refine(0, 50).GetText())

    def test_clicking_the_first_character_keeps_the_whole_range(self):
        self.assertEqual(self.TEXT, self._refine(0, 10).GetText())

    def test_a_click_below_every_character_leaves_the_range_untouched(self):
        self.assertEqual(self.TEXT, self._refine(35, 5000).GetText())


class _DegenerateRectRange(_FakeRange):
    """リンク先頭など、Chromium が高さ 0 の退化矩形 (0,0,1,0) を返す文字を再現する。"""

    BROKEN = {3, 4}

    def Clone(self):
        return _DegenerateRectRange(self.text, self.start, self.end)

    def GetBoundingRectangles(self):
        rects = super().GetBoundingRectangles()
        for n, i in enumerate(range(self.start, self.end)):
            if i in self.BROKEN:
                rects[n] = _FakeRect(0, 0, 1, 0)
        return rects


class DegenerateRectTests(unittest.TestCase):
    TEXT = "0123456789" * 5

    def setUp(self):
        self.capturer = TextCapturer(lambda t: None, lambda: None, debug=False)

    def _refine(self, x, y):
        return self.capturer._refine_to_click_char(
            _DegenerateRectRange(self.TEXT), x, y)

    def test_rects_with_zero_height_are_dropped(self):
        self.assertEqual([], capture._rects(_DegenerateRectRange(self.TEXT, 3, 5)))

    def test_a_click_after_unmeasurable_characters_still_starts_at_the_clicked_one(self):
        # 1 行目 (y=0..20) の 7 文字目 (x=48..56)。3,4 文字目は矩形が取れない
        self.assertEqual(self.TEXT[6:], self._refine(52, 10).GetText())

    def test_an_unmeasurable_character_is_not_skipped_when_it_is_the_clicked_one(self):
        # 4 文字目自体は測れない。手前から読むだけで、それより後ろには進まない
        start = len(self.TEXT) - len(self._refine(30, 10).GetText())
        self.assertLessEqual(start, 4)


class _OddRectRange(_FakeRange):
    """指定した文字だけ、測れない矩形 / 幅 1px の退化矩形を返す。"""

    BROKEN = set()   # 高さ 0 の矩形 (測れない)
    NARROW = set()   # 左端だけの幅 1px の矩形 (実体が無い)

    def Clone(self):
        return type(self)(self.text, self.start, self.end)

    def GetBoundingRectangles(self):
        rects = super().GetBoundingRectangles()
        for n, i in enumerate(range(self.start, self.end)):
            if i in self.BROKEN:
                rects[n] = _FakeRect(0, 0, 1, 0)
            elif i in self.NARROW:
                rects[n].right = rects[n].left + 1
        return rects


class _BrokenFive(_OddRectRange):
    BROKEN = {5}


class _NarrowFive(_OddRectRange):
    NARROW = {5}


class _NarrowFirst(_OddRectRange):
    NARROW = {0}


class ClickAfterUnmeasurableCharTests(unittest.TestCase):
    """測れない文字の直後 / 退化矩形の文字をクリックしても、押した文字から始める。"""

    TEXT = "0123456789" * 5

    def setUp(self):
        self.capturer = TextCapturer(lambda t: None, lambda: None, debug=False)

    def _head(self, cls, x, y):
        return self.capturer._refine_to_click_char(cls(self.TEXT), x, y).GetText()

    def test_the_char_after_an_unmeasurable_one_is_where_reading_starts(self):
        # 5 文字目は測れない。6 文字目 (x=48..56) をクリックしたら 5 文字目に戻らない
        self.assertEqual(self.TEXT[6:], self._head(_BrokenFive, 52, 10))

    def test_the_left_edge_of_the_char_after_an_unmeasurable_one_counts(self):
        self.assertEqual(self.TEXT[6:], self._head(_BrokenFive, 48, 10))

    def test_a_click_before_the_char_after_an_unmeasurable_one_does_not_skip(self):
        # 4 文字目 (x=32..40) をクリック。測れない 5 文字目より先へ進まない
        self.assertEqual(self.TEXT[4:], self._head(_BrokenFive, 36, 10))

    def test_a_first_char_with_a_one_pixel_rect_is_not_dropped(self):
        # 行頭の文字 (x=0..8) の矩形が幅 1px (0..1) でも、その中央 (4) を押せば行頭から読む。
        # 以前は右端 1 <= 4 で「手前」と判定され、先頭 1 文字が欠けていた
        self.assertEqual(self.TEXT, self._head(_NarrowFirst, 4, 10))

    def test_a_one_pixel_rect_does_not_make_the_clicked_char_look_passed(self):
        # 5 文字目 (x=40..48) の矩形が幅 1px (40..41) でも、その中央 (44) を押せば 5 文字目から
        self.assertEqual(self.TEXT[5:], self._head(_NarrowFive, 44, 10))


class TrimToAnchorTests(unittest.TestCase):
    ANCHOR = "ABCDEFG hello world"

    def test_text_starting_before_the_clicked_element_is_cut_to_its_head(self):
        text = "い。\nユーザー名四 @user4\n" + self.ANCHOR + "、続き。"
        self.assertEqual(self.ANCHOR + "、続き。", _trim_to_anchor(text, self.ANCHOR))

    def test_text_that_already_starts_at_the_element_is_unchanged(self):
        text = self.ANCHOR + "、続き。"
        self.assertEqual(text, _trim_to_anchor(text, self.ANCHOR))

    def test_text_without_the_anchor_is_unchanged(self):
        self.assertEqual("別の文", _trim_to_anchor("別の文", self.ANCHOR))
        self.assertEqual("本文", _trim_to_anchor("本文", ""))


class _LineRange(_FakeRange):
    """行単位の移動ができ、MEASURED_LINES 行目以降は矩形が取れない TextRange。

    文書末尾の隠れた領域や line-clamp で見えない行を再現する。
    """

    MEASURED_LINES = 4

    def Clone(self):
        return _LineRange(self.text, self.start, self.end)

    def ExpandToEnclosingUnit(self, unit, waitTime=0):
        self.start = self.start // self.LINE_LEN * self.LINE_LEN
        self.end = min(len(self.text), self.start + self.LINE_LEN)

    def Move(self, unit, count, waitTime=0):
        lines = (len(self.text) + self.LINE_LEN - 1) // self.LINE_LEN
        cur = self.start // self.LINE_LEN
        target = max(0, min(lines - 1, cur + count))
        self.start = target * self.LINE_LEN
        self.end = min(len(self.text), self.start + self.LINE_LEN)
        return target - cur

    def GetBoundingRectangles(self):
        limit = self.MEASURED_LINES * self.LINE_LEN
        rects = super().GetBoundingRectangles()
        return [r for n, r in zip(range(self.start, self.end), rects) if n < limit]


class LineSeekIntoUnmeasurableRegionTests(unittest.TestCase):
    TEXT = "0123456789" * 6  # 6 行。4 行目 (添字 3) までは矩形が取れ、5 行目以降は取れない

    def setUp(self):
        self.capturer = TextCapturer(lambda t: None, lambda: None, debug=False)

    def test_the_click_line_is_found_even_when_the_jump_lands_in_unmeasurable_lines(self):
        # 倍々の探索が 4 行先 (測れない領域) へ飛んでも、手前の 3 行目 (y=60..80) の
        # クリック文字 (x=16..24 の 3 文字目) を見つける。以前は 2 行目の先頭から読んでいた
        seed = _LineRange(self.TEXT)
        seed.ExpandToEnclosingUnit(None)
        rng, verified = self.capturer._start_range_at_point(None, 20, 70, seed=seed)
        self.assertTrue(verified)
        self.assertEqual(self.TEXT[32:40], rng.GetText())


class CountOnlyTests(unittest.TestCase):
    def test_counter_only_text_has_no_content(self):
        self.assertFalse(_has_content("133 663 3.5万"))
        self.assertFalse(_has_content("12\n133\n\ufffc"))

    def test_text_with_a_body_line_has_content(self):
        self.assertTrue(_has_content("なるほど\n227\nうーん"))
        self.assertTrue(_has_content("本文です。\n12"))


class DebugLogRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "debug.log")
        self._saved = capture.DEBUG_LOG
        capture.DEBUG_LOG = self.log
        self.addCleanup(setattr, capture, "DEBUG_LOG", self._saved)
        self.capturer = TextCapturer(lambda t: None, lambda: None, debug=True)

    def _read(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def test_only_the_latest_entries_are_kept(self):
        for i in range(capture.DEBUG_KEEP + 5):
            self.capturer._write_debug(i, i, f"t{i}", [f"l{i}"])
        text = self._read()
        self.assertEqual(capture.DEBUG_KEEP, text.count(capture.DEBUG_SEP))
        self.assertIn(f"click=({capture.DEBUG_KEEP + 4},", text)
        self.assertNotIn("click=(0,0)", text)

    def test_a_corrupted_log_does_not_raise_and_is_recovered(self):
        with open(self.log, "wb") as f:
            f.write(b"\xff\xfe" + capture.DEBUG_SEP.encode() + b"\x80\x81")
        self.capturer._write_debug(1, 2, "ok", ["l"])
        self.assertIn("click=(1,2)", self._read())

    def test_text_with_a_lone_surrogate_does_not_raise(self):
        self.capturer._write_debug(1, 2, "a" + chr(0xD800) + "b", ["x" + chr(0xDC00)])
        self.assertIn("click=(1,2)", self._read())


if __name__ == "__main__":
    unittest.main()
