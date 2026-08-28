"""UI Automation でクリック位置から「クリックした項目の終わり」までを取得する。

取得は 3 段構えで、上から順に当たった経路を使う。

1. 範囲切り出し (_read_to_boundary) — ブラウザ / VSCode など、本文全体を 1 つの
   TextPattern 文書として持つアプリ向けの本命経路。
     開始 = クリックされた要素 (ControlFromPoint) の範囲を出発点に、外接矩形を
            クリック座標と突き合わせてクリック行まで寄せる。
            座標→文字位置の RangeFromPoint は X のタイムラインのようにセルを
            絶対配置で仮想スクロールする画面で別の投稿を指すことがあるため、
            出発点には使わず、要素の範囲が取れないときのフォールバックに回す。
     終了 = クリック位置を包む ARIA 項目 (role=article / listitem 等) の末尾。
            タイムラインは全体で 1 つの巨大な文書なので、文書末尾まで読むと
            後続の投稿やページのフッターまで読み上げてしまう。項目が無ければ
            main、それも無ければ従来どおり文書末尾まで読む。

2. 子孫集約 (_aggregate_ancestors) — Electron 系アプリは本文を段落ごとに別々の
   TextControl へ分割し、各段落が「その段落だけの小さな TextPattern」を持つ。
   1 の経路に乗せると 1 段落で読み上げが止まるので、祖先配下の TextControl 葉を
   文書順にたどってメッセージ 1 件分の本文を再構成する。

3. アンカー照合 — 再構成した本文の中から、クリックした行のテキストを目印に
   開始位置を探す。見つからなければ本文全体を読む。

デバッグ用に、直近の取得内容を capture_debug.log(毎回上書き)に残す。
"""
import os
import queue
import re
import threading
import time

import uiautomation as auto

MAX_WALK_UP = 60      # TextPattern を探して親を辿る最大段数。届かないと高速な
                      # 「クリック位置→項目末尾」経路に乗れず、低速な子孫集約へ落ちる。
                      # X の会話ビューは実測で本文の葉から DocumentControl まで 25 段、
                      # ユーザー名のようにリンクの内側だと 33 段以上あり、30 では
                      # 足りずに投稿の一部しか読めなくなっていた。1 段あたり COM 呼び
                      # 出し 1 回(1ms 未満)なので、余裕を持たせても取得時間に響かない。
ANCHOR_MAX = 400      # クリック行から採るアンカー文字数の上限

# Electron/Chromium 系アプリ(Claude Desktop 等)は本文を 1 段落ごとに別々の
# TextControl へ分割し、各段落が「その段落だけの小さな TextPattern」を持つ。親の
# GroupControl は TextPattern を持たない。そのため「最大の TextPattern コンテナ」を
# 採ってもクリックした 1 段落しか取れず、読み上げがすぐ止まる。この場合は祖先配下の
# TextControl 葉を文書順にたどって、クリックしたメッセージ 1 件分の本文を再構成する。
AGG_TRIGGER = 500     # TextPattern 本文がこれ未満なら子孫走査で本文を再構成する
MESSAGE_MAX = 14000   # 1 メッセージとして許容する最大文字数。祖先の集約がこれを超えたら
                      # 会話ペイン(複数メッセージ)に達したとみなし、直前の集約で確定する。
BLOCK_MAX = 20000     # 集約テキストの絶対上限(暴走防止の安全弁)
MAX_DESCEND = 40      # 子孫を辿る最大の深さ
MAX_NODES = 6000      # 1 回の集約で走査するノード数の上限(暴走防止)
AGG_STALL_MIN = 16    # 祖先を1段上がってもクリック行以降の増分がこれ未満なら「増えず」とみなす
AGG_STALL_LEVELS = 2  # 増えない段がこれだけ続いたら親を辿るのを打ち切る

# RangeFromPoint の位置ずれを矩形照合で補正する際、クリック行を探して行単位で
# 移動できる最大行数。Chromium 系では要素の先頭が返るため、要素が長いほど遠くなる。
LINE_SEEK_MAX = 512

# 「クリックした 1 件」を包むノードを示す ARIA ロール。読み上げをこの範囲の終わりで
# 止める。X のタイムラインは投稿ごとに role="article" が付くので、これで隣の投稿や
# ページ末尾のフッター(利用規約 / (c) X Corp. 等)まで読み続けるのを防げる。
# row / gridcell / treeitem は「1 行 1 セル」の構造ロールで、表やツリーの上で
# クリックすると 1 行だけで読み上げが止まってしまうため、あえて入れていない。
ITEM_ROLES = ("article", "listitem", "comment")
DESCEND_BUDGET = 400  # クリック対象を矩形で降りて探すときに見る子ノード数の上限
# Chromium はアクセシビリティツリーを遅延構築するため、ControlFromPoint や
# RangeFromChild が一時的に不完全な結果を返すことがある(実測 40 回に 1 回程度)。
# いずれも取り直すと解決するので、静かに劣化した経路へ落ちる前にやり直す。
CLICK_TARGET_RETRY = 3  # クリック対象が葉に届くまでの取り直し回数
RANGE_RETRY = 2         # RangeFromChild の取り直し回数
# 項目ロールが無いページ向けの緩い境界。ヘッダ / ナビ / フッターを除ける。
SECTION_ROLES = ("main",)

DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_debug.log")
DEBUG_KEEP = 8        # ログに残す直近の取得回数。1 回分だけだと「さっきの失敗」を
                      # 追えないため履歴にする(区切りは DEBUG_SEP)。
DEBUG_SEP = "=" * 60

Start = auto.TextPatternRangeEndpoint.Start
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
                started = time.perf_counter()
                try:
                    text = self._extract(x, y, lines)
                except Exception as e:
                    text = None
                    lines.append(f"EXCEPTION: {type(e).__name__}: {e}")
                text = _trim_trailing_ui(text)
                elapsed = time.perf_counter() - started
                lines.insert(0, f"取得所要時間={elapsed * 1000:.0f}ms")
                self._write_debug(x, y, text, lines)
                # 画像やアイコンをクリックすると ￼ や数字だけが返る。読み上げても
                # 無音になるだけなので、取得失敗として橙の点灯で知らせる。
                if text and _has_letter(text):
                    self._on_text(text)
                else:
                    self._on_error()

    # ---- テキスト取得本体 ----

    def _resolve_click_target(self, control, x, y, log=None):
        """クリック座標を含む最も深い子孫まで降りて、実際のクリック対象を返す。

        Chromium はアクセシビリティツリーを遅延構築するため、ControlFromPoint が
        葉ではなく DocumentControl のような粗いノードを返すことがある。そのまま
        使うと祖先を辿っても項目境界 (role=article) に出会えず、開始位置も文書
        先頭のナビゲーション部分になってしまう(タイムラインで関係ない場所から
        読み上げが始まる原因のひとつ)。外接矩形はどのノードでも取れるので、
        座標を含む子を選んで降りることで対象を確定させる。

        既に葉が返っているときは GetChildren が空なので、その場で抜ける。

        戻り値は (対象ノード, 降りた段数, 葉に到達したか)。葉に届かなかったときは
        ツリーが構築途中の可能性が高いので、呼び出し側が取り直す判断に使う。
        """
        node = control
        budget = DESCEND_BUDGET
        depth = 0
        while depth < MAX_DESCEND and budget > 0:
            try:
                children = node.GetChildren()
            except Exception:
                break
            if not children:
                break
            best = None
            best_area = None
            for child in children:
                budget -= 1
                if budget <= 0:
                    break
                try:
                    r = child.BoundingRectangle
                except Exception:
                    continue
                if not (r.left <= x < r.right and r.top <= y < r.bottom):
                    continue
                # 座標を含む中で最も小さいものが、視覚的に最前面の要素
                area = (r.right - r.left) * (r.bottom - r.top)
                if best_area is None or area < best_area:
                    best, best_area = child, area
            if best is None:
                break
            node = best
            depth += 1
        try:
            leaf = not node.GetChildren()
        except Exception:
            leaf = False
        if depth and log is not None:
            log.append(f"=> クリック対象を再特定: {depth} 段下降 → "
                       f"{getattr(node, 'ControlTypeName', '?')}")
        return node, depth, leaf

    def _extract(self, x, y, log):
        # Chromium はアクセシビリティツリーを遅延構築するため、ControlFromPoint が
        # 葉ではなく DocumentControl を返すことがある(実測で 40 回に 1 回程度)。
        # そのまま進むと文書全体を種にしてしまい、ページ先頭の隠しナビゲーション
        # (「キーボードショートカットを表示」等)を読み上げてしまう。取り直せば
        # 解決するので、葉に届くまで数回やり直す。
        for attempt in range(CLICK_TARGET_RETRY):
            control, descended, leaf = self._resolve_click_target(
                auto.ControlFromPoint(x, y), x, y, log)
            if leaf:
                break
            log.append(f"=> クリック対象が葉に届かず(試行{attempt + 1}) → 取り直す")

        # クリック位置から親へ辿り、TextPattern を持つ祖先をすべて集める。
        # あわせて祖先ノード自体も記録し、集約フォールバックの起点に使う。
        # 同時に ARIA ロールを見て「クリックした 1 項目」を包む境界ノードを探す。
        providers = []   # (control, pattern, doc_text)
        ancestors = []   # クリック要素から上へ辿ったノード列
        item = None      # (node, depth, role) 項目境界 (article / listitem 等)
        section = None   # (node, depth, role) 緩い境界 (main)
        node = control
        # 対象の再特定で下へ降りたぶん、上限も伸ばす(文書ノードまで届かせる)
        for depth in range(MAX_WALK_UP + descended):
            if node is None:
                break
            ancestors.append(node)
            name = ""
            try:
                name = node.Name or ""
            except Exception:
                pass
            role = ""
            try:
                role = node.AriaRole or ""
            except Exception:
                role = ""
            if item is None and role in ITEM_ROLES:
                item = (node, depth, role)
            if section is None and role in SECTION_ROLES:
                section = (node, depth, role)
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
                       f"name={name[:30]!r} role={role!r} "
                       f"textPattern={'Y' if pattern else 'N'} docLen={len(doc_text)}")
            try:
                node = node.GetParentControl()
            except Exception:
                break

        # 項目境界を優先。無ければ main、それも無ければ文書末尾まで読む。
        boundary = item or section
        if boundary is None:
            log.append("=> 境界: なし (文書末尾まで)")
        else:
            log.append(f"=> 境界: [{boundary[1]}] role={boundary[2]!r}")

        # 最も多くのテキストを含む TextPattern コンテナ = 通常アプリの本文
        big_pat = None
        big_text = ""
        near_pat = None
        if providers:
            _, big_pat, big_text = max(providers, key=lambda p: len(p[2]))
            near_pat = providers[0][1]  # クリック位置に最も近い要素で位置を特定
        log.append(f"=> providers={len(providers)} bigDocLen={len(big_text)}")

        # 「クリックした要素」が文書全体そのもの = クリック位置の手がかりが何も
        # 無い状態。この状態でだけ、位置を特定できなければ取得失敗として扱う。
        # (葉に届かなかっただけなら、余白のクリック等で従来どおり読める方がよい)
        coarse = bool(not leaf and providers and providers[0][1] is big_pat)
        if coarse:
            log.append("=> 注意: クリック要素が文書全体(位置の手がかり無し)")

        # TextPattern 本文が短すぎる(Electron 系で 1 段落しか取れない)場合は、
        # 祖先配下の TextControl 葉を集約してメッセージ本文を再構成する。
        full_text = big_text
        use_aggregate = False
        anchor = ""
        if len(big_text) < AGG_TRIGGER:
            # 集約の打ち切り判定に使うため、アンカー(クリック行)を先に取っておく
            anchor = self._anchor_line(near_pat, x, y, log) if near_pat else ""
            # 境界が分かっているならそこで祖先辿りを止める(隣の項目を巻き込まない)
            scope = ancestors if boundary is None else ancestors[:boundary[1] + 1]
            agg_text = self._aggregate_ancestors(scope, anchor, log)
            if len(agg_text) > len(full_text):
                full_text = agg_text
                use_aggregate = True

        if not full_text:
            log.append("=> テキスト取得できず")
            return None

        # 大きな TextPattern 文書コンテナ(ブラウザ / VSCode 等)は、クリック位置(行頭)
        # から境界の末尾までを直接切り出すのが最も確実。全文取得+アンカー照合は、文書が
        # max_chars を超えると頭が切れてクリック行を取り込めず、先頭から読み直して
        # しまう(長い会話の途中をクリックすると上から読み始める症状の正体)。
        #
        # ただし Electron 系のように big_pat が「1 段落だけの小さな TextPattern」の場合は
        # この経路に乗せてはいけない。乗せると 1 段落しか返らず読み上げが途中で止まる。
        # その場合は集約した本文の方が正しいので、下のアンカー方式へ回す。
        if big_pat is not None and not use_aggregate:
            tail = self._read_to_boundary(
                big_pat, control, boundary[0] if boundary else None, x, y,
                coarse=coarse, log=log)
            if tail and tail.strip():
                log.append(f"=> クリック位置→境界末尾 成功 長={len(tail)}")
                return tail
            if coarse:
                # 位置の根拠が無いまま全文フォールバックへ進むと、クリックと無関係な
                # ページ先頭から読み上げてしまう。ここで失敗にする。
                log.append("=> クリック対象を特定できないため取得失敗とする")
                return None
            log.append("=> 範囲切り出しできず → アンカー方式へ")

        # アンカー方式: クリックした行を目印に、再構成した本文中の開始位置を求める
        # (TextPattern を持たない Electron 系アプリ向け)
        if not anchor and near_pat is not None:
            anchor = self._anchor_line(near_pat, x, y, log)
        if not anchor and big_pat is not None:
            anchor = self._anchor_line(big_pat, x, y, log)
        if not anchor:
            anchor = self._node_text(control)  # クリックした要素自身のテキスト
        log.append(f"=> anchor={anchor[:40]!r}")

        if anchor:
            idx = _find_anchor(full_text, anchor)
            if idx >= 0:
                log.append(f"=> anchor 一致 idx={idx} 読み上げ長={len(full_text) - idx}")
                return full_text[idx:]
            log.append("=> anchor 本文中に見つからず")

        # フォールバック: 再構成した本文全体を読む
        log.append(f"=> フォールバック 全文読み上げ 長={len(full_text)}")
        return full_text

    # ---- クリック位置の行を特定する(全アプリ共通の要) ----

    def _line_range_at_point(self, pattern, x, y, log=None, seed=None):
        """クリック位置の「行」を表す (TextRange, 矩形で特定できたか) を返す。

        RangeFromPoint + ExpandToEnclosingUnit(Line) は建前上「クリック座標の 1 行」
        を返すが、アプリによっては本文ブロックの先頭やブロック全体が返る。X の
        タイムラインがこれで、本文のどこをクリックしても投稿の先頭から読み上げて
        しまう。本文がリンクやハッシュタグで複数要素に割れている投稿だけ、たまたま
        途中の要素が返って「途中から読める」— 同じような文面でも挙動が分かれる理由。

        どの API がずれるかはアプリ次第なので、返ってきた範囲を信用せず
        「外接矩形が実際にクリック座標を含むか」で検証し、ずれていれば直す:

          1. 矩形がクリック位置を含まない → 行単位で移動して合致する行を探す
          2. 矩形が複数行ぶんある(=範囲が 1 行になっていない)
             → _refine_to_click_line が文字単位でクリック行の先頭まで切り詰める

        矩形は画面座標なので判定は確実で、標準の TextPattern API だけで完結する。
        特定サイト向けの細工にならず、TextPattern を持つどのアプリでも同じに効く。

        探索は指数的に範囲を広げて挟み込み → 二分探索、で行う(先頭から 1 行ずつ
        辿ると長い本文で COM 呼び出しが嵩み、読み上げ開始が遅れるため)。
        """
        # seed は探索の出発点となる範囲。呼び出し側がクリックした要素の範囲を
        # 渡してきたときはそれを使う(RangeFromPoint より確実。理由は
        # _read_to_boundary のコメント参照)。渡されなければ座標から求める。
        if seed is None:
            try:
                seed = pattern.RangeFromPoint(x, y)
            except Exception:
                return None, False
        else:
            try:
                seed = seed.Clone()   # 呼び出し側の範囲を壊さない
            except Exception:
                seed = None
        if seed is None:
            return None, False
        try:
            seed.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
        except Exception:
            pass

        probes = [0]

        def line_at(offset):
            """seed から offset 行移動した行の (range, 位置関係, 実移動行数)。

            位置関係は _range_side と同じ符号(0=クリック行, 他=進むべき向き)。
            """
            probes[0] += 1
            try:
                r = seed.Clone()
            except Exception:
                return None, None, 0
            moved = offset
            if offset:
                try:
                    moved = r.Move(auto.TextUnit.Line, offset, waitTime=0)
                except Exception:
                    return None, None, 0
                try:
                    r.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
                except Exception:
                    pass
            return r, _range_side(r, y), moved

        if log is not None:
            # 診断情報の取得で本処理を巻き添えにしない
            try:
                rects = _rects(seed)
                span = (f"{min(r.top for r in rects)}..{max(r.bottom for r in rects)}"
                        if rects else "なし")
                head = (seed.GetText(24) or "").replace("\n", "/")
                log.append(f"  行候補: clickY={y} 矩形{len(rects)}個 y範囲={span} "
                           f"先頭={head!r}")
            except Exception as e:
                log.append(f"  行候補: 診断取得失敗 {type(e).__name__}")

        side = _range_side(seed, y)
        if side == 0:
            if log is not None:
                log.append("=> 行特定: RangeFromPoint の行がクリック位置と一致")
            return self._refine_to_click_line(seed, y, log), True
        # 矩形が取れない(行が画面外)場合、Chromium 系では seed は要素先頭 =
        # クリック位置より前にあるのが通例なので、前方へ進みながら探す。
        if side is None:
            side = 1

        # 1) クリック位置を跨ぐまで移動幅を倍々にして挟み込む。
        # Move は文書端で丸められる(要求 64 行に対し 59 行しか動かない等)ので、
        # 要求値ではなく実移動量を区間の端として使う。要求値のまま打ち切ると、
        # 文書末尾付近のクリックで挟み込みに失敗し手前の行で確定してしまう。
        near, far = 0, side
        bracketed = False
        while abs(far) <= LINE_SEEK_MAX:
            r, s, moved = line_at(far)
            if r is None:
                break
            if s == 0:
                if log is not None:
                    log.append(f"=> 行特定: {moved} 行移動で一致 (探索{probes[0]}回)")
                return self._refine_to_click_line(r, y, log), True
            if s is not None and s != side:
                far = moved        # 跨いだ = [near, far] の間にクリック行がある
                bracketed = True
                break
            near = moved
            if moved != far:
                break              # 文書端に達した = これ以上その向きへは動けない
            far *= 2

        # 2) 挟み込めた区間を二分してクリック行に寄せる
        if bracketed:
            while abs(far - near) > 1:
                mid = near + (far - near) // 2
                r, s, moved = line_at(mid)
                if r is None or s is None or moved != mid:
                    break
                if s == 0:
                    if log is not None:
                        log.append(f"=> 行特定: {mid} 行移動で一致 (探索{probes[0]}回)")
                    return self._refine_to_click_line(r, y, log), True
                if s == side:
                    near = mid
                else:
                    far = mid

        # どの行の矩形にも入らない(行間の余白やパディングをクリックした)場合は、
        # 文書順でクリック位置の直前にある行を採る。読み始めが手前にずれるだけで、
        # クリックした箇所を読み飛ばすことがない。
        final = near if side > 0 else (far if bracketed else near)
        r, _, _ = line_at(final)
        if log is not None:
            log.append(f"=> 行特定: {final} 行移動で確定(矩形一致なし 探索{probes[0]}回)")
        return self._refine_to_click_line(r or seed, y, log), False

    def _refine_to_click_line(self, text_range, y, log=None):
        """複数行にまたがる範囲を、クリック行の先頭から始まる範囲に切り詰める。

        ExpandToEnclosingUnit(Line) は「1 行」に正規化してくれる建前だが、実装に
        よっては段落やブロック全体を返す。その場合、外接矩形はブロック全体を覆う
        ため「クリック位置を含む」判定になってしまい、矩形照合だけでは是正できない
        (どこをクリックしてもブロック先頭から読み上げてしまう)。

        そこで、矩形が複数行ぶん返ってきた=範囲が複数行にまたがっていると判断し、
        Line 単位に頼らず文字単位の二分探索でクリック行の先頭文字を割り出す。
        矩形は画面座標なので、行の概念がアプリごとにどうであれ結果は同じになる。
        """
        rects = _rects(text_range)
        if len(rects) <= 1:
            return text_range          # すでに 1 行 → そのまま
        try:
            n = len(text_range.GetText(self._max_chars) or "")
        except Exception:
            return text_range
        if n <= 1:
            return text_range

        # 先頭文字がすでにクリック行以降なら切り詰める必要はない
        if self._char_side(text_range, 0, y) != 1:
            return text_range

        # 「クリック位置より上」でなくなる最初の文字位置を二分探索で求める。
        # lo は常にクリック行より上、hi は常にクリック行以降、という不変条件を保つ。
        lo, hi = 0, n
        while hi - lo > 1:
            mid = (lo + hi) // 2
            s = self._char_side(text_range, mid, y)
            if s is None:
                break
            if s == 1:
                lo = mid
            else:
                hi = mid

        r = self._char_start_range(text_range, hi)
        if r is None:
            return text_range
        if log is not None:
            log.append(f"=> 行特定: 範囲が{len(rects)}行ぶん → {hi}/{n} 文字目まで切り詰め")
        return r

    def _char_start_range(self, base, k):
        """base の開始位置を k 文字進めた範囲を返す(終端は base のまま)。"""
        try:
            r = base.Clone()
            if k and r.MoveEndpointByUnit(Start, auto.TextUnit.Character, k,
                                          waitTime=0) != k:
                return None
            return r
        except Exception:
            return None

    def _char_side(self, base, k, y):
        """base の k 文字目とクリック位置の上下関係(_range_side と同じ符号)。"""
        r = self._char_start_range(base, k)
        if r is None:
            return None
        try:
            # 1 文字ぶんの範囲にして、その文字の矩形を得る
            r.MoveEndpointByRange(End, r, Start, waitTime=0)
            r.MoveEndpointByUnit(End, auto.TextUnit.Character, 1, waitTime=0)
        except Exception:
            return None
        return _range_side(r, y)

    def _anchor_line(self, pattern, x, y, log=None):
        """クリック位置の1行分のテキストを返す(位置特定の目印)。"""
        r, _ = self._line_range_at_point(pattern, x, y, log)
        if r is None:
            return ""
        try:
            text = r.GetText(ANCHOR_MAX) or ""
        except Exception:
            return ""
        # 範囲がクリック行以降すべてに及ぶことがあるので、先頭の 1 行だけを目印にする
        for line in text.split("\n"):
            if line.strip():
                return line.strip()
        return ""

    # ---- 子孫走査による本文再構成(Electron/Chromium 系向け) ----

    def _aggregate_ancestors(self, ancestors, anchor, log):
        """祖先を下から上へ辿り、クリックしたメッセージ 1 件分の本文を返す。

        祖先の部分木は親へ行くほどテキスト量が増える。1 メッセージのうちは
        MESSAGE_MAX に収まり、会話ペイン(=複数メッセージ)に達すると一気に
        超える。そこで「MESSAGE_MAX を超えない範囲で最大の集約」を採用する。
        これは MESSAGE_MAX 以内に収まる最も外側の祖先(=メッセージ全体を包む
        コンテナ)の全文にあたる。

        高速化のため、1 段下の祖先の走査結果を RuntimeId 照合でキャッシュ流用する
        (旧実装は段ごとに部分木全体を再走査していて、これが取得に数秒かかる主因)。
        さらに、アンカー(クリック行)より後ろ=実際に読み上げる部分が
        AGG_STALL_LEVELS 段続けて増えなくなったら、それ以上親を辿っても読み上げ
        内容は変わらないので打ち切る。これはウィンドウ外周の UI 文言(入力欄の
        プレースホルダ等)が読み上げ末尾に混ざるのを防ぐ効果もある。
        """
        best = ""
        best_tail = 0    # アンカー以降の長さ = 実際に読み上げる部分の長さ
        stalls = 0
        prev_id = None   # 1 段下の祖先の RuntimeId(キャッシュ照合キー)
        prev_cache = None  # その部分木の走査結果 (parts, nodes)
        for depth, node in enumerate(ancestors):
            parts, nodes = self._collect_descendant_text(
                node, MESSAGE_MAX, prev_id, prev_cache)
            agg = "\n".join(parts)
            log.append(f"  集約[{depth}] len={len(agg)} nodes={nodes}")
            if len(agg) > MESSAGE_MAX:
                # 1 メッセージの上限を超えた = 会話ペイン(複数メッセージ)へ到達。
                # 直前の best(メッセージ 1 件分)を採用して打ち切る。
                log.append(f"=> 集約 上限超過(会話ペイン到達)→ bestLen={len(best)} で確定")
                break
            if len(agg) > len(best):
                best = agg
            try:
                prev_id = node.GetRuntimeId()
            except Exception:
                prev_id = None
            prev_cache = (parts, nodes)
            if anchor and len(best) >= AGG_TRIGGER:
                idx = _find_anchor(best, anchor)
                if idx >= 0:
                    tail = len(best) - idx
                    if tail - best_tail < AGG_STALL_MIN:
                        stalls += 1
                        if stalls >= AGG_STALL_LEVELS:
                            log.append(f"=> 集約 読み上げ部分が増えないため {depth} 段で打ち切り")
                            break
                    else:
                        stalls = 0
                    best_tail = max(best_tail, tail)
        log.append(f"=> 集約 bestLen={len(best)}")
        return best

    def _collect_descendant_text(self, root, char_limit=BLOCK_MAX,
                                 prev_id=None, prev_cache=None):
        """root 配下の TextControl 葉のテキストを文書順(前順走査)で集める。

        char_limit を超えたら走査を打ち切る。呼び出し側が小さい上限を渡すことで、
        会話ペイン全体のような巨大な部分木を最後まで走査せず高速に判定できる。

        prev_id / prev_cache には 1 段下の祖先(root の直接の子のはず)の
        RuntimeId と走査結果を渡す。一致した子は再走査せず結果を流用するので、
        祖先を何段辿っても各ノードの走査は全体で 1 回で済む。照合の COM 呼び出しは
        root の直接の子(depth==1)に限定してコストを抑える。

        戻り値は (テキスト片のリスト, 走査ノード数)。
        """
        parts = []
        state = {"nodes": 0, "chars": 0}
        limit = char_limit + ANCHOR_MAX  # 打ち切り判定用の余裕を持たせた上限

        def walk(node, depth):
            if node is None or depth > MAX_DESCEND:
                return
            if state["nodes"] >= MAX_NODES or state["chars"] > limit:
                return
            if depth == 1 and prev_id is not None:
                try:
                    if node.GetRuntimeId() == prev_id:
                        cached_parts, cached_nodes = prev_cache
                        parts.extend(cached_parts)
                        state["nodes"] += cached_nodes
                        state["chars"] += sum(len(p) + 1 for p in cached_parts)
                        return
                except Exception:
                    pass
            state["nodes"] += 1
            try:
                children = node.GetChildren()
            except Exception:
                children = []
            # 子を持たない TextControl = テキストの実体(行/ラン)。ここだけ採る。
            # ControlTypeName も COM 呼び出しなので、葉と分かってから確認する。
            if not children:
                try:
                    ctype = node.ControlTypeName
                except Exception:
                    ctype = ""
                if ctype == "TextControl":
                    text = self._node_text(node)
                    if text:
                        parts.append(text)
                        state["chars"] += len(text) + 1
                return
            for child in children:
                if state["nodes"] >= MAX_NODES or state["chars"] > limit:
                    break
                walk(child, depth + 1)

        walk(root, 0)
        return parts, state["nodes"]

    @staticmethod
    def _node_text(node):
        """ノードの表示テキスト。Chromium 系では Name にランのテキストが入る。"""
        try:
            return (node.Name or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _range_from_child(pattern, child):
        """pattern の文書内で child 要素が占める TextRange を返す。

        座標に頼らず要素そのものから範囲を得るので、位置ずれの影響を受けない。
        (uiautomation の TextPattern.RangeFromChild は引数を無視して呼ぶ不具合が
        あるため、生の COM インターフェースを直接呼ぶ。)
        """
        if pattern is None or child is None:
            return None
        for _ in range(RANGE_RETRY):
            try:
                raw = pattern.pattern.RangeFromChild(child.Element)
            except Exception:
                raw = None
            if not raw:
                continue
            try:
                return auto.TextRange(textRange=raw)
            except Exception:
                return None
        return None

    def _read_to_boundary(self, pattern, control, boundary, x, y,
                          coarse=False, log=None):
        """クリック位置(行頭にそろえる)から、クリックした項目の末尾までを返す。

        開始位置: RangeFromPoint(座標→文字位置)は、X のタイムラインのように
        セルを絶対配置で仮想スクロールする画面では別の投稿を指すことがある
        (クリックした投稿と無関係な行から読み始める症状の正体)。そこで
        ControlFromPoint で得た「実際にクリックされた要素」の範囲を探索の出発点に
        使い、そこから矩形照合でクリック行へ寄せる。座標ヒットテストは、要素の
        範囲が取れなかったときのフォールバックに下げる。

        終了位置: boundary(role=article / listitem 等)があればその末尾で止める。
        タイムラインは全体で 1 つの巨大な文書なので、文書末尾まで読むと後続の投稿や
        ページのフッターまで読み上げてしまう。boundary が無ければ従来どおり文書末尾
        まで読む。
        """
        # 要素の範囲を種にする。矩形が一時的に取れないことがある(ツリー構築中)ので、
        # その場合だけ取り直す。矩形が取れていて位置が違うなら、それは本当に別の
        # 場所なので取り直さず座標探索へ回す。
        seed = None
        for attempt in range(RANGE_RETRY):
            cand = self._range_from_child(pattern, control)
            if cand is None:
                continue
            side = _range_side(cand, y)
            if side == 0:
                seed = cand
                break
            if side is None:
                if log is not None:
                    log.append(f"  開始種: 要素範囲の矩形が取れず(試行{attempt + 1})")
                continue
            if log is not None:
                log.append("  開始種: 要素範囲がクリック位置を含まず → 座標から探索")
            break
        if log is not None:
            log.append("  開始種: クリックした要素の範囲を使用" if seed is not None
                       else "  開始種: 座標から探索")
        r, matched = self._line_range_at_point(pattern, x, y, log, seed=seed)
        if r is None:
            return ""
        # 粗いノード(文書全体)を種にしたうえ矩形でも行を特定できていないなら、
        # 読み始めの位置に根拠が無い。そのまま読むとページ先頭の隠しナビから
        # 読み上げてしまうので、取得失敗として扱う(橙の点灯で知らせる)。
        if coarse and not matched:
            if log is not None:
                log.append("=> クリック位置を特定できず(粗いノード+矩形不一致)→ 取得失敗")
            return ""

        end_range = self._range_from_child(pattern, boundary)
        if end_range is not None:
            try:
                clipped = r.Clone()
                if clipped.MoveEndpointByRange(End, end_range, End, waitTime=0):
                    text = clipped.GetText(self._max_chars) or ""
                    if text.strip():
                        if log is not None:
                            log.append(f"  終端: 項目境界まで 長={len(text)}")
                        return text
            except Exception:
                pass
            if log is not None:
                log.append("  終端: 項目境界を適用できず → 文書末尾まで")
        try:
            if not r.MoveEndpointByRange(End, pattern.DocumentRange, End, waitTime=0):
                return ""
            return r.GetText(self._max_chars) or ""
        except Exception:
            return ""

    def _write_debug(self, x, y, text, log):
        if not self._debug:
            return
        stamp = time.strftime("%H:%M:%S")
        entry = [f"{DEBUG_SEP}\n[{stamp}] click=({x},{y})",
                 "\n".join(log),
                 "\n--- 取得テキスト先頭200字 ---",
                 (text or "")[:200],
                 "--- 末尾100字 ---",
                 (text or "")[-100:], ""]
        try:
            try:
                with open(DEBUG_LOG, "r", encoding="utf-8") as f:
                    past = f.read()
            except OSError:
                past = ""
            # 直近 DEBUG_KEEP 回分だけ残す(区切りで切り出して古い方を捨てる)
            chunks = past.split(DEBUG_SEP)[1:]
            chunks = chunks[-(DEBUG_KEEP - 1):] if DEBUG_KEEP > 1 else []
            with open(DEBUG_LOG, "w", encoding="utf-8") as f:
                for c in chunks:
                    f.write(DEBUG_SEP + c)
                f.write("\n".join(entry))
        except OSError:
            pass


def _rects(text_range):
    """範囲の外接矩形(画面座標)。取得できなければ空リスト。"""
    try:
        return text_range.GetBoundingRectangles() or []
    except Exception:
        return []


def _range_side(text_range, y):
    """行の外接矩形とクリック位置の上下関係を返す。

    0  … 行がクリック位置を含む(=クリックした行)
    1  … 行はクリック位置より上(前方へ進む必要がある)
    -1 … 行はクリック位置より下(後方へ戻る必要がある)
    None … 矩形が取れない(行が画面外など)ため判定できない

    戻り値はそのまま「移動すべき向き」として使える符号になっている。
    """
    rects = _rects(text_range)
    if not rects:
        return None
    top = min(r.top for r in rects)
    bottom = max(r.bottom for r in rects)
    if bottom <= top:
        return None
    if y < top:
        return -1
    if y >= bottom:
        return 1
    return 0


def _has_letter(text):
    """文字(言語は問わない)を 1 つでも含むか。

    数字・記号・￼(埋め込みオブジェクトの代替文字)だけの塊は、読み上げても
    意味を成さない UI の断片。本文の行は必ず文字を含むので、これで見分ける。
    """
    return any(ch.isalpha() for ch in text)


# 件数表示だけの行(1,946 / 3.5万 / 1.2K など)。単位の万・千・億・K/M/B は
# 文字なので _has_letter では弾けず、これが無いと「3.5万」で削除が止まってしまう。
_COUNT_LINE = re.compile("^[\\s\\d.,%\uFFFC\u4e07\u5343\u5104\u5146KMBkmb]+$")


def _is_ui_noise(line):
    """本文ではない行(カウンタ表示や記号だけの行)か。"""
    if not _has_letter(line):
        return True                        # 数字・記号・￼ だけの行
    return bool(_COUNT_LINE.match(line))   # 3.5万 / 1.2K のような件数表示


def _trim_trailing_ui(text):
    """末尾に続く UI の断片(返信 / リポスト / いいね / 表示回数)を落とす。

    X の投稿は本文のあとにこれらの数字が並び、そのまま読み上げると
    「12、133、663、3.5万」と数字の羅列になる。本文の行は必ず文字を含み、かつ
    件数表示の形にはならないので、末尾から _is_ui_noise が続く間だけ削れば本文は
    削られない。全部消えてしまう場合(数字だけの表など)は元のまま返す。
    """
    if not text:
        return text
    lines = text.split("\n")
    i = len(lines)
    while i > 0 and _is_ui_noise(lines[i - 1]):
        i -= 1
    if i == len(lines):
        return text
    trimmed = "\n".join(lines[:i]).rstrip()
    return trimmed if trimmed.strip() else text


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
