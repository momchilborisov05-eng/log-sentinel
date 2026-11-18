import json
import unittest

from logsentinel.summary import ClaudeSummarizer, Summarizer, SummaryError, rule_summary

from .helpers import FakeAnthropic, loaded_service, settings


class SummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc = loaded_service()
        cls.analysis = svc.analyze()
        svc.close()

    def test_rule_summary_leads_with_the_soonest_failure_and_runbooks(self):
        s = rule_summary(self.analysis)
        self.assertEqual(s["source"], "rules")
        self.assertEqual(s["severity"], "critical")
        self.assertEqual(s["title"], self.analysis["predictions"][0]["headline"])
        self.assertIn("80% band", s["summary"])
        self.assertIn("Connection pool wait", s["summary"])      # correlated log pattern
        self.assertTrue(1 <= len(s["actions"]) <= 4)

    def test_rule_summary_all_clear(self):
        s = rule_summary({"alerts": [], "predictions": [], "kpis": {"health": 100}, "now": 0})
        self.assertEqual((s["severity"], s["title"], s["actions"]), ("ok", "All clear", []))

    def test_claude_request_shape_and_parsing(self):
        reply = json.dumps({"title": "DB pool exhaustion imminent", "severity": "critical",
                            "summary": "The DB pool will be exhausted in about 1h 40m.", "actions": ["Restart", "Roll back"]})
        fake = FakeAnthropic("```json\n" + reply + "\n```")
        out = ClaudeSummarizer(settings(llm_mode="claude"), fake).summarize(self.analysis)
        self.assertEqual(out["source"], "claude")
        self.assertEqual(out["actions"], ["Restart", "Roll back"])
        call = fake.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-5-5")
        self.assertEqual(call["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(call["output_config"], {"effort": "low"})
        self.assertNotIn("thinking", call)
        snapshot = json.loads(call["messages"][0]["content"].split("\n", 1)[1])
        self.assertEqual(len(snapshot["predictions"]), 2)

    def test_refusal_and_bad_json_raise(self):
        with self.assertRaises(SummaryError):
            ClaudeSummarizer(settings(), FakeAnthropic("{}", stop_reason="refusal")).summarize(self.analysis)
        with self.assertRaises(SummaryError):
            ClaudeSummarizer(settings(), FakeAnthropic("not json at all")).summarize(self.analysis)

    def test_summarizer_falls_back_to_rules(self):
        fake = FakeAnthropic(error=ConnectionError("network down"))
        out = Summarizer(settings(llm_mode="claude"), fake).summarize(self.analysis)
        self.assertEqual(out["source"], "rules")
        self.assertIn("ConnectionError", out["fallback_reason"])
        # without a key, auto mode never calls the client
        untouched = FakeAnthropic("{}")
        out = Summarizer(settings(llm_mode="auto", anthropic_api_key=""), untouched).summarize(self.analysis)
        self.assertEqual((out["source"], untouched.calls), ("rules", []))


if __name__ == "__main__":
    unittest.main()
