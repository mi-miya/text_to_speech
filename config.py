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


class Config:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key in DEFAULTS:
                if key in saved:
                    self._data[key] = saved[key]
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
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
