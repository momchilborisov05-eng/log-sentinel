"""Synthetic but realistic telemetry for one web service ("checkout-api"), with labelled incidents.

Seven days at 5-minute resolution by default. Traffic follows a daily curve (quiet at 04:00, peak around 16:00,
weekends at 80%) and every metric is driven by it plus noise, so the detectors have to learn seasonality instead of
flagging every afternoon. On top of that sit five incidents with known start/end times (the ground truth the
benchmark scores against), a handful of harmless one-sample blips, and a routine deploy that adds a new but benign
log template. The last two incidents are still developing when the data ends: a memory leak and a database
connection leak. ``future_hours`` keeps generating past the end (not stored) so a forecast can be checked against
what "actually" happened.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

SERVICE = "checkout-api"
STEP = 300


@dataclass(frozen=True)
class Incident:
    key: str
    title: str
    start: float
    end: float                 # for ongoing incidents: the end of the data
    metrics: tuple[str, ...]
    ongoing: bool = False
    log_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dataset:
    start: float
    end: float
    step: int
    metrics: list[tuple[float, str, float]]
    logs: list[tuple[float, str, str, str]]          # ts, level, service, message
    incidents: list[Incident]
    future: list[tuple[float, str, float]] = field(default_factory=list)
    blips: list[tuple[float, str]] = field(default_factory=list)

    def true_crossing(self, metric: str, threshold: float) -> float | None:
        """First time (in the hidden future) the metric reaches ``threshold``."""
        for ts, m, v in self.future:
            if m == metric and v >= threshold:
                return ts
        return None


def traffic(ts: float) -> float:
    """0.3 at 04:00 UTC, 1.0 at 16:00 UTC; weekends at 80%."""
    dt = datetime.fromtimestamp(ts, UTC)
    hour = dt.hour + dt.minute / 60
    base = 0.3 + 0.7 * (0.5 - 0.5 * math.cos(2 * math.pi * (hour - 4) / 24))
    return base * (0.8 if dt.weekday() >= 5 else 1.0)


_NORMAL_LOGS: list[tuple[float, str, str]] = [
    # weight, level, template (filled by _fill)
    (5.0, "INFO", "GET /api/orders/{oid} 200 in {ms}ms"),
    (2.0, "INFO", "POST /api/checkout 201 in {ms2}ms"),
    (2.0, "INFO", "User {uid} logged in from {ip}"),
    (1.0, "INFO", "Cache hit ratio {pct}% for region eu-west-1"),
    (1.0, "INFO", "Background job {uuid} completed in {ms}ms"),
    (1.2, "INFO", "Payment authorized for order {oid} amount {amt} EUR"),
    (1.0, "DEBUG", "Health check OK (db={small}ms, cache={small}ms)"),
    (0.35, "INFO", "GC pause {gc}ms (heap {heap}% used)"),
    (0.25, "WARN", "Slow query on orders table took {slow}ms"),
    (0.04, "WARN", "Timeout calling payment-service after 3000ms (attempt {att}/3)"),
]


def _fill(rng: random.Random, template: str, extra: dict | None = None) -> str:
    values = {
        "oid": rng.randint(100_000, 999_999),
        "ms": rng.randint(12, 180),
        "ms2": rng.randint(120, 600),
        "uid": rng.randint(1000, 99_999),
        "ip": f"10.{rng.randint(0, 3)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "pct": f"{rng.uniform(88, 97):.1f}",
        "uuid": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "amt": f"{rng.uniform(5, 400):.2f}",
        "small": rng.randint(1, 9),
        "gc": rng.randint(15, 60),
        "heap": rng.randint(45, 62),
        "slow": rng.randint(800, 2400),
        "att": rng.randint(1, 3),
    }
    if extra:
        values.update(extra)
    return template.format(**values)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def default_end(step: int = STEP) -> float:
    now = datetime.now(UTC).timestamp()
    return float(int(now // step) * step)


def generate(
    end: float | None = None,
    days: int = 7,
    step: int = STEP,
    seed: int = 42,
    future_hours: float = 0.0,
    log_scale: float = 1.0,
) -> Dataset:
    """Build the dataset. ``end`` defaults to "now" (rounded down to ``step``) so the demo always looks current;
    pass a fixed ``end`` for reproducible tests and benchmarks."""
    rng = random.Random(seed)
    end = float(end if end is not None else default_end(step))
    end = float(int(end // step) * step)
    start = end - days * 86_400
    day = 86_400.0

    def at(d: float, h: float) -> float:
        return start + d * day + h * 3600

    # Incident windows. Positions scale with ``days`` so short datasets (tests) still contain them.
    span = end - start
    inc = [
        Incident("payment_timeouts", "Payment provider slowdown: latency and timeouts",
                 at(0, 0) + 0.24 * span, at(0, 0) + 0.24 * span + 45 * 60, ("latency_p95_ms", "error_rate_pct"),
                 log_hint="Timeout calling payment-service"),
        Incident("bad_deploy", "Bad deploy: NullPointerException in checkout",
                 at(0, 0) + 0.38 * span, at(0, 0) + 0.38 * span + 30 * 60, ("error_rate_pct", "latency_p95_ms"),
                 log_hint="NullPointerException"),
        Incident("cpu_saturation", "Runaway batch job saturates CPU",
                 at(0, 0) + 0.55 * span, at(0, 0) + 0.55 * span + 60 * 60, ("cpu_pct", "latency_p95_ms"),
                 log_hint="Worker pool saturated"),
        Incident("memory_leak", "Memory leak after v2.15.0 deploy",
                 end - 9 * 3600, end, ("memory_pct",), ongoing=True, log_hint="GC pause"),
        Incident("connection_leak", "Database connection leak",
                 end - 12 * 3600, end, ("db_connections",), ongoing=True, log_hint="Connection pool wait"),
    ]
    for i in inc[:3]:  # align to the grid
        object.__setattr__(i, "start", float(int(i.start // step) * step))
        object.__setattr__(i, "end", float(int(i.end // step) * step))
    by_key = {i.key: i for i in inc}

    def active(key: str, ts: float) -> bool:
        i = by_key[key]
        return i.start <= ts < i.end + (step if not i.ongoing else 1e12)

    # Harmless blips: one sample, moderate size. Not incidents; a good detector ignores them.
    blip_rng = random.Random(seed + 1)
    blips: list[tuple[float, str]] = []
    for _ in range(8):
        ts = start + step * blip_rng.randint(int(0.15 * span / step), int(0.95 * span / step))
        blips.append((ts, blip_rng.choice(["cpu_pct", "latency_p95_ms"])))
    blip_set = set(blips)

    deploy_ts = float(int((at(0, 0) + 0.7 * span) // step) * step)   # benign deploy (new log template)
    leak_deploy_ts = by_key["memory_leak"].start - 600

    metrics: list[tuple[float, str, float]] = []
    future: list[tuple[float, str, float]] = []
    logs: list[tuple[float, str, str, str]] = []
    weights = [w for w, _, _ in _NORMAL_LOGS]

    n_steps = int(span // step) + int(future_hours * 3600 // step)
    for k in range(n_steps + 1):
        ts = start + k * step
        is_future = ts > end
        tr = traffic(ts)
        cpu = 16 + 48 * tr + rng.gauss(0, 2.2)
        mem = 50 + 5 * tr + rng.gauss(0, 0.7)
        lat = 170 + 140 * tr * tr + rng.gauss(0, 10)
        err = 0.25 + 0.25 * tr + abs(rng.gauss(0, 0.06))
        dbc = 16 + 30 * tr + rng.gauss(0, 2.0)

        if active("payment_timeouts", ts):
            lat *= 3.4 + rng.uniform(-0.3, 0.3)
            err += 1.6 + rng.uniform(0, 0.6)
        if active("bad_deploy", ts):
            err += 6.5 + rng.uniform(-1, 1.5)
            lat *= 1.25
        if active("cpu_saturation", ts):
            cpu = 93 + rng.uniform(0, 6)
            lat *= 1.5
        leak = by_key["memory_leak"]
        if ts >= leak.start:
            mem += 3.6 * (ts - leak.start) / 3600
        conn = by_key["connection_leak"]
        if ts >= conn.start:
            dbc += 4.3 * (ts - conn.start) / 3600
        if (ts, "cpu_pct") in blip_set:
            cpu += 16
        if (ts, "latency_p95_ms") in blip_set:
            lat += 75

        row = [
            ("cpu_pct", min(100.0, max(0.0, cpu))),
            ("memory_pct", min(100.0, max(0.0, mem))),
            ("latency_p95_ms", max(20.0, lat)),
            ("error_rate_pct", max(0.0, err)),
            ("db_connections", float(min(100, max(0, round(dbc))))),   # pool max = 100
        ]
        target = future if is_future else metrics
        target.extend((ts, m, round(v, 3)) for m, v in row)
        if is_future:
            continue

        # ---- logs for this step
        def emit(level: str, msg: str, jitter: bool = True, ts: float = ts) -> None:
            logs.append((ts + (rng.uniform(0, step) if jitter else 0), level, SERVICE, msg))

        for _ in range(_poisson(rng, (1.0 + 5.0 * tr) * log_scale)):
            _, level, tpl = rng.choices(_NORMAL_LOGS, weights=weights)[0]
            emit(level, _fill(rng, tpl))
        if active("payment_timeouts", ts):
            for _ in range(_poisson(rng, 9 * log_scale)):
                emit("WARN", _fill(rng, "Timeout calling payment-service after 3000ms (attempt {att}/3)"))
            for _ in range(_poisson(rng, 3 * log_scale)):
                emit("ERROR", _fill(rng, "Checkout failed for order {oid}: upstream payment-service unavailable"))
        if active("bad_deploy", ts):
            for _ in range(_poisson(rng, 14 * log_scale)):
                emit("ERROR", _fill(rng, "Unhandled NullPointerException in OrderController.checkout at line 212 (request {uuid})"))
        if active("cpu_saturation", ts):
            for _ in range(_poisson(rng, 4 * log_scale)):
                emit("WARN", _fill(rng, "Worker pool saturated: {q} tasks queued", {"q": rng.randint(40, 400)}))
            if ts == by_key["cpu_saturation"].start:
                emit("INFO", "Batch job reindex-catalog started by scheduler", jitter=False)
        if ts >= leak.start:
            frac = (ts - leak.start) / (end - leak.start)
            for _ in range(_poisson(rng, (0.5 + 5 * frac) * log_scale)):
                emit("WARN", _fill(rng, "GC pause {gc}ms (heap {heap}% used)",
                                   {"gc": rng.randint(150, 400 + int(1400 * frac)), "heap": int(min(99, mem))}))
        if dbc > 58:
            for _ in range(_poisson(rng, (dbc - 58) / 6 * log_scale)):
                emit("WARN", _fill(rng, "Connection pool wait {w}ms, active={a} idle=0 max=100",
                                   {"w": rng.randint(200, 3000), "a": int(dbc)}))
        if ts == deploy_ts:
            emit("INFO", "Deployment v2.14.0 rolled out to 6 pods", jitter=False)
        if ts == float(int(leak_deploy_ts // step) * step):
            emit("INFO", "Deployment v2.15.0 rolled out to 6 pods", jitter=False)
        dt = datetime.fromtimestamp(ts, UTC)
        if dt.hour == 3 and dt.minute == 0:
            emit("INFO", f"Scheduled cleanup removed {rng.randint(200, 2000)} expired sessions", jitter=False)

    logs.sort(key=lambda r: r[0])
    logs = [r for r in logs if r[0] <= end]
    return Dataset(start, end, step, metrics, logs, inc, future, sorted(blips))
