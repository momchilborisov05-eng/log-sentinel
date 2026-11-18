"""Shared fixtures. No test touches the network or the real Claude API."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime
from types import SimpleNamespace

from logsentinel.config import Settings
from logsentinel.generator import Dataset, generate
from logsentinel.service import SentinelService
from logsentinel.storage import Store

END = datetime(2026, 11, 18, 10, 0, tzinfo=UTC).timestamp()   # fixed, so every run sees the same data
_CACHE: dict = {}


def settings(**overrides) -> Settings:
    base = dict(db_path=":memory:", seed_demo_data=False, llm_mode="rules", log_level="ERROR")
    base.update(overrides)
    return Settings(**base)


def dataset(days: int = 7, seed: int = 42, future_hours: float = 0.0) -> Dataset:
    key = (days, seed, future_hours)
    if key not in _CACHE:
        _CACHE[key] = generate(end=END, days=days, seed=seed, future_hours=future_hours)
    return _CACHE[key]


def loaded_service(days: int = 7, seed: int = 42, **overrides) -> SentinelService:
    svc = SentinelService(settings(**overrides), Store(":memory:"))
    svc.load_dataset(dataset(days, seed))
    return svc


def seasonal_series(days: float = 4, step: float = 300, seed: int = 1, noise: float = 1.0):
    """A daily sine wave with noise: the kind of normal behaviour a detector must *not* flag."""
    rng = random.Random(seed)
    n = int(days * 86_400 / step)
    ts = [END - (n - i) * step for i in range(n)]
    vals = [50 + 20 * math.sin(2 * math.pi * (t % 86_400) / 86_400) + rng.gauss(0, noise) for t in ts]
    return ts, vals


class FakeAnthropic:
    """Records the request and returns a canned response shaped like the SDK's."""

    def __init__(self, text: str = "", stop_reason: str = "end_turn", error: Exception | None = None):
        self.calls: list[dict] = []
        self._text, self._stop, self._error = text, stop_reason, error
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return SimpleNamespace(stop_reason=self._stop, model=kwargs.get("model"),
                               content=[SimpleNamespace(type="text", text=self._text)])
