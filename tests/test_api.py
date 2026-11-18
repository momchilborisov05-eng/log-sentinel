import time
import unittest

from fastapi.testclient import TestClient

from logsentinel.web import create_app

from .helpers import loaded_service, settings


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(), service=loaded_service())
        cls.client = TestClient(cls.app)

    def test_health_and_index(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(r.json()["llm"], "rules")
        self.assertTrue(r.headers["x-request-id"])
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Log Sentinel", page.text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_dashboard_endpoints(self):
        a = self.client.get("/api/analysis").json()
        self.assertEqual(len(a["predictions"]), 2)
        metrics = self.client.get("/api/metrics").json()["metrics"]
        self.assertEqual([m["metric"] for m in metrics][:2], ["cpu_pct", "memory_pct"])
        s = self.client.get("/api/series", params={"metric": "db_connections", "hours": 12}).json()
        self.assertTrue(s["forecast"])
        t = self.client.get("/api/templates", params={"limit": 3, "sort": "count"}).json()["templates"]
        self.assertEqual(len(t), 3)
        logs = self.client.get("/api/logs", params={"template_id": t[0]["id"], "limit": 4}).json()["logs"]
        self.assertEqual(len(logs), 4)
        self.assertTrue(all(row["template_id"] == t[0]["id"] for row in logs))
        cfg = self.client.get("/api/config").json()
        self.assertEqual(cfg["metrics"]["memory_pct"]["threshold"], 95.0)
        self.assertEqual(len(cfg["demo"]["incidents"]), 5)
        summary = self.client.post("/api/summary").json()
        self.assertEqual(summary["source"], "rules")

    def test_ingest_round_trip(self):
        now = time.time()
        r = self.client.post("/api/ingest/metrics", json={"points": [
            {"ts": now - 600, "metric": "queue.depth", "value": 3, "host": "w1"},
            {"ts": now - 300, "metric": "queue.depth", "value": 4}]})
        self.assertEqual((r.status_code, r.json()), (202, {"accepted": 2}))
        r = self.client.post("/api/ingest/logs", json={"lines": [
            {"ts": now, "message": "Kafka consumer lag 1200 on partition 3", "level": "warning", "service": "worker"}]})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["accepted"], 1)
        self.assertEqual(len(r.json()["new_templates"]), 1)
        self.assertEqual(self.client.get("/api/logs", params={"limit": 1}).json()["logs"][0]["level"], "WARN")
        names = [m["metric"] for m in self.client.get("/api/metrics").json()["metrics"]]
        self.assertIn("queue.depth", names)

    def test_validation_errors_are_json_422_and_404(self):
        bad = [
            {"points": []},
            {"points": [{"ts": time.time(), "metric": "CPU Usage", "value": 1}]},
            {"points": [{"ts": time.time(), "metric": "cpu_pct", "value": "NaN"}]},
            {"points": [{"ts": time.time() * 1000, "metric": "cpu_pct", "value": 1}]},
        ]
        for body in bad:
            r = self.client.post("/api/ingest/metrics", json=body)
            self.assertEqual(r.status_code, 422, body)
            self.assertEqual(r.json()["error"], "validation_error")
        r = self.client.post("/api/ingest/logs", json={"lines": [{"ts": time.time(), "message": "x", "level": "LOUD"}]})
        self.assertEqual(r.status_code, 422)
        too_many = {"points": [{"ts": time.time(), "metric": "cpu_pct", "value": 1}] * 5001}
        self.assertEqual(self.client.post("/api/ingest/metrics", json=too_many).status_code, 422)
        r = self.client.get("/api/series", params={"metric": "unknown_metric"})
        self.assertEqual((r.status_code, r.json()["error"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
