"""Anomaly detectors for metric time series.

Three causal detectors (each point is scored only against data before it, so a batch run over history gives the
same answer as scoring points one by one as they arrive):

* ``rolling_zscore``   - distance from the mean of a trailing window, in standard deviations.
* ``ewma_zscore``      - distance from an exponentially weighted mean/variance; adapts smoothly, reacts fast.
* ``seasonal_baseline`` + ``robust_residual_z`` - the value expected at this time of day (median of the same slot on
  previous days), and how far the point is from it in robust (MAD) units. This is what keeps the daily traffic
  peak from looking like an incident.

``detect`` combines them into an ensemble and groups flagged points into events.
"""

from __future__ import annotations

import math
from bisect import bisect_left, insort
from collections import deque
from dataclasses import asdict, dataclass
from statistics import median

from .config import MetricSpec

MAD_SCALE = 1.4826  # MAD -> standard deviation for normally distributed data


@dataclass(frozen=True)
class Anomaly:
    ts: float
    value: float
    expected: float | None
    score: float          # signed ensemble z-score (positive = above normal)


@dataclass(frozen=True)
class AnomalyEvent:
    metric: str
    start: float
    end: float
    peak_ts: float
    peak_value: float
    expected: float | None
    score: float          # largest |z| in the event
    points: int
    direction: str        # "up" | "down"

    def to_dict(self) -> dict:
        return asdict(self)


def _eps(values: list[float]) -> float:
    """Smallest standard deviation we trust: avoids infinite z-scores on perfectly flat series."""
    if not values:
        return 1e-9
    return max(1e-9, 0.002 * abs(median(values)))


def rolling_zscore(values: list[float], window: int = 48, min_periods: int = 12) -> list[float | None]:
    """z-score of each point against the mean/std of the ``window`` points before it."""
    out: list[float | None] = []
    buf: deque[float] = deque(maxlen=window)
    s = s2 = 0.0
    eps = _eps(values)
    for x in values:
        if len(buf) >= min_periods:
            n = len(buf)
            mean = s / n
            var = max(0.0, s2 / n - mean * mean)
            out.append((x - mean) / max(math.sqrt(var), eps))
        else:
            out.append(None)
        if len(buf) == window:
            old = buf[0]
            s -= old
            s2 -= old * old
        buf.append(x)
        s += x
        s2 += x * x
    return out


def ewma_zscore(values: list[float], alpha: float = 0.1, warmup: int = 12, clip: float = 6.0) -> list[float | None]:
    """z-score against an exponentially weighted moving mean and variance.

    Points far outside the band update the baseline with a clipped value, so one outlier does not inflate the
    variance and hide the next one.
    """
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    out: list[float | None] = []
    eps = _eps(values)
    mean: float | None = None
    var = 0.0
    for i, x in enumerate(values):
        if mean is None:
            mean = x
            out.append(None)
            continue
        sd = max(math.sqrt(var), eps)
        z = (x - mean) / sd
        out.append(z if i >= warmup else None)
        xu = mean + max(-clip, min(clip, z)) * sd if i >= warmup else x
        diff = xu - mean
        incr = alpha * diff
        mean += incr
        var = (1 - alpha) * (var + diff * incr)
    return out


def seasonal_baseline(
    timestamps: list[float],
    values: list[float],
    period: float = 86_400,
    step: float = 300,
    min_cycles: int = 2,
    max_cycles: int = 7,
    neighbour_slots: int = 1,
) -> list[float | None]:
    """Expected value of each point: the median of the same time-of-day slot (plus/minus ``neighbour_slots``) on
    previous cycles. ``None`` until at least ``min_cycles`` earlier cycles have data for that slot."""
    slots_per_cycle = max(1, round(period / step))
    history: dict[int, deque[tuple[float, float]]] = {}
    out: list[float | None] = []
    for ts, x in zip(timestamps, values, strict=True):
        slot = int(round((ts % period) / step)) % slots_per_cycle
        cutoff = ts - period / 2  # only earlier cycles, never the current one
        cands: list[float] = []
        cycles: set[int] = set()
        for d in range(-neighbour_slots, neighbour_slots + 1):
            for hts, hv in history.get((slot + d) % slots_per_cycle, ()):
                if hts <= cutoff:
                    cands.append(hv)
                    cycles.add(int((ts - hts + period / 2) // period))
        out.append(median(cands) if len(cycles) >= min_cycles else None)
        history.setdefault(slot, deque(maxlen=max_cycles)).append((ts, x))
    return out


def robust_residual_z(residuals: list[float | None], window: int = 288, min_periods: int = 24, eps: float = 1e-9) -> list[float | None]:
    """Scale each residual by the median absolute residual of the window before it (robust to the outliers we are
    looking for). Residuals against a seasonal median are centred on zero, so median(|r|) is the MAD."""
    out: list[float | None] = []
    hist: deque[float] = deque(maxlen=window)
    ordered: list[float] = []      # sorted copy of the absolute residuals in ``hist``
    for r in residuals:
        if r is None:
            out.append(None)
            continue
        if len(hist) >= min_periods:
            mad = ordered[len(ordered) // 2]
            out.append(r / max(MAD_SCALE * mad, eps))
        else:
            out.append(None)
        if len(hist) == window:
            old = abs(hist[0])
            ordered.pop(_index(ordered, old))
        hist.append(r)
        insort(ordered, abs(r))
    return out


def _index(sorted_list: list[float], x: float) -> int:
    i = bisect_left(sorted_list, x)
    return i if i < len(sorted_list) else len(sorted_list) - 1


@dataclass(frozen=True)
class DetectionResult:
    scores: list[float | None]
    expected: list[float | None]
    anomalies: list[Anomaly]
    events: list[AnomalyEvent]


def detect(
    timestamps: list[float],
    values: list[float],
    spec: MetricSpec,
    *,
    z_threshold: float = 4.0,
    step: float | None = None,
    min_points: int = 2,
    max_gap_steps: int = 3,
) -> DetectionResult:
    """Ensemble detector.

    Each point gets up to three z-scores (seasonal residual, EWMA, rolling). It is anomalous when the seasonal
    detector and at least one of the adaptive ones agree beyond ``z_threshold`` (or, before a seasonal baseline
    exists, when both adaptive ones agree), or when any single detector is beyond ``2 x z_threshold``.
    Flagged points within ``max_gap_steps`` of each other form an event; events need ``min_points`` points unless
    their peak is beyond ``3 x z_threshold`` - this drops most one-sample blips (the "for: 10m" rule of alerting).
    """
    n = len(values)
    if n != len(timestamps):
        raise ValueError("timestamps and values differ in length")
    if n == 0:
        return DetectionResult([], [], [], [])
    if step is None:
        diffs = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False) if b > a]
        step = median(diffs) if diffs else 60.0

    eps = _eps(values)
    expected = seasonal_baseline(timestamps, values, step=step)
    residuals = [None if e is None else v - e for v, e in zip(values, expected, strict=True)]
    z_seasonal = robust_residual_z(residuals, window=max(24, int(86_400 / step)), eps=eps)
    z_ewma = ewma_zscore(values, alpha=0.1)
    z_roll = rolling_zscore(values, window=max(12, int(4 * 3600 / step)))

    def bad(z: float | None) -> float | None:
        if z is None:
            return None
        return max(z, 0.0) if spec.direction == "up" else z

    scores: list[float | None] = []
    anomalies: list[Anomaly] = []
    for i in range(n):
        zs, ze, zr = bad(z_seasonal[i]), bad(z_ewma[i]), bad(z_roll[i])
        avail = [z for z in (zs, ze, zr) if z is not None]
        if not avail:
            scores.append(None)
            continue
        score = max(avail, key=abs)
        if zs is not None:
            agree = abs(zs) >= z_threshold and any(z is not None and abs(z) >= z_threshold and z * zs > 0 for z in (ze, zr))
        else:
            agree = ze is not None and zr is not None and abs(ze) >= z_threshold and abs(zr) >= z_threshold and ze * zr > 0
        strong = abs(score) >= 2 * z_threshold
        scores.append(score)
        if agree or strong:
            exp = expected[i]
            anomalies.append(Anomaly(timestamps[i], values[i], exp, round(score, 3)))

    events = group_events(spec.name, anomalies, step=step, max_gap_steps=max_gap_steps,
                          min_points=min_points, strong=3 * z_threshold)
    return DetectionResult(scores, expected, anomalies, events)


def group_events(metric: str, anomalies: list[Anomaly], *, step: float, max_gap_steps: int = 2,
                 min_points: int = 2, strong: float = 8.0) -> list[AnomalyEvent]:
    events: list[AnomalyEvent] = []
    run: list[Anomaly] = []

    def flush() -> None:
        if not run:
            return
        peak = max(run, key=lambda a: abs(a.score))
        if len(run) >= min_points or abs(peak.score) >= strong:
            events.append(AnomalyEvent(
                metric=metric, start=run[0].ts, end=run[-1].ts, peak_ts=peak.ts, peak_value=peak.value,
                expected=peak.expected, score=round(abs(peak.score), 2), points=len(run),
                direction="up" if peak.score >= 0 else "down",
            ))

    for a in anomalies:
        if run and (a.ts - run[-1].ts > (max_gap_steps + 0.5) * step or (a.score >= 0) != (run[-1].score >= 0)):
            flush()
            run = []
        run.append(a)
    flush()
    return events
