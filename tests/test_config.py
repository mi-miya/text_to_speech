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


if __name__ == "__main__":
    unittest.main()
