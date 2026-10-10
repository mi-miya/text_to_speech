import json
import os
import tempfile
import unittest
from unittest import mock

import config


class ConfigTests(unittest.TestCase):
    def test_invalid_values_fall_back_without_breaking_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "rate": "fast",
                    "voice": 123,
                    "max_chars": None,
                    "pos": [10],
                }, stream)

            with mock.patch.object(config, "CONFIG_PATH", path):
                settings = config.Config()

            self.assertEqual(0, settings.rate)
            self.assertIsNone(settings.voice)
            self.assertEqual(30000, settings.max_chars)
            self.assertEqual((100, 100), settings.pos)

    def test_values_are_normalized_and_saved_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"rate": 99, "max_chars": 20, "pos": [2, "3"]}, stream)

            with mock.patch.object(config, "CONFIG_PATH", path):
                settings = config.Config()
                settings.voice = "Test Voice"

            self.assertEqual(10, settings.rate)
            self.assertEqual(100, settings.max_chars)
            self.assertEqual((2, 3), settings.pos)
            self.assertFalse(os.path.exists(path + ".tmp"))
            with open(path, "r", encoding="utf-8") as stream:
                self.assertEqual("Test Voice", json.load(stream)["voice"])

    def _load_raw(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "wb") as stream:
                stream.write(raw)
            with mock.patch.object(config, "CONFIG_PATH", path):
                return config.Config()

    def test_utf8_bom_file_is_still_read(self):
        # メモ帳で編集すると BOM が付く。以前は JSON 不正扱いで全設定が既定に戻った。
        settings = self._load_raw(b'\xef\xbb\xbf{"rate": 4, "voice": "X"}')
        self.assertEqual(4, settings.rate)
        self.assertEqual("X", settings.voice)

    def test_overflow_and_garbage_files_do_not_break_startup(self):
        raws = [
            b'{"rate": 1e999}', b'{"max_chars": 1e999}', b'{"pos": [1e999, 1]}',
            b'{"rate": Infinity}', b"[" * 100000, b"", b"\x00\x00", b"\xff\xfe{",
        ]
        for raw in raws:
            settings = self._load_raw(raw)
            self.assertEqual(0, settings.rate)
            self.assertEqual(30000, settings.max_chars)
            self.assertEqual((100, 100), settings.pos)

    def test_booleans_are_not_treated_as_numbers(self):
        settings = self._load_raw(
            b'{"rate": true, "max_chars": true, "pos": [true, false]}')
        self.assertEqual(0, settings.rate)
        self.assertEqual(30000, settings.max_chars)
        self.assertEqual((100, 100), settings.pos)

    def test_stale_tmp_file_does_not_affect_load_or_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"rate": 5}')
            with open(path + ".tmp", "w", encoding="utf-8") as stream:
                stream.write('{"rate": 1, "tr')  # 保存中の強制終了を模した途中切れ
            with mock.patch.object(config, "CONFIG_PATH", path):
                settings = config.Config()
                self.assertEqual(5, settings.rate)
                settings.rate = 6
                self.assertEqual(6, config.Config().rate)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_set_rate_without_persist_is_saved_only_by_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with mock.patch.object(config, "CONFIG_PATH", path):
                settings = config.Config()
                settings.set_rate(9, persist=False)
                self.assertFalse(os.path.exists(path))
                settings.save()
                self.assertEqual(9, config.Config().rate)


if __name__ == "__main__":
    unittest.main()
