import random
import unittest

from logsentinel.config import spec_for
from logsentinel.detectors import detect, ewma_zscore, rolling_zscore, seasonal_baseline

from .helpers import seasonal_series


class RollingAndEwmaTests(unittest.TestCase):
    def test_rolling_zscore_is_causal_and_flags_a_jump(self):
        rng = random.Random(0)
        vals = [10 + rng.gauss(0, 1) for _ in range(100)] + [30.0]
        z = rolling_zscore(vals, window=48, min_periods=12)
        self.assertIsNone(z[5], "no score before min_periods")
        self.assertGreater(z[-1], 10)
        # causal: changing a future value never changes an earlier score
        z2 = rolling_zscore(vals[:-1] + [-500.0], window=48, min_periods=12)
        self.assertEqual(z[:-1], z2[:-1])

    def test_ewma_rejects_bad_alpha_and_clips_updates(self):
        with self.assertRaises(ValueError):
            ewma_zscore([1, 2, 3], alpha=0)
        vals = [5.0] * 40 + [5.5, 4.5] * 20 + [500.0] + [5.0] * 5
        z = ewma_zscore(vals, alpha=0.1)
        spike = 80
        self.assertGreater(z[spike], 6)
        # thanks to clipping, one huge outlier does not blind the detector for the following normal points
        self.assertLess(abs(z[spike + 2]), 4)


class SeasonalTests(unittest.TestCase):
    def test_seasonal_baseline_learns_the_daily_shape(self):
        ts, vals = seasonal_series(days=4, noise=0.5)
        exp = seasonal_baseline(ts, vals, step=300)
        self.assertTrue(all(e is None for e in exp[:200]), "first day has no history")
        errs = [abs(v - e) for v, e in zip(vals[-288:], exp[-288:], strict=True)]
        self.assertLess(sum(errs) / len(errs), 1.5)

    def test_daily_pattern_alone_produces_no_events(self):
        ts, vals = seasonal_series(days=5, noise=1.0, seed=3)
        res = detect(ts, vals, spec_for("cpu_pct"), z_threshold=4.0, step=300)
        self.assertEqual(res.events, [])

    def test_sustained_shift_is_one_event_and_single_blip_is_ignored(self):
        ts, vals = seasonal_series(days=5, noise=1.0, seed=4)
        vals = list(vals)
        vals[1000] += 9                      # one moderate blip
        for i in range(1300, 1312):          # one hour at +30
            vals[i] += 30
        res = detect(ts, vals, spec_for("cpu_pct"), z_threshold=4.0, step=300)
        self.assertEqual(len(res.events), 1)
        ev = res.events[0]
        self.assertLessEqual(ev.start, ts[1301])
        self.assertGreaterEqual(ev.end, ts[1308])
        self.assertGreater(ev.score, 4)
        self.assertEqual(ev.direction, "up")

    def test_direction_up_ignores_drops_for_known_metrics(self):
        ts, vals = seasonal_series(days=5, noise=1.0, seed=5)
        vals = list(vals)
        for i in range(1300, 1312):
            vals[i] -= 30
        self.assertEqual(detect(ts, vals, spec_for("cpu_pct"), step=300).events, [])
        # an unknown, ingested metric watches both directions
        self.assertEqual(len(detect(ts, vals, spec_for("queue.depth"), step=300).events), 1)

    def test_detect_validates_lengths(self):
        with self.assertRaises(ValueError):
            detect([1.0, 2.0], [1.0], spec_for("cpu_pct"))
        self.assertEqual(detect([], [], spec_for("cpu_pct")).events, [])


if __name__ == "__main__":
    unittest.main()
