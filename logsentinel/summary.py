"""Incident summary: what is going on right now, how bad it is and what to do first.

Two implementations behind one interface:

* ``rule_summary`` is deterministic and always available. It correlates open predictions, metric anomalies and
  log-template alerts (a spiking template whose lines appear while a metric is anomalous is treated as the likely
  cause), ranks them and attaches the runbook of each affected metric.
* ``ClaudeSummarizer`` sends the same structured facts to Claude (``claude-sonnet-5-5`` by default) and asks for a
  short on-call style write-up. Anything that goes wrong (no key, network error, refusal, malformed output) falls back
  to the rule-based summary, with the reason recorded in the response, so the app works without a key.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings, spec_for

log = logging.getLogger("logsentinel.summary")

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}

SYSTEM_PROMPT = """You are the on-call assistant of a monitoring system. You receive a JSON snapshot of one service: \
failure predictions from a trend forecaster, anomaly alerts from statistical detectors, and log-pattern alerts from a \
template miner. Write a short incident summary for the engineer on call.

Rules:
- Use only the facts in the snapshot. Do not invent hosts, versions, numbers or causes that are not there.
- Lead with the most urgent risk (the soonest predicted failure, or the worst open alert) and say how sure the system is.
- When a log pattern started or spiked at the same time as a metric problem, point it out as a likely cause, \
worded as a hypothesis.
- Recommend at most 4 concrete next actions, most urgent first. Prefer the runbook hints included in the snapshot.
- Plain prose, no markdown headings, 120 words at most for the summary.

Reply with JSON only: {"title": str (max 80 chars), "severity": "critical"|"warning"|"info"|"ok", \
"summary": str, "actions": [str, ...]}"""


def _facts(analysis: dict, max_alerts: int = 12) -> dict:
    """The compact, model-friendly snapshot both summarizers work from."""
    open_alerts = [a for a in analysis.get("alerts", []) if a.get("active")]
    open_alerts.sort(key=lambda a: (-SEVERITY_RANK.get(a["severity"], 0), -a["ts"]))
    recent = [a for a in analysis.get("alerts", []) if not a.get("active")][:5]
    k = analysis.get("kpis", {})
    return {
        "health_score": k.get("health"),
        "errors_last_hour": k.get("errors_last_hour"),
        "warnings_last_hour": k.get("warnings_last_hour"),
        "predictions": [
            {"metric": p["metric"], "headline": p["headline"], "eta": p["eta_human"], "band": p["band_human"],
             "confidence": p["confidence_label"], "current": p["current"], "threshold": p["threshold"],
             "slope_per_hour": p["slope_per_hour"], "runbook": p.get("runbook", "")}
            for p in analysis.get("predictions", [])
        ],
        "open_alerts": [
            {"kind": a["kind"], "severity": a["severity"], "metric": a.get("metric"), "title": a["title"],
             "detail": a["detail"], "ts": a["ts"]}
            for a in open_alerts[:max_alerts]
        ],
        "recent_closed_alerts": [{"kind": a["kind"], "title": a["title"], "ts": a["ts"]} for a in recent],
        "log_patterns_last_6h": [
            {"kind": a["kind"], "title": a["title"], "template": a["detail"], "ts": a["ts"]}
            for a in analysis.get("alerts", [])
            if a["kind"] in ("log_spike", "log_new") and a["ts"] >= analysis.get("now", 0) - 6 * 3600
        ][:6],
    }


def rule_summary(analysis: dict) -> dict:
    facts = _facts(analysis, max_alerts=50)
    preds = facts["predictions"]
    open_alerts = facts["open_alerts"]
    if not preds and not open_alerts:
        return {"title": "All clear", "severity": "ok", "source": "rules",
                "summary": "No open alerts and no metric is trending towards its failure threshold.",
                "actions": [], "facts": facts}

    sentences: list[str] = []
    actions: list[str] = []
    severity = "info"

    urgent = [p for p in preds if p["confidence"] != "low"]
    if urgent:
        p = urgent[0]
        severity = "critical" if any(a["severity"] == "critical" and a["kind"] == "prediction" for a in open_alerts) \
            else "warning"
        sentences.append(f"{p['headline']} (80% band {p['band']}, {p['confidence']} confidence): "
                         f"{spec_for(p['metric']).label.lower()} is at {p['current']:g} and rising "
                         f"{p['slope_per_hour']:+.2f}/h towards {p['threshold']:g}.")
        for other in urgent[1:3]:
            sentences.append(f"Also trending: {other['headline'][0].lower() + other['headline'][1:]} "
                             f"({other['confidence']} confidence).")
        actions += [p["runbook"] for p in urgent[:3] if p["runbook"]]

    anomalies = [a for a in open_alerts if a["kind"] == "anomaly"]
    if anomalies:
        severity = max(severity, max((a["severity"] for a in anomalies), key=SEVERITY_RANK.get), key=SEVERITY_RANK.get)
        sentences.append("Open anomalies: " + "; ".join(a["title"] for a in anomalies[:3]) + ".")
        for a in anomalies[:3]:
            rb = spec_for(a["metric"]).runbook if a["metric"] else ""
            if rb and rb not in actions:
                actions.append(rb)

    logs = [a for a in open_alerts if a["kind"] in ("log_spike", "log_new")]
    recent_logs = [{"kind": a["kind"], "title": a["title"], "detail": a["template"]} for a in facts["log_patterns_last_6h"]]
    if logs or ((urgent or anomalies) and recent_logs):
        pool = logs or recent_logs
        spikes = [a for a in pool if a["kind"] == "log_spike"]
        top = (spikes or pool)[0]
        lead = "Likely related log pattern" if (anomalies or urgent) else "Log pattern to look at"
        sentences.append(f"{lead}: \"{top['detail']}\" ({top['title'].lower()}).")
        actions.append(f"Inspect recent lines of the log pattern \"{top['detail'][:80]}\" in the templates table.")
        if spikes and logs:
            severity = max(severity, "warning", key=SEVERITY_RANK.get)

    lows = [p for p in preds if p["confidence"] == "low"]
    if lows and not urgent:
        sentences.append(f"Weak signal only: {lows[0]['headline'][0].lower() + lows[0]['headline'][1:]} "
                         "(low confidence; watch, do not page).")

    title_src = urgent[0]["headline"] if urgent else (anomalies[0]["title"] if anomalies else logs[0]["title"] if logs
                                                       else lows[0]["headline"])
    return {"title": title_src[:80], "severity": severity, "summary": " ".join(sentences),
            "actions": list(dict.fromkeys(actions))[:4], "source": "rules", "facts": facts}


class SummaryError(RuntimeError):
    pass


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise SummaryError("model reply contained no JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise SummaryError(f"model reply was not valid JSON: {e}") from e
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise SummaryError("model reply had no summary")
    sev = data.get("severity")
    actions = data.get("actions") or []
    return {
        "title": str(data.get("title") or "Incident summary")[:80],
        "severity": sev if sev in ("critical", "warning", "info", "ok") else "warning",
        "summary": data["summary"].strip(),
        "actions": [str(a) for a in actions if str(a).strip()][:4] if isinstance(actions, list) else [],
    }


class ClaudeSummarizer:
    def __init__(self, settings: Settings, client: Any = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, max_retries=2, timeout=60)
        self.client = client
        self.model = settings.model

    def request_kwargs(self, facts: dict) -> dict:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "Snapshot:\n" + json.dumps(facts, indent=1, default=str)}],
            "output_config": {"effort": "low"},
        }
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return kwargs

    def summarize(self, analysis: dict) -> dict:
        facts = _facts(analysis)
        kwargs = self.request_kwargs(facts)
        api = self.client.beta.messages if "betas" in kwargs else self.client.messages
        try:
            response = api.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - any API failure means "use the rules"
            raise SummaryError(f"Claude API call failed: {type(e).__name__}") from e
        if getattr(response, "stop_reason", None) in ("refusal", "max_tokens"):
            raise SummaryError(f"model stopped early (stop_reason={response.stop_reason})")
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        out = _parse_json(text)
        return out | {"source": "claude", "model": getattr(response, "model", self.model), "facts": facts}


class Summarizer:
    """Claude when enabled, rules otherwise; never raises."""

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self._client = client
        self._claude: ClaudeSummarizer | None = None

    @property
    def mode(self) -> str:
        return "claude" if self.settings.llm_enabled else "rules"

    def summarize(self, analysis: dict) -> dict:
        if not self.settings.llm_enabled:
            return rule_summary(analysis)
        try:
            if self._claude is None:
                self._claude = ClaudeSummarizer(self.settings, self._client)
            return self._claude.summarize(analysis)
        except Exception as e:  # noqa: BLE001
            log.warning("claude summary failed, using rules", extra={"reason": str(e)})
            return rule_summary(analysis) | {"fallback_reason": str(e)}
