"""UI Automation でクリック位置から「クリックした項目の終わり」までを取得する。

読み始めはクリックした「文字」。行を矩形で特定したあと、行内の文字の外接矩形を
二分探索してクリック X の位置まで開始点を詰める (_refine_to_click_char)。座標→
文字位置を RangeFromPoint に尋ねると Chromium は要素の先頭を返すので、判定は
すべて画面座標の矩形だけで行う。

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
ANCHOR_MIN = 8        # アンカーとして誤一致しない最低の文字数

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
AGG_TIME_BUDGET = 2.5  # 1 回の集約にかけてよい秒数。ブラウザのタブ列など本文の無い UI を
                       # クリックすると、ウィンドウ全体(数千ノード)を走査したうえで
                       # 結局取得失敗になり、失敗の通知まで数秒かかっていた(実測 1.7〜7 秒)。
                       # 本文が取れる通常の経路は 0.2 秒以内なので、超えたら諦めて確定する。

# RangeFromPoint の位置ずれを矩形照合で補正する際、クリック行を探して行単位で
# 移動できる最大行数。Chromium 系では要素の先頭が返るため、要素が長いほど遠くなる。
LINE_SEEK_MAX = 512
# 文字の矩形が取れないとき、二分探索が隣の文字から判定を補うために見る距離(文字数)。
BISECT_NEIGHBOR = 3
# これ以下の幅の文字矩形は、実体のある文字の幅ではなく退化矩形とみなして捨てる。
DEGENERATE_WIDTH = 1

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
CLICK_TARGET_RETRY_DELAY = 0.04  # 遅延構築中のツリーが安定するのを待つ秒数
# 項目ロールが無いページ向けの緩い境界。ヘッダ / ナビ / フッターを除ける。
SECTION_ROLES = ("main",)

DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_debug.log")
DEBUG_KEEP = 40       # ログに残す直近の取得回数。1 回分だけだと「さっきの失敗」を
                      # 追えないため履歴にする(区切りは DEBUG_SEP)。たまにしか
                      # 起きない不具合は、気づいてから数回試すうちに流れてしまうので
                      # 多めに残す。
DEBUG_SEP = "=" * 60

LABEL_MAX = 8            # ラベル行とみなす最大文字数(空白を除く)
REACTION_MIN_COUNTS = 2  # 反応ラベルの塊とみなすのに必要な「件数だけの行」の数

# 縦に並ぶリスト(X のタイムライン等)で、古い矩形を報告している項目を見抜く条件。
TILE_MIN_ITEMS = 5     # これ未満の兄弟では並びを判断しない(投稿の内側の小さな
                       # 縦積みを、項目のリストと取り違えないため)
TILE_TOLERANCE = 3     # 並びどおりとみなす誤差 (px)
TILE_MAJORITY = 0.6    # 兄弟のうちこの割合が並びどおりなら、縦のリストとみなす
TILE_WALK_UP = 16      # リストを探して親を辿る最大段数
TILE_STALE_MIN = 24    # 古い矩形とみなす最小のずれ (px)。これより小さいずれは
                       # 項目間の余白(Claude の最後のメッセージだけ 7px 空く等)で、
                       # 矩形は正しい。補正すると余白へ外れて取得失敗になっていた。
                       # 本当に古い矩形は投稿 1 件ぶん以上ずれる(実測 463px)。
PAD_NEAR = 32          # クリックした要素の文字がクリック位置からこの距離 (px) 以内なら、
                       # 要素の余白(表のセルの下側など)を押したとみなす

TREE_SCOPE_DESCENDANTS = 4 # UIA の TreeScope_Descendants (uiautomation は定数を公開していない)

Start = auto.TextPatternRangeEndpoint.Start
End = auto.TextPatternRangeEndpoint.End


class _AggTimeout(Exception):
    """子孫集約が AGG_TIME_BUDGET を使い切った。"""


class TextCapturer(threading.Thread):
    def __init__(self, on_text, on_error, max_chars=30000, debug=True):
        super().__init__(daemon=True, name="capture")
        self._q = queue.Queue()
        self._on_text = on_text
        self._on_error = on_error
        self._max_chars = max_chars
        self._debug = debug
        # 取得は UI Automation の都合で直列実行するが、要求には世代番号を付ける。
        # これにより取得中の再クリックや停止後に、古い結果が遅れて読み上げられる
        # 競合を防ぐ。callback もこの lock 内で呼び、cancel() 後に古い speak が
        # キューへ入る隙間を作らない。
        self._state_lock = threading.Lock()
        self._generation = 0

    def request(self, x, y):
        with self._state_lock:
            self._generation += 1
            generation = self._generation
        self._q.put((generation, x, y))

    def cancel(self):
        """処理待ち・処理中の取得結果を無効化する。"""
        with self._state_lock:
            self._generation += 1
            try:
                while True:
                    self._q.get_nowait()
            except queue.Empty:
                pass

    def run(self):
        with auto.UIAutomationInitializerInThread():
            while True:
                generation, x, y = self._q.get()
                # 連打された場合は最後のクリックだけ処理する
                try:
                    while True:
                        generation, x, y = self._q.get_nowait()
                except queue.Empty:
                    pass
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
                with self._state_lock:
                    if generation != self._generation:
                        lines.append("=> 新しい要求または停止により結果を破棄")
                    else:
                        # 画像やアイコンをクリックすると ￼ や数字だけが返る。
                        # 読み上げても無音になるため取得失敗として知らせる。
                        # 「133 663 3.5万」のような件数の行だけの場合も同じ。
                        try:
                            if text and _has_content(text):
                                self._on_text(text)
                            else:
                                self._on_error()
                        except Exception as e:
                            # 通知音など周辺処理の一時的な失敗で、以後すべての
                            # キャプチャを担う常駐スレッドまで終了させない。
                            lines.append(f"CALLBACK EXCEPTION: {type(e).__name__}: {e}")
                self._write_debug(x, y, text, lines)

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

    def _search_by_rect(self, root, x, y, log=None):
        """root 配下の全要素から、クリック座標を含む最小の要素を探して返す。

        Chromium は座標→要素の問い合わせに、過去の結果のキャッシュと矩形での
        近似で即答する。そのため本文の上をクリックしても文書全体が返ることがあり、
        取り直しても直らないことがある(X で同じ投稿が 4 回続けて失敗した)。
        _resolve_click_target で降り直そうにも、X は文書直下の入れ物がどれも
        クリック位置を矩形に含まない(子が親の矩形からはみ出している)ので、
        1 段も降りられない。親の矩形を当てにせず、全要素の矩形を直接調べる。

        要素ごとに矩形を尋ねると数千回の COM 呼び出しになるので、キャッシュ要求で
        矩形ごと一括取得する。見つからなければ None。
        """
        started = time.perf_counter()
        try:
            client = auto.uiautomation._AutomationClient.instance().IUIAutomation
            request = client.CreateCacheRequest()
            request.AddProperty(auto.PropertyId.BoundingRectangleProperty)
            found = root.Element.FindAllBuildCache(
                TREE_SCOPE_DESCENDANTS, client.CreateTrueCondition(), request)
            count = found.Length
            rects = []
            for i in range(count):
                r = found.GetElement(i).CachedBoundingRectangle
                rects.append((r.left, r.top, r.right, r.bottom))
            best = _smallest_containing(rects, x, y)
            node = (auto.Control.CreateControlFromElement(found.GetElement(best))
                    if best is not None else None)
        except Exception as e:
            if log is not None:
                log.append(f"=> 矩形検索失敗: {type(e).__name__}")
            return None
        if log is not None:
            elapsed = (time.perf_counter() - started) * 1000
            log.append(f"=> 矩形検索: {count} 要素 {elapsed:.0f}ms → "
                       + (getattr(node, 'ControlTypeName', '?') if node is not None
                          else "該当なし"))
        return node

    def _retarget_stale(self, control, x, y, log=None):
        """Chromium が古い位置を報告している項目を見抜き、クリック対象を選び直す。

        X のタイムラインでは、上の投稿の高さが後から変わる(動画や画像の読み込み)
        と下の投稿が押し下げられるが、Chromium が一部の投稿の矩形を更新し損ねる
        ことがある(実測で 1 件だけ 463px 古いまま)。古い矩形をクリック座標と
        突き合わせると、当たらずに取得失敗になるか、隣の投稿や隣の行に当たる。

        リストの項目は縦に隙間なく並ぶので、兄弟の高さを積み上げれば各項目の
        本来の位置が分かる(_tiling_slots)。クリック座標を本来の位置で受けた項目を
        対象にし、その項目の矩形がずれているぶん y を補正して返す。項目の中身は
        丸ごと同じだけずれているので、補正した y なら行や文字の矩形とも合う。

        戻り値は (対象ノード, 葉に到達したか, 補正後の y)。補正が要らなければ None。
        """
        try:
            if control is None or control.FrameworkId != "Chrome":
                return None
        except Exception:
            return None
        node = control
        below = None   # node の子のうち、クリック対象へ続く側の RuntimeId
        for _ in range(TILE_WALK_UP):
            if node is None or _is_document(node):
                return None
            try:
                children = node.GetChildren()
            except Exception:
                return None
            fixed = self._retarget_in_list(children, below, x, y, log)
            if fixed is not None:
                return fixed
            # この段は並びどおり(またはリストではない)。項目ごと古い位置に
            # ある場合、項目の内側はつじつまが合って見えるので、外側も調べる。
            try:
                below = node.GetRuntimeId()
                node = node.GetParentControl()
            except Exception:
                return None
        return None

    def _retarget_in_list(self, children, below, x, y, log=None):
        """children が縦のリストで、クリックを受けるべき項目の矩形が古ければ選び直す。"""
        if len(children) < TILE_MIN_ITEMS:
            return None
        rects = []
        for child in children:
            try:
                r = child.BoundingRectangle
                rects.append((r.left, r.top, r.right, r.bottom))
            except Exception:
                rects.append((0, 0, 0, 0))
        slots = _tiling_slots(rects)
        if slots is None:
            return None
        target = next((i for i, (top, bottom, _) in slots.items()
                       if top <= y < bottom), None)
        if target is None:
            return None
        shift = slots[target][2]
        if abs(shift) < TILE_STALE_MIN:
            return None   # 余白による小さな食い違い。矩形は信用できる
        on_path = None
        if below is not None:
            for i, child in enumerate(children):
                try:
                    if child.GetRuntimeId() == below:
                        on_path = i
                        break
                except Exception:
                    pass
        if abs(shift) <= TILE_TOLERANCE and (below is None or on_path == target):
            return None   # 並びどおり。矩形は信用できる
        shifted_y = y + shift
        found, depth, leaf = self._resolve_click_target(children[target], x, shifted_y)
        if not leaf and not depth:
            deeper = self._search_by_rect(children[target], x, shifted_y, log)
            if deeper is not None:
                found, _, leaf = self._resolve_click_target(deeper, x, shifted_y)
        if log is not None:
            log.append(f"=> 古い矩形を補正: 項目[{target}] が {shift:+d}px ずれ → "
                       f"y={y}→{shifted_y} {getattr(found, 'ControlTypeName', '?')}")
        return found, leaf, shifted_y

    def _extract(self, x, y, log):
        # Chromium はアクセシビリティツリーを遅延構築するため、ControlFromPoint が
        # 葉ではなく DocumentControl を返すことがある(実測で 40 回に 1 回程度)。
        # そのまま進むと文書全体を種にしてしまい、ページ先頭の隠しナビゲーション
        # (「キーボードショートカットを表示」等)を読み上げてしまう。取り直せば
        # 解決するので、葉に届くまで数回やり直す。
        control = None
        descended = 0
        leaf = False
        settled = False   # 全要素の矩形検索でクリック対象を確定できたか
        for attempt in range(CLICK_TARGET_RETRY):
            try:
                hit = auto.ControlFromPoint(x, y)
                control, descended, leaf = self._resolve_click_target(
                    hit, x, y, log)
                if not leaf and _is_document(control):
                    found = self._search_by_rect(control, x, y, log)
                    if found is not None:
                        control, _, leaf = self._resolve_click_target(
                            found, x, y, log)
                        # 全要素を調べた結果なので、葉でなくても(画像の脇や行間の
                        # クリック)これ以上は絞れない。ここで取り直すと Chromium が
                        # また文書全体を返し、見つけた要素を上書きして失ってしまう。
                        settled = True
            except Exception as e:
                log.append(f"=> クリック対象取得失敗(試行{attempt + 1}): "
                           f"{type(e).__name__}")
            if leaf or settled:
                break
            log.append(f"=> クリック対象が葉に届かず(試行{attempt + 1}) → 取り直す")
            if attempt + 1 < CLICK_TARGET_RETRY:
                time.sleep(CLICK_TARGET_RETRY_DELAY)

        fixed = self._retarget_stale(control, x, y, log)
        if fixed is not None:
            # 以降の矩形照合(行・文字の特定)も、同じ古い座標系の上で行う
            control, leaf, y = fixed

        control_anchor = self._node_text(control)

        # クリック位置から親へ辿り、TextPattern を持つ祖先をすべて集める。
        # あわせて祖先ノード自体も記録し、集約フォールバックの起点に使う。
        # 同時に ARIA ロールを見て「クリックした 1 項目」を包む境界ノードを探す。
        providers = []   # (control, pattern, doc_text)
        ancestors = []   # クリック要素から上へ辿ったノード列
        item = None      # (node, depth, role) 項目境界 (article / listitem 等)
        section = None   # (node, depth, role) 緩い境界 (main)
        doc_depth = None  # TextPattern を持つ最初の DocumentControl の段数
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
                if doc_depth is None and getattr(node, "ControlTypeName", "") == "DocumentControl":
                    doc_depth = depth
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
        # 集約で祖先を辿る範囲。境界が分かっていればそこで止める(隣の項目を巻き込まない)
        agg_scope = ancestors if boundary is None else ancestors[:boundary[1] + 1]
        # TextPattern を持つ DocumentControl (ブラウザのページ等) は、配下の本文を
        # すべて含む。その外側 (アドレスバーや「分割画面で開く」のようなウィンドウ
        # の UI) まで集約を広げても本文は増えず、無関係な UI 文言を拾ううえ、
        # ウィンドウ全体の走査で読み上げ開始が数秒遅れる。文書の手前で止める。
        if doc_depth is not None:
            agg_scope = agg_scope[:doc_depth + 1]

        # 最も多くのテキストを含む TextPattern コンテナ = 通常アプリの本文
        big_pat = None
        big_text = ""
        near_pat = None
        if providers:
            _, big_pat, big_text = max(providers, key=lambda p: len(p[2]))
            near_pat = providers[0][1]  # クリック位置に最も近い要素で位置を特定
        log.append(f"=> providers={len(providers)} bigDocLen={len(big_text)}")

        # 葉にも TextPattern にも到達せず、名前や ARIA 境界さえ無い場合は、
        # ツリー構築中の粗い Pane/Document を拾った状態。ここから子孫を集約すると
        # タスクバー等の無関係な UI を本文として返すため、誤読より取得失敗を選ぶ。
        if (not leaf and not providers and boundary is None
                and not _is_strong_anchor(control_anchor)):
            log.append("=> クリック対象の根拠が無いため取得失敗とする")
            return None

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
        line_anchor = ""   # クリックした文字から始まる目印。アンカー方式でも使い回す
        if len(big_text) < AGG_TRIGGER:
            # 集約の打ち切り判定に使うため、アンカー(クリック行)を先に取っておく
            if _is_strong_anchor(control_anchor):
                agg_anchor = control_anchor
            elif near_pat is not None:
                line_anchor = agg_anchor = self._anchor_line(near_pat, x, y, log)
            else:
                agg_anchor = ""
            agg_text = self._aggregate_ancestors(agg_scope, agg_anchor, log)
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
        # 項目境界の範囲は範囲切り出しとアンカー方式の両方が使うので一度だけ求める
        boundary_range = None
        if boundary is not None and big_pat is not None and not use_aggregate:
            boundary_range = self._range_from_child(big_pat, boundary[0])

        if big_pat is not None and not use_aggregate:
            tail = self._read_to_boundary(
                big_pat, control, boundary_range, x, y,
                coarse=coarse, expected_anchor=control_anchor, log=log)
            if tail and tail.strip():
                log.append(f"=> クリック位置→境界末尾 成功 長={len(tail)}")
                return tail
            if coarse:
                # 位置の根拠が無いまま全文フォールバックへ進むと、クリックと無関係な
                # ページ先頭から読み上げてしまう。ここで失敗にする。
                log.append("=> クリック対象を特定できないため取得失敗とする")
                return None
            log.append("=> 範囲切り出しできず → アンカー方式へ")

        # アンカー方式: クリックした位置のテキストを目印に、本文中の開始位置を探す
        # (TextPattern を持たない Electron 系アプリ / 範囲切り出しが効かない場合)
        if not line_anchor and near_pat is not None:
            line_anchor = self._anchor_line(near_pat, x, y, log)
        if not line_anchor and big_pat is not None:
            line_anchor = self._anchor_line(big_pat, x, y, log)

        # 行アンカーはクリックした文字から始まるので、こちらを先に試す。要素の
        # Name (control_anchor) は必ず要素の先頭から始まるため、先に使うと段落の
        # 頭まで読み始めが巻き戻ってしまう。
        candidates = []
        if _is_strong_anchor(line_anchor):
            candidates.append(("行", line_anchor))
        if _is_strong_anchor(control_anchor):
            candidates.append(("要素", control_anchor))
        if not candidates:
            weak = line_anchor or self._node_text(control)
            if weak:
                candidates.append(("弱", weak))
        log.append("=> anchor候補=" + (", ".join(
            f"{kind}:{text[:24]!r}" for kind, text in candidates) or "なし"))

        # 項目境界 (role=article 等) の範囲だけを本文として扱う。ここで文書全体を
        # 使うと、コメントを 1 件クリックしたのに後続のコメントやページ末尾の
        # フッターまで読み続けてしまう(同じ画面で取得長が 118 文字と 8000 文字に
        # 揺れる症状の正体)。範囲切り出し経路が boundary で止めているのと揃える。
        item_text = ""
        if boundary_range is not None:
            try:
                item_text = boundary_range.GetText(self._max_chars) or ""
            except Exception:
                item_text = ""
            log.append(f"=> 境界テキスト長={len(item_text)}")

        def first_hit(body, body_name):
            """本文中でクリック位置を裏付けられる最初の候補〜末尾を返す。"""
            # クリック要素の位置は候補によらず同じなので、本文ごとに一度だけ求める
            base = (_find_anchor(body, control_anchor)
                    if _is_strong_anchor(control_anchor) else -1)
            for kind, cand in candidates:
                idx = _find_anchor(body, cand)
                if idx < 0:
                    continue
                if not _anchor_consistent(idx, base, control_anchor):
                    log.append(f"=> anchor({kind}) がクリック要素の位置と"
                               f"食い違う → 次の候補へ")
                    continue
                log.append(f"=> anchor 一致({body_name}/{kind}) idx={idx} "
                           f"読み上げ長={len(body) - idx}")
                return body[idx:]
            return None

        for body_name, body in (("境界内", item_text), ("全文", full_text)):
            hit = first_hit(body, body_name) if body else None
            if hit is not None:
                return hit

        log.append("=> anchor 本文中に見つからず")
        if candidates and not use_aggregate:
            # 表示テキストと文書テキストが食い違うことがある(Name が aria-label で
            # 置き換わっている、遅延読み込みで文書側が古い等)。橙点灯で諦める前に、
            # 境界配下の TextControl 葉を集めた本文でもう一度だけ照合する。
            agg = self._aggregate_ancestors(agg_scope, candidates[0][1], log)
            hit = first_hit(agg, "集約") if agg else None
            if hit is not None:
                return hit
        if _is_strong_anchor(control_anchor):
            log.append("=> クリック位置を裏付けられないため取得失敗とする")
            return None

        # フォールバック: 分かっている中で最も狭い本文を読む
        fallback = item_text or full_text
        log.append(f"=> フォールバック 全文読み上げ 長={len(fallback)}")
        return fallback

    # ---- クリック位置の行を特定する(全アプリ共通の要) ----

    def _start_range_at_point(self, pattern, x, y, log=None, seed=None):
        """クリックした文字から始まる (TextRange, 矩形で特定できたか) を返す。

        まずクリック「行」を矩形で突き止め、そのあと行内の文字の矩形を二分探索
        してクリック X の位置まで開始点を詰める (start_at)。

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

        def start_at(rng):
            """行を確定したあと、クリックした「文字」まで開始位置を詰める。"""
            return self._refine_to_click_char(
                self._refine_to_click_line(rng, y, log), x, y, log)

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
            return start_at(seed), True
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
        far_measured = True   # far が矩形で検証できた行か(測れない行は上限にしか使わない)
        while abs(far) <= LINE_SEEK_MAX:
            r, s, moved = line_at(far)
            if r is None:
                break
            if s == 0:
                if log is not None:
                    log.append(f"=> 行特定: {moved} 行移動で一致 (探索{probes[0]}回)")
                return start_at(r), True
            if s is None:
                # 矩形が取れない行(画面外 / ツリー構築中 / line-clamp で隠れた行 /
                # 文書末尾のダンプ領域)。これを「まだ手前の行」とみなして進み続けると、
                # クリック位置と無関係な遠くの行(実測で 512 行先)まで彷徨い、そこから
                # 読み上げてしまう。かといってここで打ち切ると、倍々の跳びが測れない
                # 領域へ飛び込んだだけで、クリック行はその手前にあるのに近い行
                # (実測で 2 行手前)から読み始めてしまう。測れない行は「行き過ぎ」
                # の上限として扱い、手前の測れる範囲だけを二分探索する。
                if log is not None:
                    log.append(f"  行探索: {moved} 行先の矩形が取れず → 手前を二分探索")
                if moved == 0:
                    break
                far, far_measured, bracketed = moved, False, True
                break
            if s != side:
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
                if r is None or moved != mid:
                    break
                if s is None:
                    far, far_measured = mid, False   # 測れない行は上限として狭める
                    continue
                if s == 0:
                    if log is not None:
                        log.append(f"=> 行特定: {mid} 行移動で一致 (探索{probes[0]}回)")
                    return start_at(r), True
                if s == side:
                    near = mid
                else:
                    far, far_measured = mid, True

        if not bracketed and near == 0:
            # 一度も矩形で検証できていない。移動先の行には何の根拠も無いので、
            # 少なくともクリックした要素に由来する種の位置に留まる。
            if log is not None:
                log.append(f"=> 行特定: 矩形で検証できず 種の位置を使用 "
                           f"(探索{probes[0]}回)")
            return start_at(seed), False

        # どの行の矩形にも入らない(行間の余白やパディングをクリックした)場合は、
        # 文書順でクリック位置の直前にある行を採る。読み始めが手前にずれるだけで、
        # クリックした箇所を読み飛ばすことがない。
        final = near if side > 0 else (far if bracketed and far_measured else near)
        r, _, _ = line_at(final)
        if log is not None:
            log.append(f"=> 行特定: {final} 行移動で確定(矩形一致なし 探索{probes[0]}回)")
        return start_at(r or seed), False

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

        def above_click(k):
            s = self._char_side(text_range, k, y)
            return None if s is None else s == 1

        r, k = self._bisect_start(text_range, above_click)
        if r is None:
            return text_range
        if log is not None:
            log.append(f"=> 行特定: 範囲が{len(rects)}行ぶん → {k} 文字目まで切り詰め")
        return r

    def _refine_to_click_char(self, text_range, x, y, log=None):
        """クリック行の先頭から始まる範囲を、クリックした文字から始まる範囲にする。

        行を絞り込むのと同じ道具立て(文字の外接矩形=画面座標)だけで済ませる。
        行内の文字は左から右へ並ぶので「その文字の右端がクリック X 以下か」は
        文字位置について単調で、二分探索でクリック位置の文字を割り出せる。行を
        またいだ先の文字はクリック行より下に来るので、同じ判定がそのまま行末の
        番人になる。RangeFromPoint に文字位置を尋ねないので、Chromium が要素の
        先頭を返す癖の影響を受けない。

        判定できない文字に当たったら切り詰めをやめ、行頭のまま返す。読み始めが
        手前にずれるだけで、クリックした箇所を読み飛ばすことはない。
        """
        def measure(k):
            """k 文字目の (矩形群, クリック行との上下関係)。測れなければ ([], None)。

            Chromium はリンクや要素の先頭文字に、幅 1px 前後の退化矩形を単独で返す
            ことがある。その右端 (= 左端 + 1) で「手前」と判定すると、押した文字を
            読み飛ばして次の文字から読み始めてしまう。実体の無い矩形は捨てる。
            """
            r = self._char_range(text_range, k)
            rects = [rc for rc in (_rects(r) if r is not None else [])
                     if rc.right - rc.left > DEGENERATE_WIDTH]
            return rects, _rects_side(rects, y)

        def before_click(k):
            """k 文字目がクリック位置より手前か。判定できなければ None。"""
            rects, side = measure(k)
            if side is None:
                return None
            if side:
                return side == 1   # 1 = クリック行より上の行 = まだ手前
            return max(rc.right for rc in rects) <= x

        def reached_click(k):
            """クリック位置が k 文字目の左端以降にあるか。判定できなければ None。

            True なら k 文字目より前の文字はすべてクリック位置より手前と言える
            (文字は左から右へ並ぶため)。測れない文字の直後をクリックしたとき、
            その測れない文字を「手前」と確定させて、押した文字から読み始めるために使う。
            """
            rects, side = measure(k)
            if side is None:
                return None
            if side:
                return side == 1
            return min(rc.left for rc in rects) <= x

        r, k = self._bisect_start(text_range, before_click, reached_click)
        if r is None:
            return text_range
        if log is not None:
            log.append(f"  クリック文字: 行頭から {k} 文字目を開始位置にする")
        return r

    def _bisect_start(self, text_range, before, reached=None):
        """before(k) が False へ変わる最初の文字まで開始点を進めた (範囲, 位置)。

        before(k) は「k 文字目はまだ目標より手前か」を返し、判定できなければ
        None を返す。reached(k) は「目標は k 文字目の左端以降か」で、True なら
        k より前の文字はすべて手前と言える(省略可)。
        文書順に True → False と一度だけ切り替わることを前提に
        二分探索する。切り詰められないときは (None, -1) を返す。判定できない
        文字に当たった時点で諦めるので、読み始めが手前にずれることはあっても
        クリックした箇所を読み飛ばすことはない。
        """
        try:
            n = len(text_range.GetText(self._max_chars) or "")
        except Exception:
            return None, -1
        def judge(k):
            """before(k)。判定できない文字は、隣の文字の答えから単調性で補う。

            Chromium はリンクや強調の先頭文字に退化した矩形 (幅 1 高さ 0) を返す
            ことがあり、そこで諦めると、リンクを含む段落のリンクより後ろを
            クリックしても段落の頭から読み上げてしまう。k より後ろの文字が
            「手前」なら k も手前、k より前の文字が「手前でない」なら k も手前
            ではない、と文書順の単調性だけで確定できるので、推測は入らない。
            隣からも決まらないときは None を返し、切り詰めを諦める。
            """
            b = before(k)
            if b is not None:
                return b
            for d in range(1, BISECT_NEIGHBOR + 1):
                if k + d < n and before(k + d) is True:
                    return True
                # 後ろの文字の左端をクリックが過ぎていれば、k はその手前に収まる。
                # これが無いと、測れない文字の直後をクリックしても、その測れない
                # 文字から読み始めてしまう(1 文字手前にずれる)。
                if reached is not None and k + d < n and reached(k + d) is True:
                    return True
                if k - d > 0 and before(k - d) is False:
                    return False
            # 前の文字は手前・後ろの文字は手前でない、と挟まれて k だけ決まらない
            # ときは、k から読み始める(クリックより最大 1 文字手前になるだけ)。
            if (k > 0 and before(k - 1) is True) or (k + 1 < n and before(k + 1) is False):
                return False
            return None

        if n <= 1 or judge(0) is not True:
            return None, -1
        # lo は常に「手前」、hi は常に「手前ではない」という不変条件を保つ
        lo, hi = 0, n
        while hi - lo > 1:
            mid = (lo + hi) // 2
            b = judge(mid)
            if b is None:
                return None, -1
            lo, hi = (mid, hi) if b else (lo, mid)
        if hi == n:
            return None, -1        # 最後の文字まで手前 = 切り詰める先が無い
        r = self._char_start_range(text_range, hi)
        return (r, hi) if r is not None else (None, -1)

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

    def _char_range(self, base, k):
        """base の k 文字目 1 文字ぶんの範囲。取れなければ None。"""
        r = self._char_start_range(base, k)
        if r is None:
            return None
        try:
            r.MoveEndpointByRange(End, r, Start, waitTime=0)
            r.MoveEndpointByUnit(End, auto.TextUnit.Character, 1, waitTime=0)
        except Exception:
            return None
        return r

    def _char_side(self, base, k, y):
        """base の k 文字目とクリック位置の上下関係(_range_side と同じ符号)。"""
        r = self._char_range(base, k)
        return _range_side(r, y) if r is not None else None

    def _anchor_line(self, pattern, x, y, log=None):
        """クリック位置の1行分のテキストを返す(位置特定の目印)。"""
        r, _ = self._start_range_at_point(pattern, x, y, log)
        if r is None:
            return ""
        try:
            text = r.GetText(ANCHOR_MAX) or ""
        except Exception:
            return ""
        # 範囲がクリック行以降すべてに及ぶことがあるので、先頭の行を目印にする。
        # 開始位置はクリックした文字なので、行末近くを押すと数文字しか残らず、
        # 本文中の別の場所へ誤って一致する。十分な長さになるまで次の行も足す。
        picked = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            picked.append(line)
            if len("".join(picked)) >= ANCHOR_MIN:
                break
        return "\n".join(picked)

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
        deadline = time.perf_counter() + AGG_TIME_BUDGET
        best = ""
        best_tail = 0    # アンカー以降の長さ = 実際に読み上げる部分の長さ
        stalls = 0
        prev_id = None   # 1 段下の祖先の RuntimeId(キャッシュ照合キー)
        prev_cache = None  # その部分木の走査結果 (parts, nodes)
        for depth, node in enumerate(ancestors):
            try:
                parts, nodes = self._collect_descendant_text(
                    node, MESSAGE_MAX, prev_id, prev_cache, deadline)
            except _AggTimeout:
                # 途中までしか走査できていない段は信用できないので使わず、
                # 走査を終えた下位の段の結果で確定する。
                log.append(f"=> 集約 時間切れ({AGG_TIME_BUDGET}秒)→ {depth} 段で打ち切り")
                break
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
                                 prev_id=None, prev_cache=None, deadline=None):
        """root 配下の TextControl 葉のテキストを文書順(前順走査)で集める。

        char_limit を超えたら走査を打ち切る。呼び出し側が小さい上限を渡すことで、
        会話ペイン全体のような巨大な部分木を最後まで走査せず高速に判定できる。

        prev_id / prev_cache には 1 段下の祖先(root の直接の子のはず)の
        RuntimeId と走査結果を渡す。一致した子は再走査せず結果を流用するので、
        祖先を何段辿っても各ノードの走査は全体で 1 回で済む。照合の COM 呼び出しは
        root の直接の子(depth==1)に限定してコストを抑える。

        deadline (time.perf_counter の値) を過ぎたら _AggTimeout を送出する。

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
            if deadline is not None and time.perf_counter() > deadline:
                raise _AggTimeout()
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

    def _read_to_boundary(self, pattern, control, boundary_range, x, y,
                          coarse=False, expected_anchor="", log=None):
        """クリック位置(行頭にそろえる)から、クリックした項目の末尾までを返す。

        開始位置: RangeFromPoint(座標→文字位置)は、X のタイムラインのように
        セルを絶対配置で仮想スクロールする画面では別の投稿を指すことがある
        (クリックした投稿と無関係な行から読み始める症状の正体)。そこで
        ControlFromPoint で得た「実際にクリックされた要素」の範囲を探索の出発点に
        使い、そこから矩形照合でクリック行へ寄せる。座標ヒットテストは、要素の
        範囲が取れなかったときのフォールバックに下げる。

        終了位置: boundary_range(role=article / listitem 等の範囲)があればその
        末尾で止める。タイムラインは全体で 1 つの巨大な文書なので、文書末尾まで
        読むと後続の投稿やページのフッターまで読み上げてしまう。渡されなければ
        従来どおり文書末尾まで読む。
        """
        # 要素の範囲を種にする。矩形が一時的に取れないことがある(ツリー構築中)ので、
        # その場合だけ取り直す。矩形が取れていて位置が違うなら、それは本当に別の
        # 場所なので取り直さず座標探索へ回す。
        seed = None
        padded = None   # クリック位置のすぐ近くに文字がある、クリックした要素の範囲
        for attempt in range(RANGE_RETRY):
            if attempt:
                # Chromium はツリーを遅延構築する。待たずに取り直しても同じ結果に
                # なりやすいので、2 回目以降は少しだけ間を空ける。
                time.sleep(CLICK_TARGET_RETRY_DELAY)
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
            # 数行ぶんの小さな要素に限る。本文全体のような大きな入れ物の余白で
            # これを使うと、押した場所と無関係な先頭から読んでしまう。
            rects = _rects(cand)
            if (not _is_document(control) and _rects_gap(rects, y) <= PAD_NEAR
                    and _rects_height(rects) <= PAD_NEAR * 3):
                padded = cand
            break
        if log is not None:
            log.append("  開始種: クリックした要素の範囲を使用" if seed is not None
                       else "  開始種: 座標から探索")
        r, matched = self._start_range_at_point(pattern, x, y, log, seed=seed)
        if not matched and padded is not None:
            # 表のセルやダイアログの行の、文字の上下の余白を押した場合。座標→文字は
            # 余白に文字が無いので行を特定できないが、押した要素そのものは矩形で
            # 確定している。その要素の先頭から読む(従来は取得失敗になっていた)。
            if log is not None:
                log.append("=> 行特定: 要素の余白を押した → 要素の先頭から読む")
            r, matched = padded.Clone(), True
        if r is None:
            return ""
        # 粗いノード(文書全体)を種にしたうえ矩形でも行を特定できていないなら、
        # 読み始めの位置に根拠が無い。そのまま読むとページ先頭の隠しナビから
        # 読み上げてしまうので、取得失敗として扱う(橙の点灯で知らせる)。
        if coarse and not matched:
            if log is not None:
                log.append("=> クリック位置を特定できず(粗いノード+矩形不一致)→ 取得失敗")
            return ""

        def accept(text):
            """採用する本文を返す(破棄するなら None)。

            矩形が一致していれば開始位置は画面座標で検証済み。矩形が取れず
            文書順だけで確定した場合は、クリック要素の Name が読み始め付近に
            あることを追加確認する。動的な Chromium 文書で RangeFromPoint が
            一時的に別の通知文を指した結果を、そのまま読ませないため。
            確認できた場合も、要素より手前(前の行 / 前の項目の末尾 / ユーザー名など)
            から始まっていれば、クリックした要素の先頭まで切り詰める。
            """
            if matched or not _is_strong_anchor(expected_anchor):
                return text
            if not _anchor_near_start(text, expected_anchor):
                if log is not None:
                    log.append("  開始検証: クリック要素と取得範囲が一致せず → 破棄")
                return None
            trimmed = _trim_to_anchor(text, expected_anchor)
            if log is not None and len(trimmed) != len(text):
                log.append(f"  開始補正: クリック要素の先頭まで切り詰め "
                           f"({len(text) - len(trimmed)} 文字)")
            return trimmed

        if boundary_range is not None:
            try:
                clipped = r.Clone()
                if clipped.MoveEndpointByRange(End, boundary_range, End,
                                               waitTime=0):
                    text = clipped.GetText(self._max_chars) or ""
                    accepted = accept(text) if text.strip() else None
                    if accepted is not None and accepted.strip():
                        if log is not None:
                            log.append(f"  終端: 項目境界まで 長={len(accepted)}")
                        return accepted
            except Exception:
                pass
            if log is not None:
                log.append("  終端: 項目境界を適用できず → 文書末尾まで")
        try:
            if not r.MoveEndpointByRange(End, pattern.DocumentRange, End, waitTime=0):
                return ""
            text = r.GetText(self._max_chars) or ""
            accepted = accept(text)
            return accepted if accepted is not None else ""
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
                # 壊れたログ(途中で切れたマルチバイト等)で例外を出さない。読めない
                # 部分は置換文字にして履歴の残りを活かす。
                with open(DEBUG_LOG, "r", encoding="utf-8", errors="replace") as f:
                    past = f.read()
            except OSError:
                past = ""
            # 直近 DEBUG_KEEP 回分だけ残す(区切りで切り出して古い方を捨てる)
            chunks = past.split(DEBUG_SEP)[1:]
            chunks = chunks[-(DEBUG_KEEP - 1):] if DEBUG_KEEP > 1 else []
            # 先に全文を組み立てる。取得テキストに単独のサロゲート等があっても
            # 置換文字にして書き、書き込み途中の例外でログを空にしてしまわない。
            body = "".join(DEBUG_SEP + c for c in chunks) + "\n".join(entry)
            with open(DEBUG_LOG, "wb") as f:
                f.write(body.encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            # 診断ログの失敗で常駐スレッド(以後すべての取得)を止めない
            pass


def _is_document(node):
    try:
        return node.ControlTypeName == "DocumentControl"
    except Exception:
        return False


def _tiling_slots(rects):
    """縦に隙間なく並ぶ兄弟の矩形から、各要素の本来の位置とずれを求める。

    戻り値は {添字: (本来の top, 本来の bottom, 報告された top - 本来の top)}。
    高さを積み上げた位置と報告された top の差は、正しく報告されている要素どうし
    なら同じ値になる。その多数派を基準にするので、古い矩形の要素が混じっていても
    残りから本来の位置を復元できる。多数派が TILE_MAJORITY に満たなければ、
    縦に並ぶリストではない(横並び・重なり・隙間あり)とみなして None を返す。
    """
    items = [(i, r[1], r[3]) for i, r in enumerate(rects) if r[3] > r[1]]
    if len(items) < TILE_MIN_ITEMS:
        return None
    stacked = []   # (添字, 積み上げた位置, 高さ, 報告された top との差)
    total = 0
    for i, top, bottom in items:
        stacked.append((i, total, bottom - top, top - total))
        total += bottom - top
    base, votes = None, 0
    for _, _, _, offset in stacked:
        agree = sum(1 for s in stacked if abs(s[3] - offset) <= TILE_TOLERANCE)
        if agree > votes:
            base, votes = offset, agree
    if votes < TILE_MIN_ITEMS or votes < len(stacked) * TILE_MAJORITY:
        return None
    return {i: (pos + base, pos + base + height, offset - base)
            for i, pos, height, offset in stacked}


def _smallest_containing(rects, x, y):
    """(left, top, right, bottom) の並びから、座標を含む最小の矩形の添字を返す。

    座標を含む中で最も小さいものが、視覚的に最前面の要素。同じ大きさなら
    文書順で後ろ(=より深い子)を採る。該当が無ければ None。
    """
    best = None
    best_area = None
    for i, (left, top, right, bottom) in enumerate(rects):
        if not (left <= x < right and top <= y < bottom):
            continue
        area = (right - left) * (bottom - top)
        if best_area is None or area <= best_area:
            best, best_area = i, area
    return best


def _rects(text_range):
    """範囲の外接矩形(画面座標)。取得できなければ空リスト。"""
    try:
        rects = text_range.GetBoundingRectangles() or []
    except Exception:
        return []
    # Chromium はリンク先頭などで (0,0,1,0) のような高さ 0 の退化矩形を返す。
    # 画面座標としての意味が無く、残すと外接範囲が画面左上まで広がって誤判定する。
    return [r for r in rects if r.bottom > r.top]


def _rects_side(rects, y):
    """外接矩形群とクリック位置の上下関係を返す。

    0  … 矩形がクリック位置を含む(=クリックした行)
    1  … クリック位置より上(前方へ進む必要がある)
    -1 … クリック位置より下(後方へ戻る必要がある)
    None … 矩形が取れない(行が画面外など)ため判定できない

    戻り値はそのまま「移動すべき向き」として使える符号になっている。
    """
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


def _rects_gap(rects, y):
    """外接矩形群とクリック位置の縦の距離 (px)。含めば 0、矩形が無ければ無限大。"""
    if not rects:
        return float("inf")
    top = min(r.top for r in rects)
    bottom = max(r.bottom for r in rects)
    if y < top:
        return top - y
    if y >= bottom:
        return y - bottom + 1
    return 0


def _rects_height(rects):
    """外接矩形群が縦に占める高さ (px)。"""
    if not rects:
        return 0
    return max(r.bottom for r in rects) - min(r.top for r in rects)


def _range_side(text_range, y):
    """範囲の外接矩形とクリック位置の上下関係(_rects_side と同じ符号)。"""
    return _rects_side(_rects(text_range), y)


def _has_letter(text):
    """文字(言語は問わない)を 1 つでも含むか。

    数字・記号・￼(埋め込みオブジェクトの代替文字)だけの塊は、読み上げても
    意味を成さない UI の断片。本文の行は必ず文字を含むので、これで見分ける。
    """
    return any(ch.isalpha() for ch in text)


def _has_content(text):
    """本文と呼べる行を 1 つでも含むか(数字・記号・件数表示だけの塊でないか)。"""
    return any(not _is_ui_noise(line) for line in text.split("\n"))


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
    i = _trim_reaction_block(lines, i)
    if i == len(lines):
        return text
    trimmed = "\n".join(lines[:i]).rstrip()
    return trimmed if trimmed.strip() else text


def _is_short_label(line):
    """ボタン名やラベルのような短い行か(本文の行なら長いか句点で終わる)。"""
    compact = "".join(line.split())
    if not compact or len(compact) > LABEL_MAX:
        return False
    return not compact.endswith(("。", "．", ".", "！", "!", "？", "?"))


def _trim_reaction_block(lines, i):
    """末尾に並ぶ「反応ラベル + 件数」の塊(共感した/4174/なるほど/…)の手前を返す。

    _trim_trailing_ui は数字だけの行しか落とせないので、ラベルと件数が交互に
    並ぶ塊は「共感した、4174、なるほど、227、うーん」と読み上げられてしまう。
    塊の中に件数だけの行が 2 行以上あることを条件にして、短い一文で終わる本文を
    誤って削らないようにする。呼び出し元が末尾の件数行を先に落としているので、
    件数行が 2 行あれば塊は必ず 3 行以上になる。
    """
    j = i
    counts = 0
    while j > 0 and _is_short_label(lines[j - 1]):
        if _is_ui_noise(lines[j - 1]):
            counts += 1
        j -= 1
    return j if counts >= REACTION_MIN_COUNTS else i


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


def _anchor_consistent(idx, base, control_anchor):
    """アンカーの一致位置 idx が、クリックした要素の範囲に収まっているか。

    アンカーは短くなりうるので、本文中の別の場所へ誤って一致することがある。
    base はクリック要素のテキストが本文中で見つかった位置 (見つからなければ -1)。
    要素側を突き止められないときは検証しようがないのでそのまま通す
    (検証できないことを理由に読み上げを止めない)。
    """
    if base < 0 or not _is_strong_anchor(control_anchor):
        return True
    return base <= idx <= base + len(control_anchor)


def _is_strong_anchor(text):
    """クリック位置の根拠に使える程度の表示テキストか。"""
    return bool(text and len("".join(text.split())) >= 4 and _has_letter(text))


def _trim_to_anchor(text, anchor):
    """text がアンカー(クリック要素の表示テキスト)より手前から始まっていれば、
    アンカーの先頭へ切り詰める。見つからなければそのまま返す。"""
    idx = _find_anchor(text, anchor) if text and anchor else -1
    return text[idx:] if idx > 0 else text


def _anchor_near_start(text, anchor, limit=ANCHOR_MAX):
    """取得範囲の先頭付近がクリック要素のテキストと対応するか。"""
    if not text or not anchor:
        return False
    idx = _find_anchor(text, anchor)
    if 0 <= idx <= limit:
        return True

    # クリック要素が長い段落で、取得範囲がその途中から始まる場合も許容する。
    # 空白差を吸収した取得範囲の先頭が anchor 内にあれば、同じ段落とみなせる。
    compact_text = "".join(text.split())
    compact_anchor = "".join(anchor.split())
    overlap = min(12, len(compact_text), len(compact_anchor))
    head = compact_text[:overlap]
    return overlap >= 4 and head in compact_anchor
