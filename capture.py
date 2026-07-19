"""UI Automation でクリック位置から文書末尾までのテキストを取得する。

段落の途中をクリックすると、その位置の要素が持つ TextPattern は
「その行/ブロックだけ」を範囲とすることがあり、それが原因で読み上げが
途中で止まっていた(見出しから始めると読める、という症状の正体)。

対策として、クリック位置から親を辿って TextPattern を持つ祖先を集め、
その中で「最も多くのテキストを含むコンテナ」の全文を採用する。さらに
クリックした行のテキストを目印(アンカー)として全文中から探し、
そこから末尾までを読み上げる。アンカーが見つからないときは全文を読む。

デバッグ用に、直近の取得内容を capture_debug.log(毎回上書き)に残す。
"""
import os
import queue
import threading

import uiautomation as auto

MAX_WALK_UP = 20      # TextPattern を探して親を辿る最大段数
ANCHOR_MAX = 400      # クリック行から採るアンカー文字数の上限
DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_debug.log")

End = auto.TextPatternRangeEndpoint.End


class TextCapturer(threading.Thread):
    def __init__(self, on_text, on_error, max_chars=30000, debug=True):
        super().__init__(daemon=True, name="capture")
        self._q = queue.Queue()
        self._on_text = on_text
        self._on_error = on_error
        self._max_chars = max_chars
        self._debug = debug

    def request(self, x, y):
        self._q.put((x, y))

    def run(self):
        with auto.UIAutomationInitializerInThread():
            while True:
                x, y = self._q.get()
                # 連打された場合は最後のクリックだけ処理する
                while not self._q.empty():
                    x, y = self._q.get()
                lines = []
                try:
                    text = self._extract(x, y, lines)
                except Exception as e:
                    text = None
                    lines.append(f"EXCEPTION: {type(e).__name__}: {e}")
                self._write_debug(x, y, text, lines)
                if text and text.strip():
                    self._on_text(text)
                else:
                    self._on_error()

    # ---- テキスト取得本体 ----

    def _extract(self, x, y, log):
        control = auto.ControlFromPoint(x, y)

        # クリック位置から親へ辿り、TextPattern を持つ祖先をすべて集める
        providers = []  # (control, pattern, doc_text)
        node = control
        for depth in range(MAX_WALK_UP):
            if node is None:
                break
            name = ""
            try:
                name = node.Name or ""
            except Exception:
                pass
            pattern = None
            try:
                pattern = node.GetPattern(auto.PatternId.TextPattern)
            except Exception:
                pattern = None
            doc_text = ""
            if pattern:
                try:
                    doc_text = pattern.DocumentRange.GetText(self._max_chars) or ""
                except Exception:
                    doc_text = ""
                providers.append((node, pattern, doc_text))
            log.append(f"[{depth}] {getattr(node, 'ControlTypeName', '?')} "
                       f"name={name[:30]!r} textPattern={'Y' if pattern else 'N'} "
                       f"docLen={len(doc_text)}")
            try:
                node = node.GetParentControl()
            except Exception:
                break

        if not providers:
            log.append("=> TextPattern を持つ要素なし")
            return None

        # 最も多くのテキストを含むコンテナ = 読み上げ対象の本文
        big_ctrl, big_pat, big_text = max(providers, key=lambda p: len(p[2]))
        # クリック位置に最も近い(最初に見つかった)要素で位置を特定する
        near_pat = providers[0][1]
        log.append(f"=> providers={len(providers)} bigDocLen={len(big_text)}")

        # クリックした行を目印として全文中の開始位置を求める
        anchor = self._anchor_line(near_pat, x, y)
        if not anchor:
            anchor = self._anchor_line(big_pat, x, y)
        log.append(f"=> anchor={anchor[:40]!r}")

        if anchor:
            idx = _find_anchor(big_text, anchor)
            if idx >= 0:
                log.append(f"=> anchor 一致 idx={idx} 読み上げ長={len(big_text) - idx}")
                return big_text[idx:]
            log.append("=> anchor 全文中に見つからず")

        # フォールバック1: 大コンテナ上で RangeFromPoint → 末尾まで
        tail = self._range_from_point_to_end(big_pat, x, y)
        if tail and tail.strip():
            log.append(f"=> フォールバック RangeFromPoint 成功 長={len(tail)}")
            return tail

        # フォールバック2: 本文全体を読む
        log.append("=> フォールバック 全文読み上げ")
        return big_text

    @staticmethod
    def _anchor_line(pattern, x, y):
        """クリック位置の1行分のテキストを返す(位置特定の目印)。"""
        try:
            r = pattern.RangeFromPoint(x, y)
        except Exception:
            return ""
        if r is None:
            return ""
        try:
            r.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
        except Exception:
            pass
        try:
            return (r.GetText(ANCHOR_MAX) or "").strip()
        except Exception:
            return ""

    def _range_from_point_to_end(self, pattern, x, y):
        try:
            r = pattern.RangeFromPoint(x, y)
            if r is None:
                return ""
            ok = r.MoveEndpointByRange(End, pattern.DocumentRange, End, waitTime=0)
            if not ok:
                return ""
            return r.GetText(self._max_chars) or ""
        except Exception:
            return ""

    def _write_debug(self, x, y, text, log):
        if not self._debug:
            return
        try:
            with open(DEBUG_LOG, "w", encoding="utf-8") as f:
                f.write(f"click=({x},{y})\n")
                f.write("\n".join(log))
                f.write("\n\n--- 取得テキスト先頭200字 ---\n")
                f.write((text or "")[:200])
                f.write("\n--- 末尾100字 ---\n")
                f.write((text or "")[-100:])
                f.write("\n")
        except OSError:
            pass


def _find_anchor(full_text, anchor):
    """全文中からアンカー行の開始位置を返す。空白差を吸収して探す。"""
    idx = full_text.find(anchor)
    if idx >= 0:
        return idx
    # 空白・改行の差を無視して先頭部分で照合する
    head = "".join(anchor.split())[:30]
    if not head:
        return -1
    compact = []
    mapping = []  # compact 上の各文字が full_text 上の何文字目か
    for i, ch in enumerate(full_text):
        if not ch.isspace():
            compact.append(ch)
            mapping.append(i)
    compact = "".join(compact)
    j = compact.find(head)
    if j >= 0:
        return mapping[j]
    return -1
