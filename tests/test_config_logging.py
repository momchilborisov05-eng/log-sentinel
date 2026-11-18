import json
import logging
import unittest

from logsentinel.config import DEFAULT_MODEL, Settings, spec_for
from logsentinel.logging_setup import JsonFormatter


class ConfigTests(unittest.TestCase):
    def test_from_env_parses_and_falls_back(self):
        s = Settings.from_env({"SENTINEL_Z_THRESHOLD": "3.5", "SENTINEL_SEED_DEMO": "no", "SENTINEL_DEMO_DAYS": "x",
                               "SENTINEL_LLM": "RULES"})
        self.assertEqual((s.z_threshold, s.seed_demo_data, s.demo_days, s.llm_mode), (3.5, False, 7, "rules"))
        self.assertEqual(s.model, DEFAULT_MODEL)
        self.assertNotIn("sk-", repr(Settings(anthropic_api_key="sk-secret")))

    def test_llm_mode_rules(self):
        self.assertFalse(Settings(anthropic_api_key="").llm_enabled)
        self.assertTrue(Settings(anthropic_api_key="k").llm_enabled)
        self.assertFalse(Settings(anthropic_api_key="k", llm_mode="rules").llm_enabled)
        self.assertTrue(Settings(llm_mode="claude").llm_enabled)

    def test_unknown_metric_spec(self):
        self.assertEqual(spec_for("memory_pct").threshold, 95.0)
        s = spec_for("queue.depth")
        self.assertIsNone(s.threshold)
        self.assertEqual(s.direction, "both")


class LoggingTests(unittest.TestCase):
    def test_json_formatter_includes_extra_fields(self):
        rec = logging.LogRecord("logsentinel.api", logging.INFO, __file__, 1, "request", None, None)
        rec.path, rec.status = "/health", 200
        out = json.loads(JsonFormatter().format(rec))
        self.assertEqual((out["msg"], out["path"], out["status"], out["level"]), ("request", "/health", 200, "INFO"))
        self.assertTrue(out["ts"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
