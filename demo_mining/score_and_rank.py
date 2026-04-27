"""
Score mined windows for demo-worthiness and rank them.

Reads demo_mining/windows.jsonl, emits demo_mining/windows_ranked.jsonl with
alert tags, a per-window interest score, the rule-based-fallback disposition,
and focal-week derived stats. Thresholds mirror prompts/triage_system.txt so
the ranker speaks the same language as the LLM prompt.

Alert taxonomy:
  ADHERENCE_DROP          (mild/severe)   focal adherence vs patient baseline + absolute
  HIGH_PAIN               (mild/severe)   pain >=6 any day / pain >=8 on multiple days
  LEARNING_SATURATION     (mild/severe)   performance plateau without difficulty rise
  PROTOCOL_AVOIDANCE      (single)        one protocol <60% while others >=80%
  PROTOCOL_PERFORMANCE    (single)        one protocol far below peer protocols
  PROTOCOL_PLATEAU        (single)        per-protocol LR <0.02 while others >0.02
  CHECKIN_SIGNAL          (single)        self-report barrier flagged in focal week
  DATA_ANOMALY            (single)        adherence_minutes > 1.5 (prescription/log mismatch)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("demo_mining.score_and_rank")


# ----- thresholds (from prompts/triage_system.txt) -----------------------------
ADHERENCE_LOW = 0.60
ADHERENCE_SEVERE = 0.40
ADHERENCE_BASELINE_DROP = 0.20  # focal must be this much below patient mean to fire
PAIN_MILD = 7.0                  # raised from 6; baseline stroke pain is often 5-6
PAIN_SEVERE = 8.0
PAIN_SEVERE_MIN_DAYS = 3         # relaxed from 5 since focal window is 7 days
CHECKIN_PAIN_MIN = 7.0           # pain signal for check-in also raised
CHECKIN_MOOD_MAX = 2             # mood <=2 (was 3)
CHECKIN_ENERGY_MAX = 2           # energy <=2
LEARNING_RATE_PLATEAU = 0.01     # tightened from 0.02
LEARNING_RATE_ACTIVE = 0.03      # peer must clearly be progressing
PLATEAU_MIN_SESSIONS = 5         # raised from 3 (need real signal)
PLATEAU_MAX_PERF_RANGE = 0.10    # perf must be genuinely flat, not noisy
PLATEAU_MAX_DIFF_RANGE = 0.10
PROTOCOL_AVOIDANCE_LOW = 0.60
PROTOCOL_AVOIDANCE_PEER = 0.80
PROTOCOL_PERF_GAP = 0.25         # raised from 0.20
PROTOCOL_MIN_POINTS = 3          # per-protocol minimum data points to compare
ADHERENCE_ANOMALY = 1.5

# ----- scoring weights --------------------------------------------------------
WEIGHT = {
    "ADHERENCE_DROP": 1.0,
    "HIGH_PAIN": 1.0,
    "LEARNING_SATURATION": 2.0,  # rarer and more impressive
    "PROTOCOL_AVOIDANCE": 1.5,   # distinctive demo signal
    "PROTOCOL_PERFORMANCE": 1.5,
    "PROTOCOL_PLATEAU": 1.5,
    "CHECKIN_SIGNAL": 0.5,
    "DATA_ANOMALY": 0.0,         # informational, not a demo win
}
SEVERITY_BONUS = {"mild": 1.0, "severe": 1.5, "single": 1.0}


@dataclass
class Alert:
    tag: str
    severity: str          # mild | severe | single
    value: float | None    # the observed number (for sorting/UI)
    detail: str            # short human-readable


# ---------- helpers -----------------------------------------------------------


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _focal_days(metrics: dict, focal_start: str, focal_end: str) -> list[dict]:
    days = metrics.get("adherence", {}).get("days", []) or []
    return [d for d in days if focal_start <= str(d.get("date", ""))[:10] < focal_end]


def _focal_adherence(metrics: dict, focal_start: str, focal_end: str) -> float | None:
    days = _focal_days(metrics, focal_start, focal_end)
    planned = sum(_as_float(d.get("planned_min")) or 0 for d in days)
    done = sum(_as_float(d.get("done_min")) or 0 for d in days)
    if planned <= 0:
        return None
    return done / planned


def _focal_sessions(metrics: dict, focal_start: str, focal_end: str) -> list[dict]:
    return [s for s in (metrics.get("sessions") or []) if focal_start <= str(s.get("start_time", ""))[:10] < focal_end]


def _focal_pain_by_day(metrics: dict, focal_start: str, focal_end: str) -> dict[str, float]:
    """Daily max pain (0-10) inside focal week."""
    out: dict[str, float] = {}
    for r in metrics.get("self_reports") or []:
        if str(r.get("key", "")).lower() != "pain":
            continue
        ts = str(r.get("timestamp", ""))[:10]
        if not (focal_start <= ts < focal_end):
            continue
        v = _as_float(r.get("value"))
        if v is None:
            continue
        out[ts] = max(out.get(ts, 0.0), v)
    return out


def _focal_checkin_signals(metrics: dict, focal_start: str, focal_end: str) -> list[str]:
    """Return list of self-report barrier tags flagged in focal week (non-trivial values)."""
    tags: list[str] = []
    for r in metrics.get("self_reports") or []:
        ts = str(r.get("timestamp", ""))[:10]
        if not (focal_start <= ts < focal_end):
            continue
        key = str(r.get("key", "")).lower()
        v = _as_float(r.get("value"))
        if v is None:
            continue
        if key == "pain" and v >= CHECKIN_PAIN_MIN:
            tags.append(f"pain={v:.0f}")
        elif key == "mood" and v <= CHECKIN_MOOD_MAX:
            tags.append(f"mood={v:.0f}")
        elif key == "energy" and v <= CHECKIN_ENERGY_MAX:
            tags.append(f"energy={v:.0f}")
    return tags


def _linear_slope(series: list[tuple[str, float]]) -> float:
    if len(series) < 2:
        return 0.0
    xs = list(range(len(series)))
    ys = [v for _, v in series]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sx2 = sum(x * x for x in xs)
    denom = n * sx2 - sx * sx
    if abs(denom) < 1e-10:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _rule_disposition(metrics: dict, drift_events: list) -> str:
    """Mirror MedGemmaTriageEngine._rule_based_fallback without running it."""
    adh = _as_float((metrics.get("adherence") or {}).get("adherence_minutes"))
    if adh is not None and adh < 0.6:
        return "TRIAGE:adherence"
    if any(e.get("type") == "PLATEAU" for e in drift_events or []):
        return "TRIAGE:plateau"
    return "NO_ACTION"


# ---------- alert generators --------------------------------------------------


def _alert_adherence(row: dict, patient_mean: float | None) -> Alert | None:
    metrics = row["metrics"]
    focal_adh = _focal_adherence(metrics, row["focal_week_start"], row["focal_week_end"])
    if focal_adh is None:
        return None
    # Hard anomaly short-circuits adherence-drop logic (don't call a 0% reading inside a 400% noise weird).
    if focal_adh > ADHERENCE_ANOMALY:
        return None
    below_abs_severe = focal_adh < ADHERENCE_SEVERE
    below_abs_mild = focal_adh < ADHERENCE_LOW
    below_baseline = (
        patient_mean is not None
        and patient_mean - focal_adh >= ADHERENCE_BASELINE_DROP
    )
    if not (below_abs_mild or below_baseline):
        return None
    severity = "severe" if below_abs_severe and below_baseline else "mild"
    pm_str = f", patient mean {patient_mean:.0%}" if patient_mean is not None else ""
    return Alert(
        tag="ADHERENCE_DROP",
        severity=severity,
        value=focal_adh,
        detail=f"focal adherence {focal_adh:.0%}{pm_str}",
    )


def _alert_high_pain(row: dict) -> Alert | None:
    pain = _focal_pain_by_day(row["metrics"], row["focal_week_start"], row["focal_week_end"])
    if not pain:
        return None
    max_pain = max(pain.values())
    severe_days = sum(1 for v in pain.values() if v >= PAIN_SEVERE)
    high_days = sum(1 for v in pain.values() if v >= PAIN_MILD)
    if severe_days >= PAIN_SEVERE_MIN_DAYS:
        return Alert("HIGH_PAIN", "severe", max_pain, f"pain >={PAIN_SEVERE:.0f} on {severe_days} days")
    if max_pain >= PAIN_MILD and high_days >= 2:
        return Alert("HIGH_PAIN", "mild", max_pain, f"peak pain {max_pain:.0f} on {high_days} days")
    return None


def _alert_learning_saturation(row: dict) -> Alert | None:
    """
    Performance plateau without difficulty rise.
    Over the 28-day window: perf slope near zero AND difficulty slope near zero,
    with enough sessions to matter.
    """
    metrics = row["metrics"]
    # Build daily aggregate perf + difficulty series from metrics
    sess_by_id = {s["session_id"]: s for s in metrics.get("sessions") or []}
    perf_by_date: dict[str, list[float]] = defaultdict(list)
    diff_by_date: dict[str, list[float]] = defaultdict(list)
    for p in metrics.get("performance") or []:
        sid = p.get("session_id")
        dt = str((sess_by_id.get(sid) or {}).get("start_time", ""))[:10]
        if dt:
            perf_by_date[dt].append(_as_float(p.get("performance_mean")) or 0.0)
    for d in metrics.get("difficulty") or []:
        sid = d.get("session_id")
        dt = str((sess_by_id.get(sid) or {}).get("start_time", ""))[:10]
        if dt:
            diff_by_date[dt].append(_as_float(d.get("difficulty_mean")) or 0.0)
    if len(perf_by_date) < PLATEAU_MIN_SESSIONS:
        return None
    perf_series = sorted((d, sum(v) / len(v)) for d, v in perf_by_date.items())
    diff_series = sorted((d, sum(v) / len(v)) for d, v in diff_by_date.items())
    perf_vals = [v for _, v in perf_series]
    diff_vals = [v for _, v in diff_series]
    perf_range = max(perf_vals) - min(perf_vals) if perf_vals else 0.0
    diff_range = max(diff_vals) - min(diff_vals) if diff_vals else 0.0
    # Only fire when the series is both (a) shallow-sloped and (b) genuinely flat (small range).
    # Range filter kills the false positives from noisy daily aggregates.
    perf_slope = _linear_slope(perf_series)
    diff_slope = _linear_slope(diff_series)
    if (
        abs(perf_slope) < LEARNING_RATE_PLATEAU
        and abs(diff_slope) < LEARNING_RATE_PLATEAU
        and perf_range < PLATEAU_MAX_PERF_RANGE
        and diff_range < PLATEAU_MAX_DIFF_RANGE
    ):
        severity = "severe" if len(perf_by_date) >= 10 else "mild"
        return Alert(
            "LEARNING_SATURATION",
            severity,
            perf_slope,
            f"perf flat {perf_range:.2f} range, diff flat {diff_range:.2f} range, {len(perf_by_date)} days",
        )
    return None


def _alert_protocol_avoidance(row: dict) -> Alert | None:
    pw = (row["metrics"].get("protocol_wise") or {})
    entries = [(pid, v) for pid, v in pw.items() if isinstance(v, dict) and v.get("adherence_pct") is not None]
    if len(entries) < 2:
        return None
    low = [(pid, v) for pid, v in entries if (v.get("adherence_pct") or 0) < PROTOCOL_AVOIDANCE_LOW]
    high = [(pid, v) for pid, v in entries if (v.get("adherence_pct") or 0) >= PROTOCOL_AVOIDANCE_PEER]
    if not low or not high:
        return None
    worst_pid, worst_v = min(low, key=lambda x: x[1].get("adherence_pct") or 0)
    name = worst_v.get("name") or f"protocol {worst_pid}"
    return Alert(
        "PROTOCOL_AVOIDANCE",
        "single",
        worst_v.get("adherence_pct"),
        f"{name} adherence {(worst_v.get('adherence_pct') or 0):.0%} while {len(high)} other protocol(s) >={PROTOCOL_AVOIDANCE_PEER:.0%}",
    )


def _alert_protocol_performance(row: dict) -> Alert | None:
    pw = row["metrics"].get("protocol_wise") or {}
    proto_means: dict[str, float] = {}
    for pid, v in pw.items():
        if not isinstance(v, dict):
            continue
        perf_points = [_as_float(p.get("value")) for p in v.get("performance", []) or []]
        perf_points = [p for p in perf_points if p is not None]
        if len(perf_points) >= PROTOCOL_MIN_POINTS:
            proto_means[pid] = sum(perf_points) / len(perf_points)
    if len(proto_means) < 2:
        return None
    worst_pid, worst_val = min(proto_means.items(), key=lambda x: x[1])
    peers = [v for pid, v in proto_means.items() if pid != worst_pid]
    peer_mean = sum(peers) / len(peers)
    gap = peer_mean - worst_val
    if gap < PROTOCOL_PERF_GAP:
        return None
    mean_all = statistics.mean(proto_means.values())
    name = (pw.get(worst_pid) or {}).get("name") or f"protocol {worst_pid}"
    return Alert(
        "PROTOCOL_PERFORMANCE",
        "single",
        worst_val,
        f"{name} perf {worst_val:.2f} vs peer mean {mean_all:.2f} (gap {gap:.2f})",
    )


def _alert_protocol_plateau(row: dict) -> Alert | None:
    lrs = row["metrics"].get("learning_rates") or []
    if len(lrs) < 2:
        return None
    flat = [lr for lr in lrs if abs(_as_float(lr.get("learning_rate")) or 0) < LEARNING_RATE_PLATEAU]
    active = [lr for lr in lrs if abs(_as_float(lr.get("learning_rate")) or 0) >= LEARNING_RATE_ACTIVE]
    if not flat or not active:
        return None
    pw = row["metrics"].get("protocol_wise") or {}
    flat_pid = str(flat[0].get("protocol_id"))
    name = (pw.get(flat_pid) or {}).get("name") or f"protocol {flat_pid}"
    return Alert(
        "PROTOCOL_PLATEAU",
        "single",
        _as_float(flat[0].get("learning_rate")),
        f"{name} LR {(_as_float(flat[0].get('learning_rate')) or 0):+.3f} while {len(active)} other protocol(s) progressing",
    )


def _alert_checkin(row: dict) -> Alert | None:
    tags = _focal_checkin_signals(row["metrics"], row["focal_week_start"], row["focal_week_end"])
    if not tags:
        return None
    return Alert("CHECKIN_SIGNAL", "single", None, "; ".join(tags[:4]))


def _alert_data_anomaly(row: dict) -> Alert | None:
    adh = _as_float((row["metrics"].get("adherence") or {}).get("adherence_minutes"))
    if adh is None or adh <= ADHERENCE_ANOMALY:
        return None
    return Alert("DATA_ANOMALY", "single", adh, f"adherence reported {adh:.0%} (prescription/log mismatch)")


ALERT_FNS = [
    ("ADHERENCE_DROP", lambda row, ctx: _alert_adherence(row, ctx.get("patient_mean_adherence"))),
    ("HIGH_PAIN", lambda row, ctx: _alert_high_pain(row)),
    ("LEARNING_SATURATION", lambda row, ctx: _alert_learning_saturation(row)),
    ("PROTOCOL_AVOIDANCE", lambda row, ctx: _alert_protocol_avoidance(row)),
    ("PROTOCOL_PERFORMANCE", lambda row, ctx: _alert_protocol_performance(row)),
    ("PROTOCOL_PLATEAU", lambda row, ctx: _alert_protocol_plateau(row)),
    ("CHECKIN_SIGNAL", lambda row, ctx: _alert_checkin(row)),
    ("DATA_ANOMALY", lambda row, ctx: _alert_data_anomaly(row)),
]


# ---------- main scoring -------------------------------------------------------


def _patient_adherence_means(rows: list[dict]) -> dict[int, float]:
    """Mean of each patient's focal-week adherence, across their rows (capped at ANOMALY)."""
    per_patient: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        adh = _focal_adherence(row["metrics"], row["focal_week_start"], row["focal_week_end"])
        if adh is None or adh > ADHERENCE_ANOMALY:
            continue
        per_patient[row["patient_id"]].append(adh)
    return {pid: sum(vals) / len(vals) for pid, vals in per_patient.items() if vals}


def score_row(row: dict, patient_mean: float | None) -> tuple[list[Alert], float]:
    ctx = {"patient_mean_adherence": patient_mean}
    alerts: list[Alert] = []
    for _tag, fn in ALERT_FNS:
        try:
            a = fn(row, ctx)
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("alert %s failed on patient %s week %s: %s", _tag, row.get("patient_id"), row.get("focal_week_start"), e)
            a = None
        if a is not None:
            alerts.append(a)

    score = 0.0
    for a in alerts:
        score += WEIGHT.get(a.tag, 0.0) * SEVERITY_BONUS.get(a.severity, 1.0)
    # Magnitude bonus for adherence: lower = more dramatic
    for a in alerts:
        if a.tag == "ADHERENCE_DROP" and a.value is not None:
            score += max(0.0, 0.6 - a.value)  # 0%→+0.6, 60%→+0
        if a.tag == "HIGH_PAIN" and a.value is not None:
            score += max(0.0, (a.value - 6) * 0.2)  # 6→0, 10→+0.8
    return alerts, round(score, 4)


def run(in_path: Path, out_path: Path) -> int:
    rows = [json.loads(line) for line in in_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    logger.info("Loaded %d rows", len(rows))

    patient_means = _patient_adherence_means(rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    scored: list[dict] = []
    for row in rows:
        pm = patient_means.get(row["patient_id"])
        alerts, score = score_row(row, pm)
        focal_start, focal_end = row["focal_week_start"], row["focal_week_end"]
        focal_sessions = _focal_sessions(row["metrics"], focal_start, focal_end)
        focal_stats = {
            "focal_adherence": _focal_adherence(row["metrics"], focal_start, focal_end),
            "focal_session_count": len(focal_sessions),
            "focal_max_pain": (max(_focal_pain_by_day(row["metrics"], focal_start, focal_end).values()) if _focal_pain_by_day(row["metrics"], focal_start, focal_end) else None),
            "patient_mean_adherence": pm,
        }
        out_row = {
            **row,
            "alerts": [asdict(a) for a in alerts],
            "interest_score": score,
            "rule_disposition": _rule_disposition(row["metrics"], row.get("drift_events") or []),
            "focal_stats": focal_stats,
        }
        scored.append(out_row)

    scored.sort(key=lambda r: r["interest_score"], reverse=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in scored:
            f.write(json.dumps(r, default=str))
            f.write("\n")
    logger.info("Wrote %d scored rows -> %s", len(scored), out_path)
    return len(scored)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Score mined windows for demo-worthiness")
    p.add_argument("--in", dest="in_path", default="demo_mining/windows.jsonl")
    p.add_argument("--out", dest="out_path", default="demo_mining/windows_ranked.jsonl")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(Path(args.in_path), Path(args.out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
