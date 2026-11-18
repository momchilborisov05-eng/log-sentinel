"""Settings (read once from the environment) and the metric catalogue."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

DEFAULT_MODEL = "claude-sonnet-5-5"


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class MetricSpec:
    """What the detectors and the forecaster need to know about one metric."""

    name: str
    label: str
    unit: str
    threshold: float | None = None       # failure level the forecaster projects towards
    threshold_label: str = ""            # human wording for the failure ("pool exhausted")
    direction: str = "up"                # "up": high values are bad; "both": any deviation
    decimals: int = 1
    floor: float | None = None           # physical lower bound (0 for percentages)
    cap: float | None = None             # physical upper bound (100 for percentages)
    runbook: str = ""


METRICS: dict[str, MetricSpec] = {
    m.name: m
    for m in (
        MetricSpec("cpu_pct", "CPU", "%", 95.0, "CPU saturated", floor=0, cap=100,
                   runbook="Check for hot loops or a traffic surge; scale out the api deployment or shed load."),
        MetricSpec("memory_pct", "Memory", "%", 95.0, "out of memory (OOM kill)", floor=0, cap=100,
                   runbook="Heap is growing without being released: take a heap dump, then roll back the last deploy or schedule a restart before the OOM kill."),
        MetricSpec("latency_p95_ms", "Latency p95", "ms", 1500.0, "latency SLO breached", decimals=0, floor=0,
                   runbook="Look at the slowest downstream calls in traces; check timeouts and retries to dependencies."),
        MetricSpec("error_rate_pct", "Error rate", "%", 5.0, "error budget burning", decimals=2, floor=0, cap=100,
                   runbook="Correlate with the newest log templates and the last deploy; roll back if errors started with it."),
        MetricSpec("db_connections", "DB connections", "", 100.0, "DB connection pool exhausted", decimals=0, floor=0,
                   runbook="Connections are leaking or queries hold them too long: check for unclosed sessions and long transactions, raise the pool size only as a stopgap."),
    )
}


def spec_for(metric: str) -> MetricSpec:
    """Known metrics get their catalogue entry; ingested unknown metrics get detection-only defaults."""
    return METRICS.get(metric) or MetricSpec(metric, metric, "", None, direction="both")


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    llm_mode: str = "auto"               # auto (Claude when a key is set) | rules | claude
    db_path: str = "logsentinel.db"
    seed_demo_data: bool = True          # generate synthetic data when the database is empty
    demo_days: int = 7
    step_seconds: int = 300              # metric resolution of the synthetic data
    z_threshold: float = 4.0             # anomaly score needed to flag a point
    forecast_window_hours: float = 6.0   # history the trend forecaster fits on
    forecast_horizon_hours: float = 48.0  # predictions further out than this are not reported
    log_level: str = "INFO"
    log_json: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            anthropic_api_key=e.get("ANTHROPIC_API_KEY", ""),
            model=e.get("CLAUDE_MODEL") or DEFAULT_MODEL,
            llm_mode=(e.get("SENTINEL_LLM") or "auto").lower(),
            db_path=e.get("SENTINEL_DB") or "logsentinel.db",
            seed_demo_data=_bool(e.get("SENTINEL_SEED_DEMO"), True),
            demo_days=int(_float(e.get("SENTINEL_DEMO_DAYS"), 7)),
            z_threshold=_float(e.get("SENTINEL_Z_THRESHOLD"), 4.0),
            forecast_window_hours=_float(e.get("SENTINEL_FORECAST_WINDOW_HOURS"), 6.0),
            forecast_horizon_hours=_float(e.get("SENTINEL_FORECAST_HORIZON_HOURS"), 48.0),
            log_level=(e.get("SENTINEL_LOG_LEVEL") or "INFO").upper(),
            log_json=_bool(e.get("SENTINEL_LOG_JSON"), True),
        )

    def with_(self, **changes) -> Settings:
        return replace(self, **changes)

    @property
    def llm_enabled(self) -> bool:
        if self.llm_mode == "rules":
            return False
        if self.llm_mode == "claude":
            return True
        return bool(self.anthropic_api_key)
