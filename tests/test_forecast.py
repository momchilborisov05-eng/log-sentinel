import math
import random
import unittest

from logsentinel.forecast import fit_holt, humanize_duration, linear_fit, seasonal_offsets, time_to_threshold

from .helpers import END


class LinearAndHoltTests(unittest.TestCase):
    def test_linear_fit_recovers_slope(self):
        xs = list(range(20))
        fit = linear_fit(xs, [3 + 2 * x for x in xs])
        self.assertAlmostEqual(fit.slope, 2.0)
        self.assertAlmostEqual(fit.intercept, 3.0)
        self.assertAlmostEqual(fit.r2, 1.0)
        with self.assertRaises(ValueError):
            linear_fit([1, 1, 1], [1, 2, 3])
        with self.assertRaises(ValueError):
            linear_fit([1, 2], [1, 2])

    def test_holt_tracks_a_linear_trend(self):
        vals = [10 + 0.5 * i for i in range(60)]
        m = fit_holt(vals)
        self.assertAlmostEqual(m.trend, 0.5, places=3)
        self.assertAlmostEqual(m.forecast(10), vals[-1] + 5, places=2)
        self.assertGreater(m.stderr(20), m.stderr(1) - 1e-12)


class TimeToThresholdTests(unittest.TestCase):
    def _leak(self, slope_per_hour=4.0, noise=0.5, hours=6, start=50.0, seed=0):
        rng = random.Random(seed)
        n = int(hours * 12)
        ts = [END - (n - i) * 300 for i in range(n)]
        vals = [start + slope_per_hour * (i * 300 / 3600) + rng.gauss(0, noise) for i in range(n)]
        return ts, vals

    def test_eta_matches_the_true_crossing(self):
        ts, vals = self._leak(slope_per_hour=4.0, start=50.0)
        p = time_to_threshold(ts, vals, 95.0, metric="memory_pct")
        self.assertIsNotNone(p)
        true_eta = (95.0 - (50.0 + 4.0 * 6)) / 4.0 * 3600   # ~5.25h
        self.assertLess(abs(p.eta_seconds - true_eta), 45 * 60)
        self.assertLessEqual(p.eta_low, true_eta)
        self.assertTrue(p.eta_high is None or p.eta_high >= true_eta)
        self.assertIn(p.confidence_label, ("high", "medium"))
        self.assertTrue(p.forecast and p.forecast[0]["ts"] > ts[-1])
        self.assertTrue(all(f["lo"] <= f["value"] <= f["hi"] for f in p.forecast))

    def test_no_prediction_for_flat_noisy_or_already_breached_series(self):
        ts, flat = self._leak(slope_per_hour=0.0, noise=1.0)
        self.assertIsNone(time_to_threshold(ts, flat, 95.0))
        ts, down = self._leak(slope_per_hour=-3.0)
        self.assertIsNone(time_to_threshold(ts, down, 95.0))
        ts, over = self._leak(slope_per_hour=2.0, start=99.0)
        self.assertIsNone(time_to_threshold(ts, over, 95.0))
        self.assertIsNone(time_to_threshold(ts[:5], over[:5], 95.0))

    def test_crossing_beyond_horizon_is_not_reported(self):
        ts, vals = self._leak(slope_per_hour=0.5, noise=0.05, start=20.0)
        self.assertIsNone(time_to_threshold(ts, vals, 95.0, horizon_seconds=24 * 3600))

    def test_seasonal_offsets_remove_a_daily_ramp(self):
        step = 300
        n = 4 * 288
        ts = [END - (n - i) * step for i in range(n)]
        vals = [50 + 20 * math.sin(2 * math.pi * (t % 86_400) / 86_400) for t in ts]
        off = seasonal_offsets(ts, vals, step=step)
        resid = [v - off(t) for t, v in zip(ts, vals, strict=True)]
        self.assertLess(max(resid) - min(resid), 1.0)
        # morning ramp of a seasonal metric must not become a "time to failure"
        window = slice(n - 72, n)
        self.assertIsNone(time_to_threshold(ts[window], vals[window], 75.0, offset=off))
        self.assertEqual(seasonal_offsets(ts[:100], vals[:100])(ts[0]), 0.0)

    def test_humanize_duration(self):
        self.assertEqual(humanize_duration(25 * 60), "25m")
        self.assertEqual(humanize_duration(3 * 3600 + 5 * 60), "3h 05m")
        self.assertEqual(humanize_duration(2 * 3600), "2h")
        self.assertEqual(humanize_duration(52 * 3600), "2d 4h")
        self.assertEqual(humanize_duration(None), "n/a")


if __name__ == "__main__":
    unittest.main()
