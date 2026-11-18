"""Benchmark: detection quality on injected incidents (several seeds) and ingest throughput.

    python -m benchmarks.bench            # 10 seeds, writes examples/benchmark.md and examples/benchmark.json
    python -m benchmarks.bench --quick    # 3 seeds, smaller throughput run

Every number comes from running the code in this repository on synthetic data with known incident windows.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from logsentinel import __version__  # noqa: E402
from logsentinel.config import Settings  # noqa: E402
from logsentinel.evaluation import evaluate, to_markdown  # noqa: E402
from logsentinel.generator import generate  # noqa: E402
from logsentinel.service import SentinelService  # noqa: E402
from logsentinel.storage import Store  # noqa: E402

END = datetime(2026, 11, 18, 10, 0, tzinfo=UTC).timestamp()


def quality(seeds: list[int]) -> dict:
    runs = []
    for seed in seeds:
        t0 = time.perf_counter()
        r = evaluate(generate(end=END, seed=seed, future_hours=48))
        r["seed"], r["eval_seconds"] = seed, round(time.perf_counter() - t0, 2)
        runs.append(r)
    fams = runs[0]["scores"].keys()
    agg = {}
    for f in fams:
        alerts = sum(r["scores"][f]["alerts"] for r in runs)
        tp = sum(r["scores"][f]["true_positive_alerts"] for r in runs)
        det = sum(len(r["scores"][f]["detected"]) for r in runs)
        scope = sum(r["scores"][f]["in_scope"] for r in runs)
        ttd = [v for r in runs for v in r["scores"][f]["time_to_detect_min"].values()]
        agg[f] = {"name": runs[0]["scores"][f]["name"], "alerts": alerts, "false_positives": alerts - tp,
                  "precision": round(tp / alerts, 3) if alerts else None, "recall": round(det / scope, 3),
                  "detected": det, "in_scope": scope,
                  "median_time_to_detect_min": round(statistics.median(ttd), 1) if ttd else None}
    rows = [b for r in runs for b in r["forecast_backtest"]]
    errs = [abs(b["error_min"]) for b in rows if b["error_min"] is not None]
    by_h = {}
    for b in rows:
        if b["error_min"] is not None:
            by_h.setdefault(b["hours_before_end"], []).append(abs(b["error_min"]))
    per_incident_ttd = {}
    for r in runs:
        for k, v in r["scores"]["all"]["time_to_detect_min"].items():
            per_incident_ttd.setdefault(k, []).append(v)
    return {
        "seeds": seeds,
        "families": agg,
        "forecast": {
            "made": sum(1 for b in rows if b["predicted_eta_min"] is not None), "total": len(rows),
            "within_80_band": sum(1 for b in rows if b["within_band"]),
            "mae_min": round(statistics.mean(errs), 1) if errs else None,
            "median_abs_error_min": round(statistics.median(errs), 1) if errs else None,
            "mae_by_hours_before_end": {h: round(statistics.mean(v), 1) for h, v in sorted(by_h.items(), reverse=True)},
            "median_true_eta_min": round(statistics.median([b["true_eta_min"] for b in rows if b["true_eta_min"]]), 1),
        },
        "median_time_to_detect_by_incident_min": {k: round(statistics.median(v), 1) for k, v in per_incident_ttd.items()},
        "false_positive_labels": sorted({lbl for r in runs for lbl in r["false_positive_labels"]}),
        "analysis_ms_median": round(statistics.median(r["analysis_ms"] for r in runs), 1),
        "example_seed_table": to_markdown(runs[0]),
    }


def throughput(quick: bool) -> dict:
    from fastapi.testclient import TestClient

    from logsentinel.web import create_app

    settings = Settings(db_path=":memory:", seed_demo_data=False, llm_mode="rules", log_level="ERROR")
    ds = generate(end=time.time() - 3600, days=2 if quick else 7, seed=1)
    out = {}

    # 1) service layer, no HTTP: storage + template mining
    svc = SentinelService(settings, Store(":memory:"))
    pts = [{"ts": t, "metric": m, "value": v} for t, m, v in ds.metrics]
    lines = [{"ts": t, "level": lvl, "service": s, "message": msg} for t, lvl, s, msg in ds.logs]
    t0 = time.perf_counter()
    for i in range(0, len(pts), 5000):
        svc.ingest_metrics(pts[i:i + 5000])
    out["service_metric_points_per_s"] = round(len(pts) / (time.perf_counter() - t0))
    t0 = time.perf_counter()
    for i in range(0, len(lines), 1000):
        svc.ingest_logs(lines[i:i + 1000])
    out["service_log_lines_per_s"] = round(len(lines) / (time.perf_counter() - t0))
    t0 = time.perf_counter()
    report = svc.analyze()
    out["full_analysis_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["dataset"] = {"metric_points": len(pts), "log_lines": len(lines), "templates": report["kpis"]["templates"]}
    svc.close()

    # 2) through the HTTP API (FastAPI TestClient, JSON validation included), batches of 1000
    app = create_app(settings, service=SentinelService(settings, Store(":memory:")))
    client = TestClient(app)
    n_pts = pts[: 20_000 if quick else 50_000]
    t0 = time.perf_counter()
    for i in range(0, len(n_pts), 1000):
        r = client.post("/api/ingest/metrics", json={"points": n_pts[i:i + 1000]})
        assert r.status_code == 202, r.text
    out["api_metric_points_per_s"] = round(len(n_pts) / (time.perf_counter() - t0))
    n_lines = lines[: 5000 if quick else 20_000]
    t0 = time.perf_counter()
    for i in range(0, len(n_lines), 500):
        r = client.post("/api/ingest/logs", json={"lines": n_lines[i:i + 500]})
        assert r.status_code == 202, r.text
    out["api_log_lines_per_s"] = round(len(n_lines) / (time.perf_counter() - t0))
    out["api_batch"] = {"metrics": 1000, "logs": 500}
    return out


def render(q: dict, tp: dict, env: dict) -> str:
    f = q["families"]
    lines = [
        "# Log Sentinel benchmark", "",
        f"Generated by `python -m benchmarks.bench` on {env['date']} ({env['python']}, {env['platform']}).",
        f"Quality: {len(q['seeds'])} synthetic 7-day datasets (seeds {q['seeds'][0]}-{q['seeds'][-1]}), 5 injected incidents "
        "each (3 sudden, 2 slow-burn), plus harmless one-sample blips and a benign deploy that adds a new log pattern.", "",
        "## Detection quality (event level, all seeds pooled)", "",
        "| Detector | Alerts | False positives | Precision | Recall | Median time to detect |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for s in f.values():
        prec = "n/a" if s["precision"] is None else f"{s['precision']:.2f}"
        ttd = "n/a" if s["median_time_to_detect_min"] is None else f"{s['median_time_to_detect_min']:.0f} min"
        lines.append(f"| {s['name']} | {s['alerts']} | {s['false_positives']} | {prec} | "
                     f"{s['recall']:.2f} ({s['detected']}/{s['in_scope']}) | {ttd} |")
    fc = q["forecast"]
    lines += [
        "", "False-positive alert types seen: " + (", ".join(f"`{x}`" for x in q["false_positive_labels"]) or "none") + ".", "",
        "## Time-to-threshold forecasts (backtest against the hidden future)", "",
        "For each slow-burn incident the forecaster was run 3, 2, 1 and 0 hours before the end of the data (only past data) "
        "and its ETA compared with when the metric really crossed its threshold.", "",
        "| Measure | Value |", "|---|---:|",
        f"| Forecasts made | {fc['made']} / {fc['total']} |",
        f"| True crossing inside the 80% band | {fc['within_80_band']} / {fc['total']} ({fc['within_80_band'] / fc['total']:.0%}) |",
        f"| Mean absolute ETA error | {fc['mae_min']} min |",
        f"| Median absolute ETA error | {fc['median_abs_error_min']} min |",
        f"| Median true time to threshold | {fc['median_true_eta_min']} min |",
    ]
    for h, v in fc["mae_by_hours_before_end"].items():
        lines.append(f"| MAE, forecast made {h}h before end | {v} min |")
    lines += [
        "", "## Ingest throughput", "",
        f"Dataset: {tp['dataset']['metric_points']:,} metric points, {tp['dataset']['log_lines']:,} log lines "
        f"({tp['dataset']['templates']} templates mined). SQLite in memory, single process.", "",
        "| Path | Throughput |", "|---|---:|",
        f"| Service layer, metric points (batches of 5000) | {tp['service_metric_points_per_s']:,} points/s |",
        f"| Service layer, log lines incl. template mining (batches of 1000) | {tp['service_log_lines_per_s']:,} lines/s |",
        f"| HTTP API, metric points (JSON + validation, batches of {tp['api_batch']['metrics']}) | {tp['api_metric_points_per_s']:,} points/s |",
        f"| HTTP API, log lines (batches of {tp['api_batch']['logs']}) | {tp['api_log_lines_per_s']:,} lines/s |",
        f"| Full analysis of the dataset (5 metrics x 7 days + logs) | {tp['full_analysis_ms']} ms |",
        "", "## One dataset in detail (seed " + str(q["seeds"][0]) + ")", "", q["example_seed_table"], "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--quick", action="store_true")
    p.add_argument("--out", default=str(ROOT / "examples"))
    args = p.parse_args(argv)
    seeds = list(range(1, (3 if args.quick else args.seeds) + 1))
    t0 = time.perf_counter()
    q = quality(seeds)
    tp = throughput(args.quick)
    env = {"date": datetime.now(UTC).strftime("%Y-%m-%d"), "python": f"Python {platform.python_version()}",
           "platform": platform.system(), "version": __version__, "seconds": round(time.perf_counter() - t0, 1)}
    md = render(q, tp, env)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.md").write_text(md, encoding="utf-8")
    (out / "benchmark.json").write_text(json.dumps({"env": env, "quality": {k: v for k, v in q.items() if k != "example_seed_table"},
                                                    "throughput": tp}, indent=1), encoding="utf-8")
    print(md)
    print(f"\n(wrote {out / 'benchmark.md'} in {env['seconds']} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
