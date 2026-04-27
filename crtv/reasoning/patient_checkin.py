"""
PatientCheckInEngine - LLM writes personalized weekly reports for patients.
Uses the same rich metrics the triage engine sees, but with a patient-facing prompt.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crtv.reasoning.llm_providers import get_provider
from crtv.reasoning.medgemma_triage import (
    _extract_json_object,
    _filter_metrics_to_last_weeks,
    _metrics_to_prompt,       # same full metrics the triage LLM sees
    _build_available_plots,
)

logger = logging.getLogger("crtv.llm")

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
if os.environ.get("CRTV_PROMPTS_DIR"):
    _PROMPTS_DIR = Path(os.environ["CRTV_PROMPTS_DIR"])


@dataclass
class PatientCheckIn:
    wins: str           # LLM-written prose
    to_improve: str     # LLM-written prose, or empty
    check_in: str       # LLM-written, empty most weeks
    tone: str           # proud, steady, gentle
    progress: list[dict] = field(default_factory=list)  # visual bars from data


def _load_checkin_prompt() -> str:
    path = _PROMPTS_DIR / "patient_checkin.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to load patient check-in prompt from %s: %s", path, e)
    return ""


def _proto_name(proto_id, protocol_wise: dict) -> str:
    pw = protocol_wise or {}
    data = pw.get(str(proto_id)) or pw.get(proto_id) or {}
    name = data.get("name", "")
    if name and name not in (f"Protocol {proto_id}", f"Exercise {proto_id}"):
        return name
    return f"Exercise {proto_id}"


def _compute_progress(metrics: dict) -> list[dict]:
    """Compute visual progress bar data from raw metrics. This is the ONLY code-generated part."""
    metrics = _filter_metrics_to_last_weeks(metrics, weeks=3)
    pw = metrics.get("protocol_wise") or {}
    adherence = metrics.get("adherence") or {}
    progress = []

    # Overall adherence
    adh_pct = adherence.get("adherence_minutes")
    if adh_pct is not None:
        progress.append({
            "label": "Completion",
            "current": round(adh_pct * 100),
            "previous": None,
            "unit": "%",
            "direction": "up" if adh_pct >= 0.7 else ("flat" if adh_pct >= 0.4 else "down"),
        })

    # Per-protocol performance trends
    for proto_id, data in pw.items():
        if not isinstance(data, dict):
            continue
        name = _proto_name(proto_id, pw)
        perf = data.get("performance") or []
        diff = data.get("difficulty") or []

        if perf and len(perf) >= 3:
            vals = [p.get("value", p.get("performance_mean", 0)) for p in perf]
            vals = [v for v in vals if v is not None]
            if len(vals) >= 3:
                first = sum(vals[:len(vals)//2]) / max(len(vals[:len(vals)//2]), 1)
                last = sum(vals[len(vals)//2:]) / max(len(vals[len(vals)//2:]), 1)
                if abs(last - first) > 0.03:
                    progress.append({
                        "label": name,
                        "previous": round(first * 100),
                        "current": round(last * 100),
                        "unit": "%",
                        "direction": "up" if last > first else "down",
                    })

        if diff and len(diff) >= 2:
            vals = [d.get("value", d.get("difficulty_mean", 0)) for d in diff]
            vals = [v for v in vals if v and v > 0]
            if len(vals) >= 2:
                first_half = sum(vals[:len(vals)//2]) / max(len(vals[:len(vals)//2]), 1)
                second_half = sum(vals[len(vals)//2:]) / max(len(vals[len(vals)//2:]), 1)
                if first_half > 0:
                    pct = (second_half - first_half) / first_half
                    if pct > 0.05:
                        progress.append({
                            "label": f"{name} difficulty",
                            "previous": round(first_half * 100),
                            "current": round(second_half * 100),
                            "unit": "",
                            "direction": "up",
                        })

    return progress[:4]


def _rule_fallback(metrics: dict) -> PatientCheckIn:
    """Fallback when no LLM is configured. Generates from data patterns."""
    metrics = _filter_metrics_to_last_weeks(metrics, weeks=3)
    pw = metrics.get("protocol_wise") or {}
    adherence = metrics.get("adherence") or {}
    sessions = metrics.get("sessions") or []
    adh_pct = adherence.get("adherence_minutes")
    done = adherence.get("done_total", 0)

    wins_parts = []
    to_improve = ""

    # Find difficulty increases
    for proto_id, data in pw.items():
        if not isinstance(data, dict):
            continue
        name = _proto_name(proto_id, pw)
        diff = data.get("difficulty") or []
        if len(diff) >= 2:
            vals = [d.get("value", d.get("difficulty_mean", 0)) for d in diff]
            vals = [v for v in vals if v and v > 0]
            if len(vals) >= 2:
                first = sum(vals[:len(vals)//2]) / max(len(vals[:len(vals)//2]), 1)
                last = sum(vals[len(vals)//2:]) / max(len(vals[len(vals)//2:]), 1)
                if first > 0 and (last - first) / first > 0.05:
                    pct = (last - first) / first
                    wins_parts.append(f"{name} difficulty went up {pct:.0%}")

        perf = data.get("performance") or []
        proto_adh = data.get("adherence_pct")
        if proto_adh is not None and proto_adh < 0.2:
            to_improve = f"{name} could use some attention — even one session helps."

        if perf and len(perf) >= 3:
            vals = [p.get("value", p.get("performance_mean", 0)) for p in perf]
            if vals and sum(vals[-2:]) / 2 > sum(vals[:2]) / 2 + 0.05:
                wins_parts.append(f"{name} performance trending up")

    if adh_pct is not None and adh_pct >= 0.8:
        wins_parts.append(f"{done:.0f} minutes completed — {adh_pct:.0%} of target")
    elif sessions:
        wins_parts.append(f"{len(sessions)} sessions logged this period")

    wins = ". ".join(wins_parts[:3]) + "." if wins_parts else "Sessions logged this period."

    if adh_pct is not None and adh_pct < 0.3 and not to_improve:
        to_improve = "Try fitting in one 10-minute session to build momentum."

    tone = "proud" if len(wins_parts) >= 2 else ("gentle" if adh_pct and adh_pct < 0.3 else "steady")

    return PatientCheckIn(
        wins=wins,
        to_improve=to_improve,
        check_in="",
        tone=tone,
        progress=_compute_progress(metrics),
    )


class PatientCheckInEngine:
    """Feed the LLM full patient metrics, get back a written report."""

    def __init__(self, provider=None, use_medgemma: bool = False):
        self._provider = provider if provider is not None else get_provider(use_medgemma=use_medgemma)

    def generate(self, metrics: dict[str, Any]) -> PatientCheckIn:
        # Always compute visual progress from data
        progress = _compute_progress(metrics)

        # Build prompt with full metrics (same data the triage engine sees)
        template = _load_checkin_prompt()
        if not template:
            result = _rule_fallback(metrics)
            result.progress = progress
            return result

        metrics_str = _metrics_to_prompt(metrics)
        prompt = template.replace("{metrics}", metrics_str)

        # Cache key separate from triage
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
            cache_key = f"patient_report_v3_{pid}_{model}_{week}"

        raw = self._provider.generate(prompt, cache_key=cache_key)
        if raw:
            result = self._parse_response(raw)
            if result is not None:
                result.progress = progress
                return result
            logger.info("PatientCheckIn: LLM response could not be parsed, falling back")

        # No LLM or failed parse
        result = _rule_fallback(metrics)
        result.progress = progress
        return result

    def _parse_response(self, text: str) -> PatientCheckIn | None:
        text = (text or "").strip()
        if "```" in text:
            for delim in ("```json", "```"):
                if delim in text:
                    start = text.find(delim) + len(delim)
                    rest = text[start:].strip()
                    end = rest.find("```")
                    text = rest[:end].strip() if end >= 0 else rest
                    break

        json_str = _extract_json_object(text)
        if not json_str:
            logger.debug("PatientCheckIn: no JSON found in LLM response (len=%d)", len(text))
            return None

        try:
            data = json.loads(json_str)

            wins = data.get("wins", "")
            if isinstance(wins, list):
                wins = ". ".join(str(w) for w in wins if w)
            wins = str(wins).strip()
            if not wins:
                return None  # LLM gave empty wins — fall back

            to_improve = data.get("to_improve", "")
            if isinstance(to_improve, list):
                to_improve = ". ".join(str(t) for t in to_improve if t)
            to_improve = str(to_improve).strip()

            check_in = str(data.get("check_in", "")).strip()

            tone = str(data.get("tone", "steady")).strip()
            if tone not in ("proud", "steady", "gentle"):
                tone = "steady"

            return PatientCheckIn(
                wins=wins[:300],
                to_improve=to_improve[:200],
                check_in=check_in[:200],
                tone=tone,
                progress=[],  # filled by caller
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.debug("PatientCheckIn: parse error %s", e)
            return None
