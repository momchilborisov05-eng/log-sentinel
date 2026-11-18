"""FastAPI app: ingest API, analysis/dashboard API and the static single-page dashboard."""

from __future__ import annotations

import logging
import math
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .. import __version__
from ..config import METRICS, Settings
from ..logging_setup import configure
from ..service import SentinelService, ValidationError
from ..summary import Summarizer

STATIC = Path(__file__).resolve().parent / "static"
MAX_BATCH = 5000
METRIC_NAME = r"^[a-z][a-z0-9_.]{0,63}$"

log = logging.getLogger("logsentinel.api")


class MetricPoint(BaseModel):
    ts: float = Field(description="Unix seconds")
    metric: str = Field(pattern=METRIC_NAME)
    value: float
    host: str | None = Field(default=None, max_length=64)

    @field_validator("ts", "value")
    @classmethod
    def finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class MetricBatch(BaseModel):
    points: list[MetricPoint] = Field(min_length=1, max_length=MAX_BATCH)


class LogLine(BaseModel):
    ts: float
    message: str = Field(min_length=1, max_length=4000)
    level: Literal["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"] = "INFO"
    service: str | None = Field(default=None, max_length=64)

    @field_validator("ts")
    @classmethod
    def finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    @field_validator("level", mode="before")
    @classmethod
    def upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


class LogBatch(BaseModel):
    lines: list[LogLine] = Field(min_length=1, max_length=MAX_BATCH)


class DemoReset(BaseModel):
    seed: int = Field(default=42, ge=0, le=1_000_000)


def create_app(settings: Settings | None = None, *, service: SentinelService | None = None,
               summarizer: Summarizer | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure(settings.log_level, settings.log_json)
    service = service or SentinelService(settings)
    startup = service.startup()
    log.info("service started", extra=startup)
    summarizer = summarizer or Summarizer(settings)

    app = FastAPI(title="Log Sentinel", version=__version__,
                  description="Predicts system failures from metrics and logs.")
    app.state.service = service
    app.state.summarizer = summarizer

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # pragma: no cover - turned into a 500 by the handler below
            log.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
            response = JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        response.headers["x-request-id"] = rid
        if not request.url.path.startswith("/static"):
            log.info("request", extra={"request_id": rid, "method": request.method, "path": request.url.path,
                                       "status": response.status_code, "ms": ms})
        return response

    @app.exception_handler(RequestValidationError)
    async def on_schema_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()[:20]]
        return JSONResponse({"error": "validation_error", "detail": errors}, status_code=422)

    @app.exception_handler(ValidationError)
    async def on_value_error(request: Request, exc: ValidationError):
        return JSONResponse({"error": "validation_error", "detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def on_not_found(request: Request, exc: KeyError):
        return JSONResponse({"error": "not_found", "detail": f"unknown {exc.args[0]!r}" if exc.args else ""},
                            status_code=404)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse({"error": "internal_error"}, status_code=500)

    # ------------------------------------------------------------------ meta

    @app.get("/health")
    def health():
        h = service.health()
        return JSONResponse(h | {"version": __version__}, status_code=200 if h["status"] == "ok" else 503)

    @app.get("/api/config")
    def config():
        return {
            "version": __version__, "llm": summarizer.mode, "model": settings.model if settings.llm_enabled else None,
            "z_threshold": settings.z_threshold, "forecast_window_hours": settings.forecast_window_hours,
            "forecast_horizon_hours": settings.forecast_horizon_hours, "max_batch": MAX_BATCH,
            "metrics": {m: {"label": s.label, "unit": s.unit, "threshold": s.threshold,
                            "threshold_label": s.threshold_label} for m, s in METRICS.items()},
            "demo": service.demo_info(),
        }

    # ------------------------------------------------------------------ ingest

    @app.post("/api/ingest/metrics", status_code=202)
    def ingest_metrics(batch: MetricBatch):
        n = service.ingest_metrics([p.model_dump() for p in batch.points])
        return {"accepted": n}

    @app.post("/api/ingest/logs", status_code=202)
    def ingest_logs(batch: LogBatch):
        lines = [ln.model_dump() | {"level": "WARN" if ln.level == "WARNING" else ln.level} for ln in batch.lines]
        return service.ingest_logs(lines)

    # ------------------------------------------------------------------ analysis

    @app.get("/api/analysis")
    def analysis():
        return service.analyze()

    @app.get("/api/metrics")
    def metrics():
        return {"metrics": service.metrics_overview()}

    @app.get("/api/series")
    def series(metric: str = Query(pattern=METRIC_NAME), hours: float = Query(48, gt=0, le=24 * 31)):
        return service.series(metric, hours)

    @app.get("/api/templates")
    def templates(limit: int = Query(20, ge=1, le=500), sort: Literal["recent", "count"] = "recent"):
        return {"templates": service.templates_table(limit, sort)}

    @app.get("/api/logs")
    def logs(template_id: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=500),
             level: str | None = Query(None, pattern=r"^[A-Z]{3,8}$")):
        return {"logs": service.store.recent_logs(limit, template_id, level)}

    @app.post("/api/summary")
    def summary():
        return summarizer.summarize(service.analyze())

    @app.post("/api/demo/reset")
    def demo_reset(body: DemoReset | None = None):
        return service.reset_demo(seed=(body or DemoReset()).seed)

    # ------------------------------------------------------------------ UI

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
