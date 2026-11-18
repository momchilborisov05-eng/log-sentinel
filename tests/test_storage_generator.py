import os
import tempfile
import unittest

from logsentinel.generator import generate, traffic
from logsentinel.logmine import Template
from logsentinel.storage import Store

from .helpers import END, dataset


class StoreTests(unittest.TestCase):
    def test_series_averages_hosts_and_filters_by_time(self):
        s = Store(":memory:")
        s.insert_metrics([(100.0, "cpu_pct", 10.0, "a"), (100.0, "cpu_pct", 30.0, "b"), (200.0, "cpu_pct", 50.0, "a"),
                          (100.0, "memory_pct", 1.0, "a")])
        self.assertEqual(s.series("cpu_pct"), ([100.0, 200.0], [20.0, 50.0]))
        self.assertEqual(s.series("cpu_pct", since=150), ([200.0], [50.0]))
        self.assertEqual(s.metric_names(), ["cpu_pct", "memory_pct"])
        v1 = s.data_version()
        s.insert_logs([(150.0, "WARN", "api", "x", 1)])
        self.assertNotEqual(v1, s.data_version())
        self.assertEqual(s.counts(), {"metric_points": 4, "log_lines": 1, "templates": 0})
        self.assertEqual(s.time_range(), (100.0, 200.0))

    def test_templates_and_meta_persist_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "ls.db")
            s = Store(path)
            s.upsert_templates([Template(1, ["GET", "<NUM>"], 3, 1.0, 5.0)])
            s.upsert_templates([Template(1, ["GET", "<*>"], 4, 1.0, 6.0)])
            s.set_meta("k", "v")
            s.close()
            s2 = Store(path)
            [t] = s2.templates()
            self.assertEqual((t.text, t.count, t.last_seen), ("GET <*>", 4, 6.0))
            self.assertEqual(s2.get_meta("k"), "v")
            self.assertIsNone(s2.get_meta("missing"))
            s2.close()


class GeneratorTests(unittest.TestCase):
    def test_reproducible_and_complete(self):
        a, b = generate(end=END, days=2, seed=5), generate(end=END, days=2, seed=5)
        self.assertEqual(a.metrics[:50], b.metrics[:50])
        self.assertEqual(len(a.metrics), (2 * 288 + 1) * 5)
        self.assertNotEqual(generate(end=END, days=2, seed=6).metrics[:50], a.metrics[:50])
        self.assertTrue(all(a.start <= ts <= a.end for ts, *_ in a.logs))
        self.assertEqual([i.key for i in a.incidents],
                         ["payment_timeouts", "bad_deploy", "cpu_saturation", "memory_leak", "connection_leak"])

    def test_incidents_are_visible_in_the_data(self):
        ds = dataset()
        cpu = {ts: v for ts, m, v in ds.metrics if m == "cpu_pct"}
        inc = next(i for i in ds.incidents if i.key == "cpu_saturation")
        self.assertTrue(all(cpu[t] >= 93 for t in cpu if inc.start <= t <= inc.end))
        npe = [m for ts, lvl, svc, m in ds.logs if "NullPointerException" in m]
        bad = next(i for i in ds.incidents if i.key == "bad_deploy")
        self.assertGreater(len(npe), 50)
        self.assertTrue(all(bad.start <= ts <= bad.end + 300 for ts, _, _, m in ds.logs if "NullPointerException" in m))

    def test_traffic_curve_and_hidden_future(self):
        self.assertLess(traffic(END - (END % 86_400) + 4 * 3600), traffic(END - (END % 86_400) + 16 * 3600))
        ds = dataset(future_hours=24)
        self.assertTrue(all(ts > ds.end for ts, _, _ in ds.future))
        cross = ds.true_crossing("memory_pct", 95.0)
        self.assertIsNotNone(cross)
        self.assertLess(cross - ds.end, 6 * 3600)


if __name__ == "__main__":
    unittest.main()
