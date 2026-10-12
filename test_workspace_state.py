import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import load_preferences, save_preferences, sanitize_preferences


class PreferencesTests(unittest.TestCase):
    def test_missing_file_is_read_only_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.assertEqual(load_preferences(path)["refresh_seconds"], 15)
            self.assertFalse(path.exists())

    def test_bad_json_does_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("broken", encoding="utf-8")
            self.assertEqual(load_preferences(path)["watchlist"], [])

    def test_roundtrip_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.assertTrue(save_preferences({"recent_queries": ["301307", "东方财富"], "watchlist": ["301307"], "refresh_seconds": 30}, path))
            actual = load_preferences(path)
            self.assertEqual(actual["refresh_seconds"], 30)
            self.assertEqual(actual["recent_queries"], ["301307", "东方财富"])
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_invalid_preferences_are_sanitized(self):
        result = sanitize_preferences({"refresh_seconds": "NaN", "daily_range": -100, "watchlist": ["301307", "invalid", "301307"], "recent_queries": ["", "x\n", "x", "x", 42]})
        self.assertEqual(result["refresh_seconds"], 15)
        self.assertEqual(result["daily_range"], 60)
        self.assertEqual(result["watchlist"], ["301307"])
        self.assertEqual(result["recent_queries"], ["x"])

    def test_large_file_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"last_export_dir": "x" * 70000}), encoding="utf-8")
            self.assertEqual(load_preferences(path)["last_export_dir"], "")

    def test_failed_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            save_preferences({"refresh_seconds": 30}, path)
            with patch("workspace_state.os.replace", side_effect=OSError("blocked")):
                self.assertFalse(save_preferences({"refresh_seconds": 5}, path))
            self.assertEqual(load_preferences(path)["refresh_seconds"], 30)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_recent_queries_are_bounded(self):
        self.assertEqual(len(sanitize_preferences({"recent_queries": [str(i) for i in range(100)]})["recent_queries"]), 15)

    def test_unhashable_chart_mode_is_defaulted(self):
        for value in ([], {}, ["日线"]):
            self.assertEqual(sanitize_preferences({"chart_mode": value})["chart_mode"], "日线")

    def test_export_directory_is_bounded(self):
        self.assertEqual(len(sanitize_preferences({"last_export_dir": "x" * 5000})["last_export_dir"]), 4096)

    def test_ai_settings_default_blank_without_credentials(self):
        actual = sanitize_preferences({"api_key": "secret", "ai_api_key": "secret", "access_token": "token"})
        self.assertEqual(actual["ai_base_url"], "")
        self.assertEqual(actual["ai_model"], "")
        self.assertEqual(actual["ai_auth_mode"], "api_key")
        self.assertNotIn("secret", json.dumps(actual))
        self.assertNotIn("access_token", actual)

    def test_ai_settings_roundtrip_only_public_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            save_preferences({"ai_base_url": "http://127.0.0.1:8317/v1/", "ai_model": "my-model", "ai_protocol": "responses", "ai_auth_mode": "chatgpt", "api_key": "dont-save"}, path)
            actual = load_preferences(path)
            self.assertEqual(actual["ai_base_url"], "http://127.0.0.1:8317/v1")
            self.assertEqual(actual["ai_protocol"], "responses")
            self.assertEqual(actual["ai_model"], "my-model")
            self.assertEqual(actual["ai_auth_mode"], "chatgpt")
            self.assertNotIn("dont-save", path.read_text(encoding="utf-8"))

    def test_ai_credentials_embedded_in_url_are_not_saved(self):
        for value in ("https://user:secret@host/v1", "https://host/v1?api_key=secret", "https://host/v1#token", "http://external.test/v1", "file:///data", "https://host:bad/v1", "https://host/v1\n", [], {}):
            self.assertEqual(sanitize_preferences({"ai_base_url": value})["ai_base_url"], "")

    def test_ai_settings_invalid_types_defaulted(self):
        actual = sanitize_preferences({"ai_auth_mode": [], "ai_protocol": {}, "ai_model": ["a"]})
        self.assertEqual(actual["ai_auth_mode"], "api_key")
        self.assertEqual(actual["ai_protocol"], "chat_completions")
        self.assertEqual(actual["ai_model"], "")

    def test_overlong_ai_settings_not_silently_changed(self):
        actual = sanitize_preferences({"ai_model": "x" * 241, "ai_base_url": "https://example.com/" + "x" * 2048})
        self.assertEqual(actual["ai_model"], "")
        self.assertEqual(actual["ai_base_url"], "")

    def test_wait_settings_default_validate_and_roundtrip(self):
        self.assertEqual(sanitize_preferences({})["ai_stream_timeout"], 600)
        self.assertEqual(sanitize_preferences({})["ai_idle_timeout"], 120)
        for name, low, high, default in (("ai_stream_timeout", 30, 1800, 600), ("ai_idle_timeout", 5, 300, 120)):
            for bad in (None, True, False, [], {}, "nan", float("inf"), low - 1, high + 1):
                with self.subTest(name=name, bad=bad):
                    self.assertEqual(sanitize_preferences({name: bad})[name], default)
            self.assertEqual(sanitize_preferences({name: low})[name], low)
            self.assertEqual(sanitize_preferences({name: high})[name], high)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefs.json"
            self.assertTrue(save_preferences({"ai_stream_timeout": 900, "ai_idle_timeout": 180}, path))
            self.assertEqual(load_preferences(path)["ai_stream_timeout"], 900)
            self.assertEqual(load_preferences(path)["ai_idle_timeout"], 180)


if __name__ == "__main__":
    unittest.main()
