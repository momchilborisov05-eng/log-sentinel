"""Trend forecasting and time-to-threshold estimates.

The question the dashboard answers is "if nothing changes, when does this metric hit its failure level, and how sure
are we?". The pipeline for one metric:

1. Remove the daily pattern (``seasonal_offsets``) so the morning traffic ramp is not mistaken for a leak.
2. Ordinary least squares on the de-seasonalised window gives the slope, its standard error and R^2. No forecast
   unless the upward trend is statistically clear (t-statistic of the slope >= ``min_t``).
3. Holt's linear exponential smoothing (level + trend, smoothing constants chosen by grid search on one-step-ahead
   error) gives the point forecast; its analytic forecast variance gives an 80% band.
4. The ETA is the first step where the forecast (seasonal pattern added back) reaches the threshold; the band edges
   give the optimistic / pessimistic ETAs. Confidence combines R^2 with how wide that ETA range is.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from statistics import median

Z80 = 1.2816  # two-sided 80% normal quantile


@dataclass(frozen=True)
class LinearFit:
    slope: float           # units per x unit
    intercept: float
    r2: float
    slope_stderr: float
    resid_std: float
    n: int

    @property
    def t_stat(self) -> float:
        if self.slope_stderr <= 0:
            return math.inf if self.slope > 0 else (-math.inf if self.slope < 0 else 0.0)
        return self.slope / self.slope_stderr


def linear_fit(xs: list[float], ys: list[float]) -> LinearFit:
    n = len(xs)
    if n != len(ys):
        raise ValueError("xs and ys differ in length")
    if n < 3:
        raise ValueError("need at least 3 points")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("xs are all equal")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else (1.0 if ss_res == 0 else 0.0)
    resid_var = ss_res / (n - 2)
    return LinearFit(slope, intercept, max(0.0, r2), math.sqrt(resid_var / sxx), math.sqrt(resid_var), n)


@dataclass(frozen=True)
class HoltModel:
    alpha: float
    beta: float
    level: float
    trend: float          # per step
    sigma: float          # std of one-step-ahead errors

    def forecast(self, h: int) -> float:
        return self.level + h * self.trend

    def stderr(self, h: int) -> float:
        """Analytic h-step forecast standard error of Holt's linear method:
        sigma^2 * (1 + sum_{j=1}^{h-1} (alpha * (1 + j * beta))^2)."""
        acc = 1.0
        for j in range(1, h):
            acc += (self.alpha * (1 + j * self.beta)) ** 2
        return self.sigma * math.sqrt(acc)


def holt_run(values: list[float], alpha: float, beta: float) -> tuple[float, float, list[float]]:
    """Run Holt's method; return the final level, trend and the one-step-ahead errors."""
    if len(values) < 2:
        raise ValueError("need at least 2 points")
    level, trend = values[0], values[1] - values[0]
    errors: list[float] = []
    for y in values[1:]:
        pred = level + trend
        errors.append(y - pred)
        new_level = alpha * y + (1 - alpha) * (level + trend)
        trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level
    return level, trend, errors


def fit_holt(values: list[float], alphas: tuple[float, ...] = (0.05, 0.1, 0.2, 0.3, 0.5, 0.7),
             betas: tuple[float, ...] = (0.01, 0.03, 0.05, 0.1, 0.2)) -> HoltModel:
    """Grid search for the smoothing constants with the smallest one-step-ahead squared error (after a short burn-in
    so the crude initial trend does not decide the winner)."""
    if len(values) < 4:
        raise ValueError("need at least 4 points")
    burn = min(len(values) // 4, 12)
    best: tuple[float, HoltModel] | None = None
    for a in alphas:
        for b in betas:
            level, trend, errs = holt_run(values, a, b)
            scored = errs[burn:] or errs
            sse = sum(e * e for e in scored)
            sigma = math.sqrt(sse / max(1, len(scored) - 2))
            if best is None or sse < best[0]:
                best = (sse, HoltModel(a, b, level, trend, sigma))
    assert best is not None
    return best[1]


def seasonal_offsets(timestamps: list[float], values: list[float], period: float = 86_400,
                     step: float = 300, min_cycles: int = 2) -> Callable[[float], float]:
    """Time-of-day profile: offset(ts) = median of the slot - overall median. Returns 0 everywhere when there are
    fewer than ``min_cycles`` cycles of history (nothing to learn a pattern from)."""
    if not timestamps or timestamps[-1] - timestamps[0] < min_cycles * period * 0.95:
        return lambda ts: 0.0
    slots = max(1, round(period / step))
    buckets: dict[int, list[float]] = {}
    for ts, v in zip(timestamps, values, strict=True):
        buckets.setdefault(int(round((ts % period) / step)) % slots, []).append(v)
    overall = median(values)
    profile = {s: median(vs) - overall for s, vs in buckets.items()}

    def offset(ts: float) -> float:
        s = int(round((ts % period) / step)) % slots
        if s in profile:
            return profile[s]
        near = [profile.get((s + d) % slots) for d in (-1, 1, -2, 2)]
        near = [x for x in near if x is not None]
        return near[0] if near else 0.0

    return offset


@dataclass
class Prediction:
    metric: str
    threshold: float
    current: float
    eta_seconds: float          # point estimate of time until the threshold is reached
    eta_low: float              # optimistic edge of the 80% band (reached sooner)
    eta_high: float | None      # pessimistic edge; None = beyond the horizon
    crossing_ts: float
    confidence: float           # 0..1
    confidence_label: str       # high | medium | low
    slope_per_hour: float       # de-seasonalised trend
    r2: float
    method: str = "holt"
    forecast: list[dict] = field(default_factory=list)   # [{ts, value, lo, hi}] for the chart

    def to_dict(self) -> dict:
        return asdict(self)


def _confidence(r2: float, eta: float, low: float, high: float | None, horizon: float) -> tuple[float, str]:
    span = ((high if high is not None else horizon) - low) / max(eta, 1.0)
    width_score = 1.0 / (1.0 + span)            # 1 when the ETA range is tight, -> 0 when it is wide
    conf = max(0.0, min(1.0, 0.55 * r2 + 0.45 * width_score))
    label = "high" if conf >= 0.7 else "medium" if conf >= 0.45 else "low"
    return round(conf, 3), label


def time_to_threshold(
    timestamps: list[float],
    values: list[float],
    threshold: float,
    *,
    metric: str = "",
    horizon_seconds: float = 48 * 3600,
    offset: Callable[[float], float] | None = None,
    min_t: float = 4.0,
    min_r2: float = 0.3,
    path_padding_seconds: float = 2 * 3600,
) -> Prediction | None:
    """Predict when ``values`` (sampled at ``timestamps``) first reach ``threshold``.

    Returns ``None`` when there is no clear upward trend, the metric is already past the threshold, or the crossing
    is further away than ``horizon_seconds``.
    """
    n = len(values)
    if n < 12 or n != len(timestamps):
        return None
    offset = offset or (lambda ts: 0.0)
    diffs = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False) if b > a]
    if not diffs:
        return None
    step = median(diffs)
    deseason = [v - offset(t) for t, v in zip(timestamps, values, strict=True)]
    hours = [(t - timestamps[0]) / 3600 for t in timestamps]
    lf = linear_fit(hours, deseason)
    if lf.slope <= 0 or lf.t_stat < min_t or lf.r2 < min_r2:
        return None
    current = values[-1]
    if current >= threshold:
        return None

    model = fit_holt(deseason)
    if model.trend <= 0:  # smoothing says the climb has stopped; trust the recent behaviour
        return None

    t0 = timestamps[-1]
    max_steps = int(horizon_seconds / step)
    eta = low = high = None
    path: list[dict] = []
    acc = 0.0  # running sum for HoltModel.stderr, so the loop stays O(h) instead of O(h^2)
    for h in range(1, max_steps + 1):
        ts = t0 + h * step
        mu = model.forecast(h) + offset(ts)
        acc += 1.0 if h == 1 else (model.alpha * (1 + (h - 1) * model.beta)) ** 2
        se = model.sigma * math.sqrt(acc)
        lo, hi = mu - Z80 * se, mu + Z80 * se
        if low is None and hi >= threshold:
            low = h * step
        if eta is None and mu >= threshold:
            eta = h * step
        if high is None and lo >= threshold:
            high = h * step
        path.append({"ts": ts, "value": round(mu, 3), "lo": round(lo, 3), "hi": round(hi, 3)})
        if high is not None and h * step >= high + path_padding_seconds:
            break
    if eta is None or low is None:
        return None
    end = (eta + path_padding_seconds)
    path = [p for p in path if p["ts"] - t0 <= end]
    conf, label = _confidence(lf.r2, eta, low, high, horizon_seconds)
    return Prediction(
        metric=metric, threshold=threshold, current=current, eta_seconds=eta, eta_low=low, eta_high=high,
        crossing_ts=t0 + eta, confidence=conf, confidence_label=label, slope_per_hour=round(lf.slope, 4),
        r2=round(lf.r2, 4), forecast=path,
    )


def humanize_duration(seconds: float | None) -> str:
    """3h 40m, 25m, 2d 4h."""
    if seconds is None:
        return "n/a"
    m = int(round(seconds / 60))
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m:02d}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"
