"""画面上のテキストを Ctrl+Alt+左クリックで読み上げる常駐ツール。

画面上に丸いフローティングアイコンが常に表示される。
  - Ctrl+Alt を押しながら文中を左クリック → その位置から文末まで読み上げ
  - Esc または アイコンを左クリック → 停止
  - アイコンをドラッグ → 移動 (位置は記憶される)
  - アイコンを右クリック → 停止 / 話速 / 音声 / 終了 のメニュー
起動は launch.vbs をダブルクリック (コンソール窓なしで常駐)。
"""
import ctypes
import os
import re
import sys

# pythonw (コンソール無し) では stdout/stderr が None になり、
# ライブラリの出力でクラッシュするため devnull に差し替える
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import threading
import tkinter as tk
import winsound

from pynput import keyboard, mouse

from capture import TextCapturer
from config import Config
from speech import SpeechEngine, list_voices

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

RATE_MIN = -10  # SAPI 話速の下限(最も遅い)
RATE_MAX = 10   # SAPI 話速の上限(最も速い)。0 が標準。刻みはこの 21 段階が最小

COLOR_IDLE = "#1E6ED2"      # 待機中: 青
COLOR_SPEAKING = "#2FA84F"  # 読み上げ中: 緑
COLOR_ERROR = "#E67E22"     # テキスト取得失敗: 橙 (一時的に点灯)
TRANSPARENT = "#010101"     # ウィンドウを丸く見せるための透過色


def ensure_single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "ScreenReaderTool_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            0, "読み上げツールは既に起動しています。\n画面上の丸いアイコンを探してください。",
            "読み上げツール", 0x40)
        sys.exit(0)


def hotkey_down():
    state = ctypes.windll.user32.GetAsyncKeyState
    return bool(state(VK_CONTROL) & 0x8000) and bool(state(VK_MENU) & 0x8000)


def normalize(text):
    text = re.sub(r"[ \t　]+", " ", text)
    # \r\n と \n で段落の扱いが変わらないよう、先に改行コードを揃える。
    # 空行(段落区切り)は 1 つ残し、3 連続以上の改行だけ詰める。
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def resolve_voice(configured, voices):
    """保存済み音声を解決し、消えていれば安全なローカル音声へ退避する。"""
    if not configured:
        return None
    configured_lower = configured.lower()
    for name in voices:
        if name.lower() == configured_lower:
            return name
    # 部分一致は並び順で最初の音声が選ばれるため、"Nanami" のような短い指定が
    # Online 音声に当たらないよう、指定自体に online と書かれていなければ除外する。
    allow_online = "online" in configured_lower
    for name in voices:
        lower = name.lower()
        if not allow_online and "online" in lower:
            continue
        if configured_lower in lower or lower in configured_lower:
            return name
    # Natural Voice Adapter や音声モデルが一時的に消えた場合、オンライン音声を
    # 勝手に選ぶと本文が外部へ送信され得る。非 Online が無ければ None を返し、
    # 呼び出し側は音声を変えずシステム既定のまま読み上げる。
    return next((name for name in voices if "online" not in name.lower()), None)


class FloatingWidget:
    """常に最前面に表示される丸いアイコン。ドラッグ移動・右クリックメニュー付き。"""

    def __init__(self, app):
        self.app = app
        self.root = tk.Tk()
        self.root.title("読み上げツール")

        try:
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except OSError:
            scale = 1.0
        size = int(56 * scale)
        self._size = size

        self.root.overrideredirect(True)          # 枠なし
        self.root.attributes("-topmost", True)    # 常に最前面
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg=TRANSPARENT)
        self.root.wm_attributes("-transparentcolor", TRANSPARENT)  # 円形に見せる
        x, y = app.config.pos
        self.root.geometry(f"{size}x{size}+{x}+{y}")

        self.canvas = tk.Canvas(self.root, width=size, height=size,
                                bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        m = max(2, int(3 * scale))
        self.circle = self.canvas.create_oval(m, m, size - m, size - m,
                                              fill=COLOR_IDLE, outline="white", width=2)
        self.label = self.canvas.create_text(size // 2, size // 2, text="読",
                                             fill="white",
                                             font=("Yu Gothic UI", -int(22 * scale), "bold"))

        # ドラッグ移動と左クリック(=停止)の判別
        self.canvas.bind("<ButtonPress-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._show_menu)

        self._build_menu()

        self._start_x = self._start_y = 0
        self._win_x = self._win_y = 0
        self._has_moved = False
        self._flashing = False

        self._rate_win = None       # 話速調整ウィンドウ(単一インスタンス)
        self._rate_scale = None
        self._rate_save_job = None  # ドラッグ確定を遅延保存するための after id
        self._flash_job = None      # 橙フラッシュ解除の after id

        self._poll_state()

    # ---- メニュー ----

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="読み上げを停止 (Esc)", command=self.app.stop)
        self.menu.add_separator()

        self.menu.add_command(label="話速を調整...", command=self._open_rate_dialog)

        self._voice_var = tk.StringVar(value=self.app.active_voice or "")
        voice_menu = tk.Menu(self.menu, tearoff=0)
        if self.app.voices:
            for name in self.app.voices:
                voice_menu.add_radiobutton(
                    label=name, value=name, variable=self._voice_var,
                    command=lambda n=name: self.app.set_voice(n))
        else:
            voice_menu.add_command(label="(音声が見つかりません)", state="disabled")
        self.menu.add_cascade(label="音声", menu=voice_menu)

        self.menu.add_separator()
        self.menu.add_command(label="終了", command=self.app.quit)

    def _show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---- 話速の調整(スライダー) ----

    def _open_rate_dialog(self):
        # 既に開いていれば前面に出すだけ(二重起動を防ぐ)
        if self._rate_win is not None and self._rate_win.winfo_exists():
            self._rate_win.deiconify()
            self._rate_win.lift()
            self._rate_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._rate_win = win
        win.title("話速の調整")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(padx=16, pady=12)
        win.protocol("WM_DELETE_WINDOW", self._close_rate_dialog)

        tk.Label(win, text="遅い ←  0=標準  → 速い",
                 font=("Yu Gothic UI", 10)).pack(anchor="w")

        self._rate_scale = tk.Scale(
            win, from_=RATE_MIN, to=RATE_MAX, orient="horizontal", length=300,
            resolution=1, tickinterval=5, showvalue=True,
            font=("Yu Gothic UI", 9), command=self._on_rate_slide)
        self._rate_scale.set(self.app.config.rate)
        self._rate_scale.pack(fill="x", pady=(4, 8))
        # ドラッグ確定時に保存(キー操作でも動くよう遅延保存も併用)
        self._rate_scale.bind("<ButtonRelease-1>", lambda e: self._persist_rate())

        btns = tk.Frame(win)
        btns.pack(fill="x")
        tk.Button(btns, text="試聴", width=8, command=self._preview_rate).pack(side="left")
        tk.Button(btns, text="標準に戻す", width=10, command=self._reset_rate).pack(
            side="left", padx=6)
        tk.Button(btns, text="閉じる", width=8, command=self._close_rate_dialog).pack(
            side="right")

        # フローティングアイコンの近くに表示する
        win.update_idletasks()
        wx, wy = self.root.winfo_x(), self.root.winfo_y()
        win.geometry(f"+{max(0, wx - 330)}+{max(0, wy)}")

    def _on_rate_slide(self, value):
        rate = int(float(value))
        self.app.set_rate(rate, persist=False)  # 即時反映(保存は遅延)
        if self._rate_save_job is not None:
            self.root.after_cancel(self._rate_save_job)
        self._rate_save_job = self.root.after(500, self._persist_rate)

    def _persist_rate(self):
        if self._rate_save_job is not None:
            self.root.after_cancel(self._rate_save_job)
            self._rate_save_job = None
        self.app.config.save()

    def _preview_rate(self):
        self.app.speech.speak("これは話速の試聴です。1、2、3、4、5。")

    def _reset_rate(self):
        if self._rate_scale is not None:
            self._rate_scale.set(0)
        # Scale.set のコールバックは idle 時に遅れて呼ばれるため、それを待たずに
        # 0 を反映してから保存する(待つと旧値が保存され、終了で失われ得る)
        self.app.set_rate(0, persist=False)
        self._persist_rate()

    def _close_rate_dialog(self):
        self._persist_rate()
        if self._rate_win is not None:
            self._rate_win.destroy()
            self._rate_win = None
            self._rate_scale = None

    # ---- ドラッグ移動 / クリック ----

    def _start_move(self, event):
        self._start_x, self._start_y = event.x_root, event.y_root
        self._win_x, self._win_y = self.root.winfo_x(), self.root.winfo_y()
        self._has_moved = False

    def _do_move(self, event):
        dx = event.x_root - self._start_x
        dy = event.y_root - self._start_y
        if abs(dx) > 3 or abs(dy) > 3:
            self._has_moved = True
            self.root.geometry(f"+{self._win_x + dx}+{self._win_y + dy}")

    def _on_release(self, event):
        if self._has_moved:
            self.app.config.pos = (self.root.winfo_x(), self.root.winfo_y())
        else:
            self.app.stop()  # ドラッグでない左クリックは停止

    # ---- 状態表示 ----

    def _poll_state(self):
        # capture スレッドは Event を立てるだけにし、Tk の操作は必ず
        # mainloop のスレッドで行う。Tkinter のスレッド間呼び出しによる
        # RuntimeError やまれな停止を防ぐ。
        if self.app.capture_error_pending.is_set():
            self.app.capture_error_pending.clear()
            self._do_flash()
        elif not self._flashing:
            color = COLOR_SPEAKING if self.app.speech.is_speaking else COLOR_IDLE
            self.canvas.itemconfig(self.circle, fill=color)
        self.root.after(300, self._poll_state)

    def _do_flash(self):
        self._flashing = True
        self.canvas.itemconfig(self.circle, fill=COLOR_ERROR)
        # 連続で失敗したとき、前回の解除予約が新しい点灯を早く消さないよう取り消す
        if self._flash_job is not None:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(900, self._end_flash)

    def _end_flash(self):
        self._flash_job = None
        self._flashing = False

    def run(self):
        self.root.mainloop()


class App:
    def __init__(self):
        self.config = Config()
        self.speech = SpeechEngine(rate=self.config.rate, voice_name=self.config.voice)
        self.capture_error_pending = threading.Event()
        self.capturer = TextCapturer(
            on_text=self._on_text,
            on_error=self._on_capture_error,
            max_chars=self.config.max_chars,
        )
        self.capturer.start()
        self._swallow_up = False
        try:
            voices = list_voices()
        except Exception:
            voices = []
        # 日本語音声のみメニューに出す (オンライン英語音声の羅列を防ぐ)
        self.voices = [n for n in voices if "Japanese" in n] or voices
        self.active_voice = resolve_voice(self.config.voice, self.voices)
        if (self.config.voice and self.active_voice
                and self.active_voice.lower() != self.config.voice.lower()):
            # 設定は書き換えず、音声モデルが復旧した次回起動時に元の選択へ戻せる
            # ようにする。現在の実行だけ、確実に存在する音声へフォールバックする。
            self.speech.set_voice(self.active_voice)
        self.widget = None

    # ---- テキスト取得 → 読み上げ ----

    def _on_text(self, text):
        self.speech.speak(normalize(text))

    def _on_capture_error(self):
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        self.capture_error_pending.set()

    # ---- フック ----

    def _mouse_filter(self, msg, data):
        if msg == WM_LBUTTONDOWN and hotkey_down():
            self.capturer.request(data.pt.x, data.pt.y)
            self._swallow_up = True
            self.mouse_listener.suppress_event()
        elif msg == WM_LBUTTONUP and self._swallow_up:
            self._swallow_up = False
            self.mouse_listener.suppress_event()
        elif msg == WM_LBUTTONDOWN:
            # ホットキー無しの押下は対象アプリへ渡る。前回抑制した down の up が
            # 届かなかった場合に旗が残っていると、この押下の up を握りつぶして
            # 対象アプリ側にボタン押しっぱなしが残るため、新しい押下で必ず解除する。
            self._swallow_up = False
        return True

    def _on_key_press(self, key):
        if key == keyboard.Key.esc:
            self.stop()

    def stop(self):
        """読み上げと、まだ完了していないテキスト取得をまとめて停止する。"""
        self.capturer.cancel()
        self.speech.stop()

    # ---- 設定変更 ----

    def set_rate(self, value, persist=True):
        self.config.set_rate(value, persist)
        self.speech.set_rate(value)

    def set_voice(self, name):
        self.config.voice = name
        self.active_voice = name
        self.speech.set_voice(name)

    # ---- 起動・終了 ----

    def quit(self):
        self.stop()
        try:
            # 話速スライダーの遅延保存(500ms)待ちのまま os._exit すると
            # 直前の変更が失われるため、終了前に必ず書き出す。
            self.config.save()
            self.mouse_listener.stop()
            self.keyboard_listener.stop()
            self.widget.root.destroy()
        finally:
            os._exit(0)

    def run(self):
        self.mouse_listener = mouse.Listener(win32_event_filter=self._mouse_filter)
        self.mouse_listener.start()
        self.keyboard_listener = keyboard.Listener(on_press=self._on_key_press)
        self.keyboard_listener.start()

        self.widget = FloatingWidget(self)
        self.widget.run()


def main():
    ensure_single_instance()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 高DPI環境での座標ずれ防止
    except OSError:
        pass
    App().run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        raise
