"""
TriageEngine - LLM as the brains: full metrics in, structured conclusion out.
Uses swappable TriageLLM provider (MedGemma, OpenAI, or rule-based fallback).
Schema-locked JSON output for triage card.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from crtv.domain.models import TriageCard, ActionItem
from crtv.reasoning.llm_providers import get_provider

logger = logging.getLogger("crtv.triage")

# Default prompts dir: project root / prompts; overridable via CRTV_PROMPTS_DIR
_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
if os.environ.get("CRTV_PROMPTS_DIR"):
    _PROMPTS_DIR = Path(os.environ["CRTV_PROMPTS_DIR"])

_DEFAULT_TRIAGE_PROMPT = """You are a clinical triage assistant for stroke telerehabilitation. Given the following patient metrics, produce a structured triage conclusion. Output ONLY valid JSON, no other text.

Patient metrics:
{metrics}

Output JSON format (strict):
{"headline":"one line summary","reasons":["reason1","reason2"],"recommended_actions":[{"action_type":"...","params":{}}],"disposition":"NO_ACTION|SUGGEST|ESCALATE","confidence":0.0-1.0}

Rules: disposition ESCALATE only for safety/urgent; SUGGEST for actionable interventions; NO_ACTION when stable. Recommended actions must be from: pause_prescription, adjust_prescribed_minutes, swap_protocol, assign_questionnaire, message, escalate_to_clinician.
JSON:"""


def _load_triage_prompt() -> str:
    """Load triage prompt: versioned store (Ops-editable) → file → built-in default."""
    try:
        from chatbot.triage_prompt_store import get_active
        active = get_active()
        if active and active.get("system_prompt"):
            return active["system_prompt"]
    except Exception as e:
        logger.debug("triage_prompt_store unavailable, falling back: %s", e)
    path = _PROMPTS_DIR / "triage_system.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to load prompt from %s: %s", path, e)
    return _DEFAULT_TRIAGE_PROMPT

if TYPE_CHECKING:
    from crtv.reasoning.llm_providers import TriageLLM


@dataclass
class ObservationItem:
    text: str
    attention: int  # 1=ok, 2=mild, 3=sustained high concern
    refs: list[str]  # plot IDs: adherence, performance, difficulty, protocol_X, sessions, self_reports


@dataclass
class MedGemmaConclusion:
    headline: str
    reasons: list[str]  # legacy: derived from observations
    observations: list[ObservationItem]  # at most 3, each with text, attention, refs
    recommended_actions: list[dict]
    disposition: str  # NO_ACTION or TRIAGE
    severity: str  # low, medium, high (when TRIAGE)
    confidence: float


def _extract_json_object(text: str) -> str | None:
    """Extract the first complete JSON object from text using brace-counting. Handles nested braces."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    quote_char: str | None = None
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if in_str:
            if c == "\\":
                escape = True
            elif c == quote_char:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str = True
            quote_char = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _safe_conclusion() -> MedGemmaConclusion:
    return MedGemmaConclusion(
        headline="Needs manual review",
        reasons=[],
        observations=[],
        recommended_actions=[],
        disposition="TRIAGE",
        severity="medium",
        confidence=0.0,
    )


def _filter_metrics_to_last_weeks(metrics: dict, weeks: int = 3) -> dict:
    """Return a copy of metrics restricted to the last N weeks before checkpoint."""
    cp_str = metrics.get("checkpoint_date")
    if not cp_str:
        return metrics
    try:
        cp = datetime.fromisoformat(cp_str.replace("Z", "+00:00")[:10]).date()
    except (ValueError, TypeError):
        return metrics
    cutoff = cp - timedelta(days=weeks * 7)

    out = dict(metrics)
    # Filter adherence days
    if out.get("adherence") and out["adherence"].get("days"):
        days = [d for d in out["adherence"]["days"] if str(d.get("date", ""))[:10] >= str(cutoff)]
        out["adherence"] = {**out["adherence"], "days": days}
        # Recompute totals for filtered days
        planned = sum(d.get("planned_min", 0) for d in days)
        done = sum(d.get("done_min", 0) for d in days)
        out["adherence"]["planned_total"] = planned
        out["adherence"]["done_total"] = done
        out["adherence"]["adherence_minutes"] = done / planned if planned > 0 else 0

    # Filter sessions
    valid_sess_ids = set()
    if out.get("sessions"):
        filtered_sessions = [
            s for s in out["sessions"]
            if (s.get("start_time", "")[:10] or "") >= str(cutoff)
        ]
        valid_sess_ids = {s.get("session_id") for s in filtered_sessions}
        out["sessions"] = filtered_sessions

    # Filter protocol_wise performance/difficulty
    if isinstance(out.get("protocol_wise"), dict):
        pw = {}
        for proto_id, data in list(out["protocol_wise"].items()):
            if not isinstance(data, dict):
                continue
            perf = [x for x in data.get("performance", []) if str(x.get("date", ""))[:10] >= str(cutoff)]
            diff = [x for x in data.get("difficulty", []) if str(x.get("date", ""))[:10] >= str(cutoff)]
            pw[proto_id] = {**data, "performance": perf, "difficulty": diff}
        out["protocol_wise"] = pw

    # Filter aggregate performance/difficulty by valid session IDs
    if valid_sess_ids and out.get("performance"):
        out["performance"] = [p for p in out["performance"] if p.get("session_id") in valid_sess_ids]
    if valid_sess_ids and out.get("difficulty"):
        out["difficulty"] = [d for d in out["difficulty"] if d.get("session_id") in valid_sess_ids]

    return out


def _daily_avg(series: list, date_key: str = "date", value_key: str = "value") -> list[tuple[str, float]]:
    """Aggregate series by date, return sorted (date, avg) pairs."""
    by_date: dict[str, list[float]] = {}
    for x in series:
        dt = str(x.get(date_key, ""))[:10]
        v = x.get(value_key) or x.get("performance_mean") or x.get("difficulty_mean") or 0
        if dt:
            by_date.setdefault(dt, []).append(float(v))
    return [(d, sum(v) / len(v)) for d, v in sorted(by_date.items())]


def _metrics_to_prompt(metrics: dict) -> str:
    """Format all metrics for LLM context. Full 4-week window. Includes daily averages."""
    metrics = _filter_metrics_to_last_weeks(metrics, weeks=4)
    lines = []

    # Trial context
    pid = metrics.get("patient_id")
    cp = metrics.get("checkpoint_date")
    if pid is not None:
        lines.append(f"Patient ID: {pid}. Checkpoint date: {cp or 'N/A'}.")

    fm_bl = metrics.get("fm_bl")
    if fm_bl is not None:
        lines.append(f"Fugl-Meyer baseline: {fm_bl}.")

    # Adherence
    if metrics.get("adherence"):
        a = metrics["adherence"]
        lines.append(f"Adherence: {a.get('adherence_minutes', 0):.0%} "
                     f"(done {a.get('done_total', 0):.0f} / planned {a.get('planned_total', 0):.0f} min).")
        days = a.get("days", [])
        if days:
            per_day = ", ".join(f"{d['date'][:10]}: {d.get('done_min', 0):.0f}/{d.get('planned_min', 0)}" for d in days[:14])
            lines.append(f"Per-day (planned/done min): {per_day}.")

    # Sessions
    sess = metrics.get("sessions") or []
    if sess:
        lines.append(f"Sessions: {len(sess)}.")
        recent = sess[-5:] if len(sess) >= 5 else sess
        lines.append(f"Recent: {[(s.get('start_time', '')[:10], s.get('duration_sec', 0)) for s in recent]}.")

    session_by_id = {s["session_id"]: s for s in sess}
    perf_raw = metrics.get("performance") or []
    diff_raw = metrics.get("difficulty") or []

    # Aggregate daily averages (performance, difficulty)
    perf_by_date: dict[str, list[float]] = {}
    for p in perf_raw:
        sid = p.get("session_id")
        dt = (session_by_id.get(sid) or {}).get("start_time", "")[:10] if sid else ""
        if dt:
            perf_by_date.setdefault(dt, []).append(p.get("performance_mean", 0))
    agg_perf = [(d, sum(v) / len(v)) for d, v in sorted(perf_by_date.items())]
    if agg_perf:
        lines.append(f"Aggregate performance (daily avg): {[(d, round(v, 3)) for d, v in agg_perf]}.")

    diff_by_date: dict[str, list[float]] = {}
    for d in diff_raw:
        sid = d.get("session_id")
        dt = (session_by_id.get(sid) or {}).get("start_time", "")[:10] if sid else ""
        if dt:
            diff_by_date.setdefault(dt, []).append(d.get("difficulty_mean", 0))
    agg_diff = [(d, sum(v) / len(v)) for d, v in sorted(diff_by_date.items())]
    if agg_diff:
        lines.append(f"Aggregate difficulty (daily avg): {[(d, round(v, 3)) for d, v in agg_diff]}.")

    # Protocol-wise: daily averages
    pw = metrics.get("protocol_wise")
    if isinstance(pw, dict):
        for proto_id, data in pw.items():
            name = data.get("name", f"Protocol {proto_id}")
            perf_daily = _daily_avg(data.get("performance", []), "date", "value")
            diff_daily = _daily_avg(data.get("difficulty", []), "date", "value")
            if perf_daily:
                lines.append(f"{name} performance (daily avg): {[(d, round(v, 3)) for d, v in perf_daily]}.")
            if diff_daily:
                lines.append(f"{name} difficulty (daily avg): {[(d, round(v, 3)) for d, v in diff_daily]}.")
            if data.get("adherence_pct") is not None:
                lines.append(f"{name} adherence: {data['adherence_pct']:.0%}.")

    # Learning rates (use protocol names when available)
    pw_for_names = metrics.get("protocol_wise") or {}
    if metrics.get("learning_rates"):
        lr = metrics["learning_rates"]
        if lr:
            parts = []
            for x in lr:
                pid = x.get("protocol_id")
                name = (pw_for_names.get(str(pid)) or pw_for_names.get(pid) or {}).get("name", f"Protocol {pid}")
                parts.append(f"{name}={round(x.get('learning_rate', 0), 4)}")
            lines.append(f"Learning rates by protocol: {parts}.")

    # Self-reports
    if metrics.get("self_reports"):
        lines.append(f"Self-reports: {metrics['self_reports']}.")

    # Drift (pre-detected)
    if metrics.get("drift_events"):
        lines.append(f"Detected drift: {[e.get('type') for e in metrics['drift_events']]}.")

    return "\n".join(lines) if lines else "No metrics available."


def _build_available_plots(metrics: dict) -> str:
    """List all plot refs with descriptions for the LLM."""
    lines = [
        "- adherence: overall planned vs done minutes over time (daily ratio)",
        "- performance: aggregate task success (0–1) over time",
        "- difficulty: aggregate task difficulty over time",
        "- sessions: minutes per day completed",
    ]
    # Self-reports: pain, mood, energy as separate plots (only list keys that exist)
    sr = metrics.get("self_reports")
    if isinstance(sr, dict):
        for key in ("pain", "mood", "energy"):
            if key in sr and sr[key]:
                lines.append(f"- {key}: patient-reported {key} over time (use ref {key})")
    elif isinstance(sr, list):
        keys = set()
        for r in sr:
            if isinstance(r, dict) and r.get("key"):
                keys.add(str(r["key"]).lower())
        for k in ("pain", "mood", "energy"):
            if k in keys:
                lines.append(f"- {k}: patient-reported {k} over time (use ref {k})")
    # Legacy: self_reports shows all self-report keys if any exist
    if sr:
        lines.append("- self_reports: all self-report keys (pain, mood, energy) combined; prefer specific refs (pain, mood, energy) when citing one")

    # Protocol-wise: 3 separate plots per protocol
    pw = metrics.get("protocol_wise")
    if isinstance(pw, dict):
        for proto_id, data in pw.items():
            name = data.get("name", f"Protocol {proto_id}")
            lines.append(f"- protocol_{proto_id}_adh: {name} adherence (use ref protocol_{proto_id}_adh)")
            lines.append(f"- protocol_{proto_id}_perf: {name} performance (use ref protocol_{proto_id}_perf)")
            lines.append(f"- protocol_{proto_id}_diff: {name} difficulty (use ref protocol_{proto_id}_diff)")
    return "\n".join(lines)


class MedGemmaTriageEngine:
    """
    LLM reasons over full metrics and produces triage conclusion.
    Uses swappable TriageLLM provider (MedGemma, OpenAI-compatible API, or rule-only).
    """

    def __init__(self, provider: "TriageLLM | None" = None, use_medgemma: bool = False):
        """
        Args:
            provider: Swappable LLM. If None, uses get_provider(use_medgemma).
            use_medgemma: When provider is None and no CRTV_LLM_PROVIDER, selects medgemma if True else rule.
        """
        self._provider = provider if provider is not None else get_provider(use_medgemma=use_medgemma)

    def conclude(self, metrics: dict[str, Any]) -> MedGemmaConclusion:
        """
        Feed all metrics to LLM; get structured triage conclusion.
        On failure or empty response -> rule-based fallback.
        """
        template = _load_triage_prompt()
        metrics_str = _metrics_to_prompt(metrics)
        plots_str = _build_available_plots(metrics)
        prompt = template.replace("{metrics}", metrics_str).replace("{available_plots}", plots_str)
        cache_key = None
        pid = metrics.get("patient_id")
        week = metrics.get("checkpoint_week")
        if pid is not None and week is not None:
            prov = os.environ.get("CRTV_LLM_PROVIDER", "").lower()
            if prov == "openai":
                model = os.environ.get("CRTV_OPENAI_MODEL", "gpt-5-mini")
            elif prov == "medgemma":
                model = os.environ.get("MEDGEMMA_MODEL", "google/medgemma-4b-it")
            else:
                model = prov or "rule"
            cache_key = f"triage_v4_{pid}_{model}_{week}"
        raw = self._provider.generate(prompt, cache_key=cache_key)
        if raw:
            result = self._parse_response(raw)
            if result.headline == "Needs manual review" and not result.reasons:
                logger.info("TriageEngine: LLM response could not be parsed, using safe default")
            return result
        logger.debug("TriageEngine: empty LLM response, using rule-based fallback")
        return self._rule_based_fallback(metrics)

    def _parse_response(self, text: str) -> MedGemmaConclusion:
        """Extract and parse JSON from LLM response. Handles markdown code blocks and nested JSON."""
        text = (text or "").strip()
        # Strip markdown code blocks if present
        if "```" in text:
            for delim in ("```json", "```"):
                if delim in text:
                    start = text.find(delim) + len(delim)
                    rest = text[start:].strip()
                    end = rest.find("```")
                    text = rest[:end].strip() if end >= 0 else rest
                    break
        # Find first { and extract full object via brace-counting (handles nested JSON)
        json_str = _extract_json_object(text)
        if not json_str:
            logger.debug("TriageEngine: no JSON object found in LLM response (len=%d)", len(text))
            return _safe_conclusion()
        try:
            data = json.loads(json_str)
            headline = str(data.get("headline") or "Needs review").strip()[:200]
            observations: list[ObservationItem] = []
            reasons: list[str] = []
            raw_obs = data.get("observations")
            raw_evidence = data.get("evidence")
            raw_reasons = data.get("reasons")
            if isinstance(raw_obs, list):
                for e in raw_obs[:3]:
                    if isinstance(e, dict) and e.get("text"):
                        refs = e.get("refs")
                        refs = [str(r).strip() for r in refs] if isinstance(refs, list) else []
                        att = e.get("attention")
                        att = int(att) if att in (1, 2, 3) else 2
                        observations.append(ObservationItem(text=str(e["text"]).strip(), attention=att, refs=refs))
                        reasons.append(str(e["text"]).strip())
            elif isinstance(raw_evidence, list):
                for e in raw_evidence[:3]:
                    if isinstance(e, dict) and e.get("text"):
                        refs = e.get("refs")
                        refs = [str(r).strip() for r in refs] if isinstance(refs, list) else []
                        observations.append(ObservationItem(text=str(e["text"]).strip(), attention=2, refs=refs))
                        reasons.append(str(e["text"]).strip())
                    elif isinstance(e, str) and e.strip():
                        observations.append(ObservationItem(text=e.strip(), attention=2, refs=[]))
                        reasons.append(e.strip())
            elif isinstance(raw_reasons, list):
                for r in raw_reasons[:3]:
                    if r:
                        t = str(r).strip()
                        observations.append(ObservationItem(text=t, attention=2, refs=[]))
                        reasons.append(t)
            elif isinstance(raw_reasons, str) and raw_reasons.strip():
                t = raw_reasons.strip()
                observations.append(ObservationItem(text=t, attention=2, refs=[]))
                reasons = [t]
            actions = []
            for a in data.get("recommended_actions", [])[:3]:
                if isinstance(a, dict):
                    at = str(a.get("action_type", "message")).lower()
                    if at == "escalate_to_clinician":
                        continue
                    actions.append({"action_type": at, "params": a.get("params", {})})
            disp = str(data.get("disposition", "TRIAGE")).upper()
            if disp not in ("NO_ACTION", "TRIAGE"):
                disp = "TRIAGE"
            sev = str(data.get("severity", "medium")).lower()
            if sev not in ("low", "medium", "high"):
                sev = "medium"
            conf = float(data.get("confidence", 0.5))
            return MedGemmaConclusion(headline=headline, reasons=reasons, observations=observations, recommended_actions=actions, disposition=disp, severity=sev, confidence=conf)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.debug("TriageEngine: parse error %s", e)
            return _safe_conclusion()

    def _rule_based_fallback(self, metrics: dict) -> MedGemmaConclusion:
        """Fallback when MedGemma unavailable."""
        adherence = metrics.get("adherence", {})
        adh_pct = adherence.get("adherence_minutes")
        drift = metrics.get("drift_events", [])
        if adh_pct is not None and adh_pct < 0.6:
            return MedGemmaConclusion(
                headline="Adherence below threshold",
                reasons=[f"Adherence {adh_pct:.0%} (target 60%)"],
                observations=[ObservationItem(text=f"Adherence {adh_pct:.0%} (target 60%)", attention=2, refs=["adherence"])],
                recommended_actions=[{"action_type": "message", "params": {"template": "adherence_nudge"}}],
                disposition="TRIAGE",
                severity="medium",
                confidence=0.7,
            )
        if any(d.get("type") == "PLATEAU" for d in drift):
            return MedGemmaConclusion(
                headline="Progress plateau detected",
                reasons=["Learning rate near zero over recent sessions"],
                observations=[ObservationItem(text="Learning rate near zero over recent sessions", attention=2, refs=["performance", "difficulty"])],
                recommended_actions=[],
                disposition="TRIAGE",
                severity="low",
                confidence=0.6,
            )
        return MedGemmaConclusion(
            headline="No action needed",
            reasons=[],
            observations=[],
            recommended_actions=[],
            disposition="NO_ACTION",
            severity="none",
            confidence=0.5,
        )
