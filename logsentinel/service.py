"""Orchestration: ingestion, analysis (detectors + forecaster + template signals -> alerts) and dashboard queries.
The web layer, the CLI and the benchmark only talk to ``SentinelService``."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from statistics import median

from .config import METRICS, Settings, spec_for
from .detectors import AnomalyEvent, detect
from .forecast import Prediction, humanize_duration, seasonal_offsets, time_to_threshold
from .generator import Dataset, generate
from .logmine import TemplateMiner, TemplateSignal, template_signals
from .storage import Store

log = logging.getLogger("logsentinel.service")

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}
LOG_BUCKET = 900          # template-rate buckets: 15 minutes
ACTIVE_WINDOW = 3600      # an alert counts as "open" if it touched the last hour


class ValidationError(ValueError):
    """Bad input that passed schema validation (e.g. timestamps far in the future)."""


@dataclass
class MetricAnalysis:
    metric: str
    timestamps: list[float]
    values: list[float]
    expected: list[float | None]
    events: list[AnomalyEvent]
    prediction: Prediction | None
    step: float


def _fmt(value: float, metric: str) -> str:
    spec = spec_for(metric)
    unit = spec.unit
    txt = f"{value:,.{spec.decimals}f}"
    return f"{txt}{unit}" if unit == "%" else (f"{txt} {unit}" if unit else txt)


class SentinelService:
    def __init__(self, settings: Settings | None = None, store: Store | None = None):
        self.settings = settings or Settings.from_env()
        self.store = store or Store(self.settings.db_path)
        self.miner = TemplateMiner()
        self.miner.load(self.store.templates())
        self._ingest_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: tuple[str, dict, dict[str, MetricAnalysis]] | None = None

    # ------------------------------------------------------------------ lifecycle

    def startup(self) -> dict:
        counts = self.store.counts()
        seeded = False
        if counts["metric_points"] == 0 and self.settings.seed_demo_data:
            self.reset_demo()
            seeded = True
        return {"seeded": seeded, **self.store.counts()}

    def close(self) -> None:
        self.store.close()

    def reset_demo(self, end: float | None = None, seed: int = 42) -> dict:
        t0 = time.perf_counter()
        ds = generate(end=end, days=self.settings.demo_days, step=self.settings.step_seconds, seed=seed)
        self.load_dataset(ds)
        took = time.perf_counter() - t0
        log.info("demo data generated", extra={"metric_points": len(ds.metrics), "log_lines": len(ds.logs),
                                               "seconds": round(took, 3)})
        return {"metric_points": len(ds.metrics), "log_lines": len(ds.logs), "incidents": len(ds.incidents),
                "seconds": round(took, 3)}

    def load_dataset(self, ds: Dataset) -> None:
        with self._ingest_lock:
            self.store.clear()
            self.miner = TemplateMiner()
            self.store.insert_metrics((ts, m, v, "api-1") for ts, m, v in ds.metrics)
            rows = []
            for ts, level, service, msg in ds.logs:
                rows.append((ts, level, service, msg, self.miner.add(msg, ts).template.id))
            self.store.upsert_templates(self.miner.templates.values())
            self.store.insert_logs(rows)
            self.store.set_meta("demo", json.dumps({"incidents": [i.to_dict() for i in ds.incidents],
                                                    "start": ds.start, "end": ds.end}))
        self._invalidate()

    def _invalidate(self) -> None:
        with self._cache_lock:
            self._cache = None

    # ------------------------------------------------------------------ ingestion

    def _check_ts(self, ts: float) -> None:
        now = time.time()
        if ts > now + 86_400:
            raise ValidationError(f"timestamp {ts} is more than 24h in the future")
        if ts < 946_684_800:  # 2000-01-01: almost always a milliseconds/seconds mix-up or a zero
            raise ValidationError(f"timestamp {ts} is before 2000-01-01 (send unix seconds)")

    def ingest_metrics(self, points: list[dict]) -> int:
        rows = []
        for p in points:
            self._check_ts(p["ts"])
            rows.append((float(p["ts"]), p["metric"], float(p["value"]), p.get("host") or "api-1"))
        with self._ingest_lock:
            n = self.store.insert_metrics(rows)
        self._invalidate()
        return n

    def ingest_logs(self, lines: list[dict]) -> dict:
        for line in lines:
            self._check_ts(line["ts"])
        new: list[dict] = []
        with self._ingest_lock:
            rows, touched = [], {}
            for line in lines:
                res = self.miner.add(line["message"], float(line["ts"]))
                touched[res.template.id] = res.template
                if res.change == "new":
                    new.append({"id": res.template.id, "template": res.template.text})
                rows.append((float(line["ts"]), line.get("level", "INFO").upper(), line.get("service") or "unknown",
                             line["message"], res.template.id))
            self.store.upsert_templates(touched.values())
            self.store.insert_logs(rows)
        self._invalidate()
        return {"accepted": len(rows), "new_templates": new}

    # ------------------------------------------------------------------ analysis

    def analyze_metric(self, metric: str, timestamps: list[float], values: list[float]) -> MetricAnalysis:
        spec = spec_for(metric)
        diffs = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False) if b > a]
        step = median(diffs) if diffs else float(self.settings.step_seconds)
        res = detect(timestamps, values, spec, z_threshold=self.settings.z_threshold, step=step)
        prediction = None
        if spec.threshold is not None and len(values) >= 12:
            prediction = self.predict(metric, timestamps, values, step=step)
        return MetricAnalysis(metric, timestamps, values, res.expected, res.events, prediction, step)

    def predict(self, metric: str, timestamps: list[float], values: list[float], *, step: float,
                at_index: int | None = None) -> Prediction | None:
        """Forecast from the window ending at ``at_index`` (default: the last point). Only data up to that point
        is used: the seasonal profile comes from the history before the window."""
        spec = spec_for(metric)
        if spec.threshold is None:
            return None
        end = len(values) if at_index is None else at_index + 1
        win = self.settings.forecast_window_hours * 3600
        t_end = timestamps[end - 1]
        lo = next((i for i in range(end) if timestamps[i] >= t_end - win), 0)
        offset = seasonal_offsets(timestamps[:lo], values[:lo], step=step)
        return time_to_threshold(
            timestamps[lo:end], values[lo:end], spec.threshold, metric=metric,
            horizon_seconds=self.settings.forecast_horizon_hours * 3600, offset=offset,
        )

    def _analysis(self) -> tuple[dict, dict[str, MetricAnalysis]]:
        version = self.store.data_version()
        with self._cache_lock:
            if self._cache and self._cache[0] == version:
                return self._cache[1], self._cache[2]
        t0 = time.perf_counter()
        per_metric: dict[str, MetricAnalysis] = {}
        for metric in self.store.metric_names():
            ts, vals = self.store.series(metric)
            if ts:
                per_metric[metric] = self.analyze_metric(metric, ts, vals)
        start, end = self.store.time_range()
        result = self._build_report(per_metric, start, end)
        result["analysis_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        log.info("analysis complete", extra={"ms": result["analysis_ms"], "alerts": len(result["alerts"]),
                                             "predictions": len(result["predictions"])})
        with self._cache_lock:
            self._cache = (version, result, per_metric)
        return result, per_metric

    def analyze(self) -> dict:
        return self._analysis()[0]

    def log_signals(self, start: float, end: float) -> list[TemplateSignal]:
        occ = self.store.template_occurrences()
        return template_signals(occ, start=start, end=end + 1, bucket=LOG_BUCKET)

    def _build_report(self, per_metric: dict[str, MetricAnalysis], start: float | None, end: float | None) -> dict:
        now = end or time.time()
        alerts: list[dict] = []
        predictions: list[dict] = []
        templates = self.miner.templates

        for m, a in per_metric.items():
            spec = spec_for(m)
            for e in a.events:
                sev = "critical" if e.score >= 3 * self.settings.z_threshold or e.points >= 6 else "warning"
                expected = f" vs ~{_fmt(e.expected, m)} expected" if e.expected is not None else ""
                dur = humanize_duration(e.end - e.start + a.step)
                alerts.append({
                    "id": f"anom:{m}:{int(e.start)}", "kind": "anomaly", "severity": sev, "metric": m,
                    "ts": e.start, "end": e.end, "score": e.score,
                    "title": f"{spec.label} {'spike' if e.direction == 'up' else 'drop'}: {_fmt(e.peak_value, m)}{expected}",
                    "detail": f"{e.points} anomalous points over {dur}, peak z = {e.score:.1f}.",
                    "active": e.end >= now - ACTIVE_WINDOW,
                })
            if a.prediction:
                p = a.prediction
                eta = humanize_duration(p.eta_seconds)
                band = f"{humanize_duration(p.eta_low)} - {humanize_duration(p.eta_high) if p.eta_high else '> ' + humanize_duration(self.settings.forecast_horizon_hours * 3600)}"
                sev = "critical" if p.eta_seconds <= 6 * 3600 else "warning" if p.eta_seconds <= 24 * 3600 else "info"
                if p.confidence_label == "low":   # shown, but not allowed to page anyone
                    sev = "info"
                text = f"{spec.threshold_label or spec.label + ' over threshold'} in ~{eta}"
                pred = {
                    "metric": m, "label": spec.label, "headline": text[0].upper() + text[1:],
                    "eta_seconds": p.eta_seconds, "eta_human": eta, "eta_low": p.eta_low, "eta_high": p.eta_high,
                    "band_human": band, "crossing_ts": p.crossing_ts, "threshold": p.threshold,
                    "current": p.current, "confidence": p.confidence, "confidence_label": p.confidence_label,
                    "slope_per_hour": p.slope_per_hour, "r2": p.r2, "severity": sev, "unit": spec.unit,
                    "runbook": spec.runbook,
                }
                predictions.append(pred)
                alerts.append({
                    "id": f"pred:{m}", "kind": "prediction", "severity": sev, "metric": m, "ts": now, "end": now,
                    "score": p.confidence, "title": pred["headline"],
                    "detail": (f"{spec.label} at {_fmt(p.current, m)}, rising {p.slope_per_hour:+.2f}{spec.unit}/h; "
                               f"reaches {_fmt(p.threshold, m)} in {eta} (80% band {band}), confidence {p.confidence_label}."),
                    "active": True,
                })

        if start is not None and end is not None:
            for s in self.log_signals(start, end):
                t = templates.get(s.template_id)
                text = t.text if t else f"template #{s.template_id}"
                if s.kind == "new":
                    alerts.append({
                        "id": f"tpl-new:{s.template_id}", "kind": "log_new", "severity": "info", "metric": None,
                        "ts": s.ts, "end": s.ts, "score": 0, "template_id": s.template_id,
                        "title": "New log pattern", "detail": text, "active": s.ts >= now - ACTIVE_WINDOW,
                    })
                else:
                    ratio = s.count / max(s.baseline, 0.25)
                    alerts.append({
                        "id": f"tpl-spike:{s.template_id}:{int(s.ts)}", "kind": "log_spike",
                        "severity": "critical" if s.score >= 10 else "warning", "metric": None,
                        "ts": s.ts, "end": s.ts + LOG_BUCKET, "score": s.score, "template_id": s.template_id,
                        "title": f"Log pattern spiking x{ratio:.0f} ({s.count} lines in 15m)", "detail": text,
                        "active": s.ts + LOG_BUCKET >= now - ACTIVE_WINDOW,
                    })

        predictions.sort(key=lambda p: p["eta_seconds"])
        alerts.sort(key=lambda a: (a["active"], a["ts"]), reverse=True)
        open_alerts = [a for a in alerts if a["active"]]
        penalty = sum({"critical": 25, "warning": 10, "info": 2}[a["severity"]] for a in open_alerts)
        counts = self.store.counts()
        recent_levels = self.store.level_counts(now - 3600)
        new_24h = sum(1 for a in alerts if a["kind"] == "log_new" and a["ts"] >= now - 86_400)
        return {
            "now": now, "start": start, "end": end,
            "kpis": {
                "health": max(0, 100 - penalty),
                "open_alerts": len(open_alerts),
                "critical_open": sum(1 for a in open_alerts if a["severity"] == "critical"),
                "predicted_failures": len(predictions),
                "next_failure": predictions[0]["headline"] if predictions else None,
                "templates": counts["templates"],
                "new_templates_24h": new_24h,
                "errors_last_hour": recent_levels.get("ERROR", 0),
                "warnings_last_hour": recent_levels.get("WARN", 0),
                **counts,
            },
            "predictions": predictions,
            "alerts": alerts,
        }

    # ------------------------------------------------------------------ dashboard queries

    def metrics_overview(self) -> list[dict]:
        report, per_metric = self._analysis()
        out = []
        for m, a in per_metric.items():
            spec = spec_for(m)
            out.append({
                "metric": m, "label": spec.label, "unit": spec.unit, "threshold": spec.threshold,
                "threshold_label": spec.threshold_label, "latest": a.values[-1], "latest_ts": a.timestamps[-1],
                "points": len(a.values), "anomaly_events": len(a.events), "known": m in METRICS,
                "has_prediction": a.prediction is not None,
            })
        order = list(METRICS)
        out.sort(key=lambda r: (order.index(r["metric"]) if r["metric"] in order else 99, r["metric"]))
        return out

    def series(self, metric: str, hours: float = 48) -> dict:
        _, per_metric = self._analysis()
        a = per_metric.get(metric)
        if a is None:
            raise KeyError(metric)
        spec = spec_for(metric)
        since = a.timestamps[-1] - hours * 3600
        idx = [i for i, t in enumerate(a.timestamps) if t >= since]
        pts = [{"ts": a.timestamps[i], "value": a.values[i],
                "expected": None if a.expected[i] is None else round(a.expected[i], 3)} for i in idx]
        anomalies = []
        flagged = set()
        for e in a.events:
            if e.end >= since:
                for i in idx:
                    if e.start <= a.timestamps[i] <= e.end:
                        flagged.add(i)
                anomalies.append(e.to_dict())
        return {
            "metric": metric, "label": spec.label, "unit": spec.unit, "decimals": spec.decimals,
            "threshold": spec.threshold, "threshold_label": spec.threshold_label,
            "points": pts,
            "anomaly_points": [{"ts": a.timestamps[i], "value": a.values[i]} for i in sorted(flagged)],
            "events": anomalies,
            "forecast": a.prediction.forecast if a.prediction else [],
            "prediction": a.prediction.to_dict() | {"forecast": None} if a.prediction else None,
        }

    def templates_table(self, limit: int = 20, sort: str = "recent") -> list[dict]:
        report, _ = self._analysis()
        now = report["now"]
        occ = self.store.template_occurrences(since=now - 25 * 3600)
        flags: dict[int, set[str]] = {}
        for al in report["alerts"]:
            if al.get("template_id") is not None and al["ts"] >= now - 86_400:
                flags.setdefault(al["template_id"], set()).add("new" if al["kind"] == "log_new" else "spiking")
        levels = self._template_levels()
        rows = []
        for t in self.miner.templates.values():
            times = occ.get(t.id, [])
            last_hour = sum(1 for x in times if x >= now - 3600)
            prior = sum(1 for x in times if now - 25 * 3600 <= x < now - 3600) / 24
            rows.append({
                "id": t.id, "template": t.text, "count": t.count, "last_hour": last_hour,
                "baseline_per_hour": round(prior, 2), "ratio": round(last_hour / max(prior, 0.5), 2),
                "first_seen": t.first_seen, "last_seen": t.last_seen, "level": levels.get(t.id, "INFO"),
                "flags": sorted(flags.get(t.id, ())),
            })
        if sort == "count":
            rows.sort(key=lambda r: -r["count"])
        else:  # what an on-call engineer wants first: flagged, then busiest right now relative to normal
            rows.sort(key=lambda r: (-len(r["flags"]), -r["ratio"] * min(r["last_hour"], 50), -r["count"]))
        return rows[:limit]

    def _template_levels(self) -> dict[int, str]:
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT template_id, level, COUNT(*) c FROM logs GROUP BY template_id, level").fetchall()
        best: dict[int, Counter] = {}
        for tid, level, c in rows:
            best.setdefault(tid, Counter())[level] += c
        return {tid: c.most_common(1)[0][0] for tid, c in best.items()}

    def demo_info(self) -> dict | None:
        raw = self.store.get_meta("demo")
        return json.loads(raw) if raw else None

    def health(self) -> dict:
        try:
            counts = self.store.counts()
            db = "ok"
        except Exception as e:  # noqa: BLE001 - reported, not raised
            counts, db = {}, f"error: {e}"
        return {"status": "ok" if db == "ok" else "degraded", "db": db, **counts,
                "llm": "claude" if self.settings.llm_enabled else "rules"}
