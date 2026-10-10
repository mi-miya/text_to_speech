"""SAPI5 による読み上げエンジン。

専用スレッドで SpVoice を保持し、コマンドキュー経由で操作する。
SpVoice は生成したスレッドでのみ触る(COM のアパートメント制約のため)。
NaturalVoiceSAPIAdapter を導入すると、Microsoft の自然音声 (Nanami 等) が
SAPI 音声として見えるようになり、このコードのまま使える。
"""
import queue
import re
import threading
import unicodedata

import pythoncom
import win32com.client

SVSF_ASYNC = 1
SVSF_PURGE = 2
# 本文を SAPI XML として解釈させない。ページ上の <tag> がマークアップ扱いで欠落したり、
# <speak> 等で合成が失敗して無音になる(Nanami で確認)のを防ぐ。
SVSF_IS_NOT_XML = 16

# 自然音声(ニューラル合成)は Speak に渡した塊をある程度まとめて合成してから
# 再生を始める。クリック位置から文末までの全文を一度に渡すと、最初の音が出るまで
# 全体の合成を待たされる。そこで「最初の一文」だけ先に鳴らし、残りは後ろにキュー
# することで、立ち上がりを「1 文分の合成時間」まで短縮する。
_SENTENCE_END = re.compile(r"(?<=[。!?！？\n])")
FIRST_CHUNK_MIN = 60  # 最初に鳴らす塊のおおよその下限文字数(短すぎる細切れを防ぐ)


def _split_first(text):
    """最初のひとまとまり(概ね FIRST_CHUNK_MIN 以上の一文)と残りに分ける。

    句点等が全く無い塊はそのまま返す(不自然な途中切りを避ける)。
    """
    parts = [p for p in _SENTENCE_END.split(text) if p]
    if len(parts) <= 1:
        return text, ""
    first = parts[0]
    idx = 1
    while idx < len(parts) and len(first) < FIRST_CHUNK_MIN:
        first += parts[idx]
        idx += 1
    return first, "".join(parts[idx:])

# 一部アプリはソフト改行や区切りに制御文字を使う。これらは改行に寄せる。
_LINE_SEPARATORS = str.maketrans({
    "\x0b": "\n", "\x0c": "\n",                # 垂直タブ / 改ページ
    "\x1c": "\n", "\x1d": "\n", "\x1e": "\n",  # ファイル/グループ/レコード区切り
    "\x85": "\n", " ": "\n", " ": "\n",  # NEL / 行区切り / 段落区切り
})

# XML 1.0 で許可される文字は \t \n \r と U+0020 以降 (サロゲート等を除く)。
# 許可されない制御文字などが SSML に混じると、アダプタの合成が丸ごと失敗して
# 読み上げが停止する (EMBEDDED_TTS_ERROR_INVALID_SSML)。事前に除去しておく。
_INVALID_XML = re.compile(
    "[^\t\n\r -퟿-�\U00010000-\U0010FFFF]"
)


# 読み上げ対象として無意味な不可視文字は削除する(Web ページに多い)。
_NOISE_CHARS = dict.fromkeys([
    0xFFFC,                      # 画像等の埋め込みオブジェクト代替文字
    0xFEFF,                      # BOM / ゼロ幅ノーブレークスペース
    0x200B, 0x200C, 0x200D,      # ゼロ幅スペース / 非接合子 / 接合子
    0x2060,                      # ワードジョイナ
    0xFFF9, 0xFFFA, 0xFFFB,      # ルビ(interlinear annotation)アンカー
], None)


def sanitize_for_sapi(text):
    """SAPI/SSML に渡して安全なテキストへ整える。制御文字による停止を防ぐ。"""
    text = text.translate(_LINE_SEPARATORS)
    text = text.translate(_NOISE_CHARS)
    return _INVALID_XML.sub(" ", text)


# URL は読み上げると長いだけで内容が伝わらないので読み飛ばす。日本語の文章は
# URL の直後に空白なしで本文が続くことが多いため、URL に使える ASCII 文字だけを
# 対象にして後続の本文を巻き込まない。X はスキームを省いて「example.com/abc…」と
# 表示するので、スキーム無しでも「ドメイン + /パス」の形なら URL とみなす
# (「3.5」「Node.js」のようにパスの無いものは本文なので残す)。
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"
_URL = re.compile(
    r"(?:https?://|www\.)" + _URL_CHARS + r"+…?"
    r"|(?<![A-Za-z0-9@._-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/"
    + _URL_CHARS + r"*…?"
)

# 絵文字の合成に使う不可視の部品(異体字セレクタ / 囲み記号 / タグ文字)。
_EMOJI_PARTS = re.compile("[︀-️⃣󠀠-󠁿]")
# 読み上げても意味を成さない装飾記号。通貨・%・四則演算・句読点は文意や
# 抑揚に関わるので残し、見出しや飾りに使われるものだけを落とす。
_DECOR_CHARS = frozenset("#＃*＊_＿|｜~^`\※")
_RULE_LINE = re.compile(r"[-=‐-―─━]{3,}")  # ----- や ━━━ のような罫線


def _is_unspoken(ch):
    """絵文字・装飾記号など、読み飛ばす文字か。"""
    if ch in _DECOR_CHARS:
        return True
    # So: 絵文字・★・♪・©・罫線素片 / Sk: 肌色修飾など / Co: 私用領域(アイコンフォント)
    if unicodedata.category(ch) in ("So", "Sk", "Co"):
        return True
    return "←" <= ch <= "⇿"  # 矢印(→ ⇒ 等)


def strip_unspoken(text):
    """絵文字・装飾記号・URL を読み飛ばす(利用者の要望)。"""
    text = _URL.sub(" ", text)
    text = _EMOJI_PARTS.sub("", text)
    text = _RULE_LINE.sub(" ", text)
    text = "".join(" " if _is_unspoken(ch) else ch for ch in text)
    return re.sub(r"[ 	]{2,}", " ", text)


def list_voices():
    """インストール済み SAPI 音声の説明文字列一覧を返す(メインスレッド用)。"""
    pythoncom.CoInitialize()
    voice = tokens = None
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        return [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
    finally:
        # COM オブジェクトを CoUninitialize より先に解放する。後だと
        # "Win32 exception occurred releasing IUnknown" が stderr に出る。
        del voice, tokens
        pythoncom.CoUninitialize()


SRSE_DONE = 1  # SpeechRunState: 再生完了


class SpeechEngine:
    def __init__(self, rate=0, voice_name=None):
        self._q = queue.Queue()
        self._speaking = False
        self._q.put(("rate", rate))
        if voice_name:
            self._q.put(("voice", voice_name))
        self._thread = threading.Thread(target=self._run, daemon=True, name="speech")
        self._thread.start()

    @property
    def is_speaking(self):
        return self._speaking

    def speak(self, text):
        text = strip_unspoken(sanitize_for_sapi(text))
        if text.strip():
            self._q.put(("speak", text))

    def stop(self):
        self._q.put(("stop", None))

    def set_rate(self, rate):
        self._q.put(("rate", rate))

    def set_voice(self, name):
        self._q.put(("voice", name))

    def _run(self):
        pythoncom.CoInitialize()
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        while True:
            try:
                cmd, arg = self._q.get(timeout=0.3)
            except queue.Empty:
                # 再生が終わったかを定期的に確認して状態を更新する
                if self._speaking:
                    try:
                        if voice.Status.RunningState == SRSE_DONE:
                            self._speaking = False
                    except pythoncom.com_error:
                        self._speaking = False
                continue
            try:
                if cmd == "speak":
                    # 最初の一文だけ即座に再生(PURGE で前の読み上げを止める)、
                    # 残りは PURGE 無しで後ろにキューし、続けて滑らかに読ませる。
                    first, rest = _split_first(arg)
                    voice.Speak(first, SVSF_ASYNC | SVSF_PURGE | SVSF_IS_NOT_XML)
                    if rest:
                        voice.Speak(rest, SVSF_ASYNC | SVSF_IS_NOT_XML)
                    self._speaking = True
                elif cmd == "stop":
                    voice.Speak("", SVSF_ASYNC | SVSF_PURGE)
                    self._speaking = False
                elif cmd == "rate":
                    voice.Rate = max(-10, min(10, int(arg)))
                elif cmd == "voice":
                    self._apply_voice(voice, arg)
            except (pythoncom.com_error, TypeError, ValueError, OverflowError,
                    AttributeError):
                # 音声デバイス切替等の一時的なエラーや、不正な話速・音声名(数値化できない値、
                # inf、None 等)で、この専用スレッドが死んで以後の読み上げが無音になるのを防ぐ。
                pass

    @staticmethod
    def _pick_voice(descriptions, name):
        """name に合う音声の添字を返す(無ければ None)。

        完全一致を最優先し、部分一致では Online 版より手元で合成する音声を先に選ぶ。
        「Microsoft Nanami」のような名前が列挙順次第で Online 版(本文を Microsoft へ
        送信する)に当たるのを防ぐ。name 自体に online を含めば Online 版も選べる。
        空の名前は全音声に部分一致してしまうので何も選ばない。
        """
        if not isinstance(name, str) or not name.strip():
            return None
        want = name.lower()
        lowered = [d.lower() for d in descriptions]
        if want in lowered:
            return lowered.index(want)
        hits = [i for i, d in enumerate(lowered) if want in d]
        if "online" not in want:
            hits = [i for i in hits if "online" not in lowered[i]] or hits
        return hits[0] if hits else None

    @staticmethod
    def _apply_voice(voice, name):
        tokens = voice.GetVoices()
        count = tokens.Count
        descriptions = [tokens.Item(i).GetDescription() for i in range(count)]
        idx = SpeechEngine._pick_voice(descriptions, name)
        if idx is not None:
            voice.Voice = tokens.Item(idx)
