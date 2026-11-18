"""Command line: ``python -m logsentinel <command>``.

    serve      run the API + dashboard (seeds 7 days of synthetic data on first start)
    reset      regenerate the demo data in the database
    eval       score detectors and forecaster against the injected incidents (fixed, reproducible dataset)
    bench      evaluation over several seeds + ingest throughput; writes examples/benchmark.{md,json}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _settings():
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # python-dotenv is optional
        pass
    from .config import Settings

    return Settings.from_env()


def cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app

    app = create_app(_settings())
    cfg = app.state.service.health()
    print(f"Log Sentinel on http://{args.host}:{args.port}  summaries={cfg['llm']}  "
          f"metric_points={cfg.get('metric_points')}  log_lines={cfg.get('log_lines')}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_reset(args) -> int:
    from .service import SentinelService

    svc = SentinelService(_settings().with_(seed_demo_data=False))
    print(json.dumps(svc.reset_demo(seed=args.seed)))
    svc.close()
    return 0


def cmd_eval(args) -> int:
    from .evaluation import evaluate, to_markdown
    from .generator import generate

    ds = generate(end=args.end, seed=args.seed, future_hours=48)
    result = evaluate(ds)
    print(json.dumps(result, indent=1) if args.json else to_markdown(result))
    return 0


def cmd_bench(args) -> int:
    sys.path.insert(0, str(ROOT))
    from benchmarks.bench import main as bench_main

    return bench_main(["--seeds", str(args.seeds)] + (["--quick"] if args.quick else []))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="logsentinel", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8016)
    s.set_defaults(func=cmd_serve)
    r = sub.add_parser("reset", help="regenerate demo data")
    r.add_argument("--seed", type=int, default=42)
    r.set_defaults(func=cmd_reset)
    e = sub.add_parser("eval", help="precision/recall on injected incidents")
    e.add_argument("--seed", type=int, default=42)
    e.add_argument("--end", type=float, default=1_794_996_000.0, help="unix seconds of the last sample")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval)
    b = sub.add_parser("bench", help="full benchmark (writes examples/benchmark.md)")
    b.add_argument("--seeds", type=int, default=10)
    b.add_argument("--quick", action="store_true")
    b.set_defaults(func=cmd_bench)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
