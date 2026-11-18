import time
import unittest

from logsentinel.evaluation import evaluate, to_markdown
from logsentinel.service import SentinelService, ValidationError
from logsentinel.storage import Store

from .helpers import dataset, loaded_service, settings


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = loaded_service()
        cls.report = cls.svc.analyze()

    @classmethod
    def tearDownClass(cls):
        cls.svc.close()

    def test_both_slow_burn_failures_are_predicted(self):
        metrics = {p["metric"] for p in self.report["predictions"]}
        self.assertEqual(metrics, {"memory_pct", "db_connections"})
        for p in self.report["predictions"]:
            self.assertLess(p["eta_seconds"], 6 * 3600)
            self.assertEqual(p["severity"], "critical")
            self.assertTrue(p["runbook"])

    def test_alerts_cover_every_kind_and_kpis_reflect_them(self):
        kinds = {a["kind"] for a in self.report["alerts"]}
        self.assertEqual(kinds, {"anomaly", "prediction", "log_new", "log_spike"})
        k = self.report["kpis"]
        self.assertLess(k["health"], 50)
        self.assertEqual(k["predicted_failures"], 2)
        self.assertTrue(k["next_failure"])
        self.assertEqual(k["open_alerts"], sum(1 for a in self.report["alerts"] if a["active"]))

    def test_analysis_is_cached_until_new_data_arrives(self):
        a1 = self.svc.analyze()
        self.assertIs(a1, self.svc.analyze())
        svc = loaded_service(days=3)
        before = svc.analyze()
        svc.ingest_metrics([{"ts": svc.store.time_range()[1] + 300, "metric": "cpu_pct", "value": 40.0}])
        self.assertIsNot(before, svc.analyze())
        svc.close()

    def test_series_and_templates_queries(self):
        s = self.svc.series("memory_pct", hours=24)
        self.assertGreater(len(s["points"]), 280)
        self.assertTrue(s["forecast"])
        self.assertTrue(s["anomaly_points"])
        self.assertIsNotNone(s["prediction"])
        with self.assertRaises(KeyError):
            self.svc.series("nope")
        rows = self.svc.templates_table(limit=5)
        self.assertEqual(len(rows), 5)
        self.assertIn("Connection pool wait", rows[0]["template"])
        self.assertIn("new", rows[0]["flags"])
        by_count = self.svc.templates_table(limit=3, sort="count")
        self.assertGreaterEqual(by_count[0]["count"], by_count[1]["count"])

    def test_ingest_validates_timestamps_and_mines_templates(self):
        svc = SentinelService(settings(), Store(":memory:"))
        now = time.time()
        with self.assertRaises(ValidationError):
            svc.ingest_metrics([{"ts": now * 1000, "metric": "cpu_pct", "value": 1.0}])   # milliseconds by mistake
        with self.assertRaises(ValidationError):
            svc.ingest_logs([{"ts": 12.0, "message": "x"}])
        r1 = svc.ingest_logs([{"ts": now, "message": "Disk /dev/sda1 at 91% on node 4", "level": "warn"}])
        r2 = svc.ingest_logs([{"ts": now, "message": "Disk /dev/sda1 at 97% on node 7"}])
        self.assertEqual(len(r1["new_templates"]), 1)
        self.assertEqual(r2["new_templates"], [])
        self.assertEqual(svc.store.recent_logs(1)[0]["template_id"], r1["new_templates"][0]["id"])
        svc.close()


class EvaluationTests(unittest.TestCase):
    def test_quality_floor_on_the_reference_dataset(self):
        r = evaluate(dataset(future_hours=48))
        s = r["scores"]
        self.assertEqual(s["all"]["recall"], 1.0)
        self.assertGreaterEqual(s["all"]["precision"], 0.85)
        self.assertGreaterEqual(s["metric"]["precision"], 0.8)
        self.assertEqual(s["trend"]["recall"], 1.0)
        self.assertEqual(r["forecast_made"], r["forecast_total"])
        self.assertGreaterEqual(r["forecast_within_band"], r["forecast_total"] - 1)
        self.assertLess(r["forecast_mae_min"], 90)
        md = to_markdown(r)
        self.assertIn("| Combined |", md)
        self.assertIn("Database connection leak", md)


if __name__ == "__main__":
    unittest.main()
