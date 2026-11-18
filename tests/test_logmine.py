import unittest

from logsentinel.logmine import TemplateMiner, mask, poisson_sf, spike_scores, template_signals


class MaskingAndMiningTests(unittest.TestCase):
    def test_mask_replaces_variables_but_keeps_identifiers(self):
        self.assertEqual(mask("User 4821 logged in from 10.0.3.7"), "User <NUM> logged in from <IP>")
        self.assertEqual(mask("job 3f2b8c1e-9a4d-4e7b-8c11-2a9f00d1b6e3 done"), "job <UUID> done")
        self.assertIn("eu-west-1", mask("Cache hit 93.1% for eu-west-1"))
        self.assertIn("v2.14.0", mask("Deployment v2.14.0 rolled out to 6 pods"))

    def test_similar_lines_merge_into_one_template_with_wildcards(self):
        m = TemplateMiner()
        a = m.add("Worker pool saturated: 120 tasks queued in shard alpha", 1.0)
        b = m.add("Worker pool saturated: 87 tasks queued in shard beta", 2.0)
        self.assertEqual(a.change, "new")
        self.assertEqual(b.change, "updated")
        self.assertEqual(a.template.id, b.template.id)
        self.assertEqual(b.template.text, "Worker pool saturated: <NUM> tasks queued in shard <*>")
        self.assertEqual(b.template.count, 2)
        self.assertEqual(b.template.last_seen, 2.0)

    def test_different_messages_get_different_templates(self):
        m = TemplateMiner()
        ids = {m.add(msg).template.id for msg in [
            "GET /api/orders/1 200 in 12ms", "GET /api/orders/9 200 in 50ms",
            "Unhandled NullPointerException in OrderController.checkout at line 212",
            "Connection pool wait 300ms, active=80 idle=0 max=100"]}
        self.assertEqual(len(ids), 3)
        self.assertIsNotNone(m.match("GET /api/orders/77 200 in 9ms"))

    def test_load_restores_state(self):
        m = TemplateMiner()
        t = m.add("Payment authorized for order 1 amount 2.50 EUR").template
        m2 = TemplateMiner()
        m2.load(list(m.templates.values()))
        res = m2.add("Payment authorized for order 7 amount 9.99 EUR")
        self.assertEqual(res.template.id, t.id)
        self.assertNotEqual(m2.add("something completely different here").template.id, t.id)


class SignalTests(unittest.TestCase):
    def test_poisson_tail(self):
        self.assertEqual(poisson_sf(0, 3.0), 1.0)
        self.assertAlmostEqual(poisson_sf(1, 2.0), 1 - 2.718281828 ** -2, places=6)
        self.assertLess(poisson_sf(20, 2.0), 1e-9)

    def test_spike_scores_use_busy_hour_rate(self):
        counts = [1, 2, 0, 3, 1, 2, 4, 1, 2, 1] * 10 + [30]
        z, lam, p = spike_scores(counts)[-1]
        self.assertGreaterEqual(lam, 3.0)       # 90th percentile, not the mean (~1.7)
        self.assertLess(p, 1e-6)
        self.assertGreater(z, 10)

    def test_new_and_spiking_templates(self):
        start, day = 0.0, 86_400.0
        normal = [i * 600.0 for i in range(int(3 * day / 600))]                 # steady 6/h for 3 days
        burst = [2.5 * day + i * 20 for i in range(40)]                         # 40 lines in ~13 minutes
        occ = {1: sorted(normal + burst), 2: [10.0, 20.0], 3: [2 * day + 5.0, 2 * day + 30.0]}
        sig = template_signals(occ, start=start, end=3 * day)
        kinds = {(s.template_id, s.kind) for s in sig}
        self.assertIn((1, "spike"), kinds)
        self.assertIn((3, "new"), kinds)
        self.assertNotIn((2, "new"), kinds, "templates seen during warm-up are not 'new'")
        self.assertEqual(sum(1 for s in sig if s.template_id == 1 and s.kind == "spike"), 1)


if __name__ == "__main__":
    unittest.main()
