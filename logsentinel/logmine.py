"""Log template mining (a compact take on Drain) and template-level anomaly detection.

``mask`` replaces the variable parts of a line (UUIDs, IPs, hex ids, numbers) with placeholders. ``TemplateMiner``
then groups masked lines by token count and first token (Drain's fixed-depth tree, two levels deep) and, inside a
group, merges a line into the most similar template when at least ``sim_threshold`` of the token positions match;
positions that differ become ``<*>``. Templates are what we count: a template that never existed before, or one
whose rate jumps far above its own history, is a signal even when no metric has moved yet.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from statistics import mean

WILDCARD = "<*>"

_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,}\b"), "<HEX>"),
    (re.compile(r"(?<![A-Za-z0-9<.\-])[-+]?\d+(?:\.\d+)?"), "<NUM>"),
]


def mask(message: str) -> str:
    """Replace variable tokens with placeholders. Numbers glued to a letter, dot or hyphen on their left (``sha256``, ``p95``,
    ``eu-west-1``, ``v2.14``) are kept because they are part of a name; numbers with a unit on the right (``850ms``)
    or after ``=``, ``/``, ``_`` or a space are masked."""
    out = message.strip()
    for pattern, repl in _MASKS:
        out = pattern.sub(repl, out)
    return out


def tokenize(message: str) -> list[str]:
    return mask(message).split() or ["<EMPTY>"]


@dataclass
class Template:
    id: int
    tokens: list[str]
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(self.tokens)

    @property
    def constant_tokens(self) -> int:
        return sum(1 for t in self.tokens if t != WILDCARD)


def similarity(tokens: list[str], template: list[str]) -> float:
    """Share of positions where the line matches the template (a wildcard matches anything)."""
    if len(tokens) != len(template) or not tokens:
        return 0.0
    same = sum(1 for a, b in zip(tokens, template, strict=True) if a == b or b == WILDCARD)
    return same / len(tokens)


def _group_key(tokens: list[str]) -> tuple[int, str]:
    first = tokens[0]
    return len(tokens), (WILDCARD if "<" in first else first)


@dataclass
class MinerResult:
    template: Template
    change: str           # "new" | "updated" (template text changed) | "none"


@dataclass
class TemplateMiner:
    sim_threshold: float = 0.5
    max_templates: int = 5000
    templates: dict[int, Template] = field(default_factory=dict)
    _groups: dict[tuple[int, str], list[int]] = field(default_factory=dict)
    _next_id: int = 1

    def _best(self, tokens: list[str]) -> tuple[Template | None, float]:
        best: Template | None = None
        best_sim = -1.0
        for tid in self._groups.get(_group_key(tokens), ()):
            t = self.templates[tid]
            s = similarity(tokens, t.tokens)
            if s > best_sim or (s == best_sim and best is not None and t.constant_tokens > best.constant_tokens):
                best, best_sim = t, s
        return best, best_sim

    def match(self, message: str) -> Template | None:
        """Find the template for a line without changing the miner."""
        t, s = self._best(tokenize(message))
        return t if t is not None and s >= self.sim_threshold else None

    def add(self, message: str, ts: float = 0.0) -> MinerResult:
        tokens = tokenize(message)
        best, sim = self._best(tokens)
        if best is not None and sim >= self.sim_threshold:
            merged = [a if a == b else WILDCARD for a, b in zip(tokens, best.tokens, strict=True)]
            change = "updated" if merged != best.tokens else "none"
            best.tokens = merged
            best.count += 1
            best.first_seen = min(best.first_seen, ts) if best.count > 1 else ts
            best.last_seen = max(best.last_seen, ts)
            return MinerResult(best, change)
        if len(self.templates) >= self.max_templates:
            # Bounded memory: overflow lines are attributed to a catch-all template instead of growing forever.
            return self.add_overflow(ts)
        t = Template(self._next_id, tokens, 1, ts, ts)
        self._next_id += 1
        self.templates[t.id] = t
        self._groups.setdefault(_group_key(tokens), []).append(t.id)
        return MinerResult(t, "new")

    def add_overflow(self, ts: float) -> MinerResult:
        for t in self.templates.values():
            if t.tokens == ["<OVERFLOW>"]:
                t.count += 1
                t.last_seen = max(t.last_seen, ts)
                return MinerResult(t, "none")
        t = Template(self._next_id, ["<OVERFLOW>"], 1, ts, ts)
        self._next_id += 1
        self.templates[t.id] = t
        return MinerResult(t, "new")

    def load(self, templates: list[Template]) -> None:
        """Restore persisted templates (keeps ids stable across restarts)."""
        self.templates.clear()
        self._groups.clear()
        for t in templates:
            self.templates[t.id] = t
            if t.tokens != ["<OVERFLOW>"]:
                self._groups.setdefault(_group_key(t.tokens), []).append(t.id)
        self._next_id = max(self.templates, default=0) + 1


# ------------------------------------------------------------------ template rate anomalies

@dataclass(frozen=True)
class TemplateSignal:
    template_id: int
    kind: str              # "new" | "spike"
    ts: float              # bucket start (spike) or first occurrence (new)
    count: int             # lines in that bucket
    baseline: float        # expected lines per bucket (busy-hour normal)
    score: float           # (count - baseline) / sqrt(baseline) for spikes, 0 for new templates


def bucketize(timestamps: list[float], bucket: float, start: float, end: float) -> list[int]:
    nb = max(1, int(math.ceil((end - start) / bucket)))
    counts = [0] * nb
    for ts in timestamps:
        i = int((ts - start) // bucket)
        if 0 <= i < nb:
            counts[i] += 1
    return counts


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def spike_scores(counts: list[int], baseline_buckets: int = 96, min_baseline: int = 8) -> list[tuple[float, float, float]]:
    """(z, expected rate, tail probability) of every bucket against the ``baseline_buckets`` before it.

    The expected rate is the *busy-hour* normal: max(mean, 90th percentile, 0.5) of the baseline window, so a
    template that is simply busier in the afternoon does not look like a spike. The tail probability is the
    Poisson P(X >= count) at that rate; z = (count - rate) / sqrt(rate) is kept as a readable score.
    """
    out: list[tuple[float, float, float]] = []
    for i, c in enumerate(counts):
        lo = max(0, i - baseline_buckets)
        hist = counts[lo:i]
        if len(hist) < min_baseline:
            out.append((0.0, 0.0, 1.0))
            continue
        srt = sorted(hist)
        p90 = srt[min(len(srt) - 1, int(0.9 * len(srt)))]
        lam = max(mean(hist), float(p90), 0.5)
        out.append(((c - lam) / math.sqrt(lam), lam, poisson_sf(c, lam) if c > lam else 1.0))
    return out


def template_signals(
    occurrences: dict[int, list[float]],
    *,
    start: float,
    end: float,
    bucket: float = 900,
    baseline_buckets: int = 96,
    max_p: float = 1e-6,
    min_count: int = 5,
    warmup: float = 86_400,
) -> list[TemplateSignal]:
    """Every "new template" and "template spike" signal over [start, end).

    A template is *new* when its first line arrives after ``warmup`` seconds of history (before that, everything
    is new). A bucket is a *spike* when it has at least ``min_count`` lines and its Poisson tail probability at the
    busy-hour baseline rate is below ``max_p`` (1e-6 is roughly a one-sided 4.75 sigma event).
    Consecutive spiking buckets of one template are reported once, at the first bucket.
    """
    signals: list[TemplateSignal] = []
    for tid, times in occurrences.items():
        if not times:
            continue
        first = min(times)
        if first >= start + warmup:
            signals.append(TemplateSignal(tid, "new", first, 1, 0.0, 0.0))
        counts = bucketize(times, bucket, start, end)
        prev = False
        for i, (z, mu, p) in enumerate(spike_scores(counts, baseline_buckets)):
            hit = counts[i] >= min_count and p < max_p
            if hit and not prev:
                signals.append(TemplateSignal(tid, "spike", start + i * bucket, counts[i], round(mu, 3), round(z, 2)))
            prev = hit
    signals.sort(key=lambda s: s.ts)
    return signals
