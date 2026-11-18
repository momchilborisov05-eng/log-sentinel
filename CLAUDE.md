# CLAUDE.md

Guidance for Claude Code (and other AI coding assistants) working in this repository. Read it at the start of every
session.

## What this is

Log Sentinel predicts service failures from metrics and logs: an anomaly-detection ensemble (seasonal baseline,
rolling z-score, EWMA), Drain-lite log template mining (new / spiking patterns), Holt time-to-threshold forecasts with
an 80% band, and an incident summary (Claude when `ANTHROPIC_API_KEY` is set, rule-based otherwise). Python 3.11+,
FastAPI, Pydantic v2, stdlib `sqlite3`, the Anthropic SDK. No numpy, no ORM, no frontend build (vanilla JS + Chart.js
+ GSAP from cdnjs).

## Commands

```bash
python -m unittest                         # all tests (offline, ~10 s). Run from the project root after every change.
python -m coverage run -m unittest && python -m coverage report   # CI gate: 85%
ruff check .                               # lint (config in pyproject.toml)
python -m logsentinel serve --port 8016    # API + dashboard; seeds 7 days of demo data into an empty DB
python -m logsentinel eval                 # precision/recall on one fixed dataset
python -m logsentinel bench                # 10 seeds + throughput -> examples/benchmark.{md,json} (~30 s)
```

Do not use `unittest discover -s tests`: the tests use relative imports (`from .helpers import ...`).

## Map

| Path | Responsibility |
|---|---|
| `logsentinel/config.py` | `Settings.from_env`, metric catalogue (`METRICS`: threshold, direction, runbook), `spec_for` |
| `logsentinel/storage.py` | SQLite schema and queries; WAL; every call goes through one `RLock` |
| `logsentinel/generator.py` | synthetic telemetry with injected incidents (3 sudden, 2 slow-burn), blips, a benign deploy |
| `logsentinel/detectors.py` | seasonal baseline, robust residual z, rolling z, EWMA, ensemble events |
| `logsentinel/logmine.py` | Drain-lite masking and mining; new-template and Poisson spike rules |
| `logsentinel/forecast.py` | OLS trend gate, Holt on de-seasonalised data, ETA + 80% band + confidence |
| `logsentinel/service.py` | `SentinelService`: ingest, analysis snapshot (cached per data version), health, demo reset |
| `logsentinel/evaluation.py` | event-level precision/recall (30 min grace), forecast backtest, Markdown report |
| `logsentinel/summary.py` | `rule_summary`, `ClaudeSummarizer`, `Summarizer` (never raises; falls back to rules) |
| `logsentinel/web/app.py` | FastAPI app: validation models, request-id middleware, error envelopes, routes |
| `logsentinel/web/static/` | dashboard (`index.html`, `app.js`, `style.css`) |
| `benchmarks/bench.py` | multi-seed quality + throughput benchmark |
| `tests/` | unittest suite; `helpers.py` has the fixed `END` timestamp, cached dataset and `FakeAnthropic` |

## Rules

- The web layer and the CLI talk only to `SentinelService`; detectors, miner and forecaster stay pure functions of
  their inputs (no DB access), which is what makes them testable and benchmarkable.
- The app must work without an API key and without network. Any Claude failure must end in `rule_summary` with a
  `fallback_reason`, never in a 500.
- Claude call shape: `output_config={"effort": "low"}`; the server-side fallback beta only for models in
  `FALLBACK_MODELS`; no `thinking` parameter; check `stop_reason` for `refusal` / `max_tokens`.
- README numbers must come from `examples/benchmark.md`. If you change a detector, re-run `python -m logsentinel bench`
  and update the README tables with the new real numbers. Never edit the numbers by hand.
- Tests are deterministic: use `tests.helpers.END` and fixed seeds, never `time.time()`.
- In the dashboard, every value inserted with `innerHTML` goes through `esc()`.
- No secrets in code or logs; the API key field is `repr=False`.

## Definition of done

1. `ruff check .` passes.
2. `python -m unittest` passes and coverage stays at 85% or more.
3. If detection, mining or forecasting changed: benchmark re-run, README tables updated from `examples/benchmark.md`.
4. `python -m logsentinel serve` starts, `/health` returns 200 and the dashboard renders without console errors.
