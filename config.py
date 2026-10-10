"""設定の読み書き。config.json をスクリプトと同じフォルダに保存する。"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "rate": 0,          # SAPI の話速 (-10 〜 10)
    "voice": None,      # 音声名の部分一致文字列。None なら既定の音声
    "max_chars": 30000, # 一度に読み上げる最大文字数
    "pos": [100, 100],  # フローティングアイコンの位置
}


def _to_int(value):
    """設定値を整数へ変換する。変換できない値(型違い・真偽値・inf/NaN)は None。

    bool は int の派生で int(True) == 1 になってしまうため明示的に除外する。
    inf は int() が ValueError ではなく OverflowError を投げる。
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


class Config:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self._data["pos"] = list(DEFAULTS["pos"])
        self.load()

    def load(self):
        try:
            # utf-8-sig: メモ帳等で保存された BOM 付きでも読める(通常の UTF-8 も可)。
            # BOM があると utf-8 では JSON が不正扱いになり、設定が全て既定に戻ってしまう。
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                return

            rate = _to_int(saved.get("rate"))
            if rate is not None:
                self._data["rate"] = max(-10, min(10, rate))
            voice = saved.get("voice")
            if voice is None or isinstance(voice, str):
                self._data["voice"] = voice
            max_chars = _to_int(saved.get("max_chars"))
            if max_chars is not None:
                self._data["max_chars"] = max(100, min(1_000_000, max_chars))
            pos = saved.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                x, y = _to_int(pos[0]), _to_int(pos[1])
                if x is not None and y is not None:
                    self._data["pos"] = [x, y]
        # RecursionError: 極端に深い入れ子の JSON。壊れた設定で起動不能にならないよう
        # 読み込み失敗として扱う(UnicodeDecodeError / JSONDecodeError は ValueError 派生)。
        except (OSError, ValueError, RecursionError):
            pass

    def save(self):
        # 直接上書き中にプロセスが終了すると JSON が途中で切れ、次回起動時に
        # 設定が失われる。隣接する一時ファイルへ書いてから原子的に置き換える。
        temp_path = CONFIG_PATH + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, CONFIG_PATH)
        except OSError:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    @property
    def rate(self):
        return self._data["rate"]

    @rate.setter
    def rate(self, value):
        self.set_rate(value, persist=True)

    def set_rate(self, value, persist=True):
        """話速を更新する。persist=False ならファイル保存を省く。

        スライダーのドラッグ中は毎フレーム保存するとディスク書き込みが多発する
        ため、ドラッグ中は persist=False で即時反映だけ行い、確定時に save() する。
        """
        self._data["rate"] = max(-10, min(10, int(value)))
        if persist:
            self.save()

    @property
    def voice(self):
        return self._data["voice"]

    @voice.setter
    def voice(self, value):
        self._data["voice"] = value
        self.save()

    @property
    def max_chars(self):
        return self._data["max_chars"]

    @property
    def pos(self):
        return tuple(self._data["pos"])

    @pos.setter
    def pos(self, value):
        self._data["pos"] = [int(value[0]), int(value[1])]
        self.save()
