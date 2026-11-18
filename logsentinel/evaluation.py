"""Score the detectors and the forecaster against the incidents injected by the generator.

Event-level scoring, the way an on-call engineer experiences alerts:

* An **alert event** is a grouped anomaly (metric detectors), a run of hourly "will cross its threshold within 24h"
  forecasts (trend detector), or a new/spiking log template (log detector).
* An alert is a **true positive** when it overlaps an incident window (from one step before the start to 30 minutes
  after the end). **Precision** = true-positive alerts / all alerts. **Recall** = incidents with at least one
  overlapping alert / all incidents. **Time to detect** = first overlapping alert start - incident start.
* **Forecast accuracy**: for the incidents still developing at the end of the data, the forecaster is run at several
  cut-off points (using only data up to the cut) and its ETA is compared with the time the metric really crosses
  its threshold in the generator's hidden future.

Everything is causal: each detector only looks at data before the point it scores, so these numbers are what the
system would have produced live.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, spec_for
from .forecast import humanize_duration
from .generator import Dataset, Incident
from .service import SentinelService
from .storage import Store

GRACE = 30 * 60


@dataclass(frozen=True)
class AlertEvent:
    family: str          # metric | trend | log
    label: str
    start: float
    end: float


def _overlaps(a: AlertEvent, inc: Incident, step: float) -> bool:
    return a.start <= inc.end + GRACE and a.end >= inc.start - step


def trend_events(svc: SentinelService, metric: str, ts: list[float], vals: list[float], step: float,
                 every_steps: int = 12, eta_limit: float = 24 * 3600, warmup: float = 3 * 86_400) -> list[AlertEvent]:
    """Re-run the forecaster every ``every_steps`` points over history; group consecutive "crosses within
    ``eta_limit``" hits into events. Low-confidence forecasts are shown on the dashboard but do not count as alerts,
    and the first ``warmup`` seconds are skipped: the daily profile needs a few cycles before it is trustworthy."""
    hits: list[float] = []
    for i in range(0, len(ts), every_steps):
        if ts[i] - ts[0] < warmup:
            continue
        p = svc.predict(metric, ts, vals, step=step, at_index=i)
        if p is not None and p.eta_seconds <= eta_limit and p.confidence_label != "low":
            hits.append(ts[i])
    events: list[AlertEvent] = []
    for t in hits:
        if events and t - events[-1].end <= every_steps * step * 1.5:
            events[-1] = AlertEvent("trend", events[-1].label, events[-1].start, t)
        else:
            events.append(AlertEvent("trend", f"trend:{metric}", t, t))
    return events


def evaluate(ds: Dataset, settings: Settings | None = None, *, backtest_hours: tuple[float, ...] = (3, 2, 1, 0)) -> dict:
    settings = (settings or Settings()).with_(db_path=":memory:", seed_demo_data=False)
    svc = SentinelService(settings, Store(":memory:"))
    try:
        svc.load_dataset(ds)
        report, per_metric = svc._analysis()

        events: list[AlertEvent] = []
        for m, a in per_metric.items():
            events += [AlertEvent("metric", f"anomaly:{m}", e.start, e.end) for e in a.events]
            if spec_for(m).threshold is not None:
                events += trend_events(svc, m, a.timestamps, a.values, a.step)
        for s in svc.log_signals(ds.start, ds.end):
            events.append(AlertEvent("log", f"log_{s.kind}:{s.template_id}", s.ts, s.ts + (900 if s.kind == "spike" else 0)))

        families = {"metric": "Metric anomalies (ensemble)", "trend": "Trend forecasts (Holt, ETA <= 24h; gradual incidents)",
                    "log": "Log patterns (new / spiking)", "all": "Combined"}
        scores = {}
        for fam in families:
            evs = [e for e in events if fam == "all" or e.family == fam]
            tp = [e for e in evs if any(_overlaps(e, i, ds.step) for i in ds.incidents)]
            # Trend forecasting is only meant for slow-burn failures; sudden incidents are out of its scope.
            scope = [i for i in ds.incidents if i.ongoing] if fam == "trend" else ds.incidents
            detected = {}
            for inc in scope:
                hits = [e.start for e in evs if _overlaps(e, inc, ds.step)]
                if hits:
                    detected[inc.key] = max(0.0, min(hits) - inc.start)
            scores[fam] = {
                "name": families[fam], "alerts": len(evs), "true_positive_alerts": len(tp),
                "false_positive_alerts": len(evs) - len(tp),
                "precision": round(len(tp) / len(evs), 3) if evs else None,
                "recall": round(len(detected) / len(scope), 3), "in_scope": len(scope),
                "detected": sorted(detected),
                "time_to_detect_min": {k: round(v / 60, 1) for k, v in detected.items()},
            }
        false_positives = [e.label for e in events if not any(_overlaps(e, i, ds.step) for i in ds.incidents)]

        backtests = []
        for inc in ds.incidents:
            if not inc.ongoing:
                continue
            for metric in inc.metrics:
                spec = spec_for(metric)
                a = per_metric[metric]
                full = list(zip(a.timestamps, a.values, strict=True)) + [(t, v) for t, m, v in ds.future if m == metric]
                for h in backtest_hours:
                    cut_ts = ds.end - h * 3600
                    idx = max(i for i, t in enumerate(a.timestamps) if t <= cut_ts)
                    p = svc.predict(metric, a.timestamps, a.values, step=a.step, at_index=idx)
                    truth = next((t for t, v in full if t > a.timestamps[idx] and v >= spec.threshold), None)
                    true_eta = None if truth is None else truth - a.timestamps[idx]
                    row = {"incident": inc.key, "metric": metric, "hours_before_end": h,
                           "true_eta_min": None if true_eta is None else round(true_eta / 60),
                           "predicted_eta_min": None, "error_min": None, "within_band": None, "confidence": None}
                    if p is not None:
                        row.update(predicted_eta_min=round(p.eta_seconds / 60), confidence=p.confidence_label,
                                   band_min=[round(p.eta_low / 60), None if p.eta_high is None else round(p.eta_high / 60)])
                        if true_eta is not None:
                            row["error_min"] = round((p.eta_seconds - true_eta) / 60)
                            row["within_band"] = p.eta_low <= true_eta <= (p.eta_high or float("inf"))
                    backtests.append(row)
        errs = [abs(r["error_min"]) for r in backtests if r["error_min"] is not None]
        return {
            "incidents": [i.to_dict() for i in ds.incidents],
            "scores": scores,
            "false_positive_labels": false_positives,
            "forecast_backtest": backtests,
            "forecast_mae_min": round(sum(errs) / len(errs), 1) if errs else None,
            "forecast_within_band": sum(1 for r in backtests if r["within_band"]),
            "forecast_made": sum(1 for r in backtests if r["predicted_eta_min"] is not None),
            "forecast_total": len(backtests),
            "analysis_ms": report["analysis_ms"],
        }
    finally:
        svc.close()


def to_markdown(result: dict) -> str:
    lines = ["| Detector | Alerts | False positives | Precision | Recall (incidents) |", "|---|---:|---:|---:|---:|"]
    for s in result["scores"].values():
        prec = "n/a" if s["precision"] is None else f"{s['precision']:.2f}"
        lines.append(f"| {s['name']} | {s['alerts']} | {s['false_positive_alerts']} | {prec} | "
                     f"{s['recall']:.2f} ({len(s['detected'])}/{s['in_scope']}) |")
    lines += ["", "| Incident | First alert after start (combined) |", "|---|---:|"]
    ttd = result["scores"]["all"]["time_to_detect_min"]
    for inc in result["incidents"]:
        v = ttd.get(inc["key"])
        lines.append(f"| {inc['title']} | {'missed' if v is None else humanize_duration(v * 60)} |")
    lines += ["", "| Incident | Forecast made | True time to threshold | Predicted | Error | Inside 80% band |",
              "|---|---|---:|---:|---:|:---:|"]
    for r in result["forecast_backtest"]:
        def fmt(m):
            return "n/a" if m is None else humanize_duration(m * 60)
        err = "n/a" if r["error_min"] is None else f"{r['error_min']:+d}m"
        band = "n/a" if r["within_band"] is None else ("yes" if r["within_band"] else "no")
        lines.append(f"| {r['incident']} ({r['metric']}) | {r['hours_before_end']}h before end | {fmt(r['true_eta_min'])} | "
                     f"{fmt(r['predicted_eta_min'])} | {err} | {band} |")
    return "\n".join(lines)
