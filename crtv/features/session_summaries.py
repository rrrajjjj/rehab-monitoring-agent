"""SessionSignalSummarizer - per-session difficulty/performance features."""

from dataclasses import dataclass, field
from collections import defaultdict
import math

from crtv.domain.models import (
    PatientHistoryBundle,
    Session,
    DifficultyTick,
    PerformancePoint,
)
from crtv.builders.difficulty_builder import build_difficulty_ticks
from crtv.builders.performance_builder import build_performance_points
from crtv.builders.kinematics_builder import build_kinematics_points


@dataclass
class SessionSignalSummary:
    """Per-session summary."""

    session_id: int
    protocol_id: int
    duration_sec: float
    short_session: bool
    difficulty_end: dict[str, float]
    difficulty_mean: dict[str, float]
    difficulty_slope: dict[str, float]
    performance_mean: float
    performance_min: float
    performance_max: float
    performance_slope: float
    data_quality_flags: list[str]
    kinematics_median: dict[str, float] = field(default_factory=dict)
    kinematics_slope: dict[str, float] = field(default_factory=dict)


def _robust_slope(x: list[float], y: list[float]) -> float:
    """Simple linear slope; 0 if insufficient points."""
    if len(x) < 2 or len(y) < 2 or len(x) != len(y):
        return 0.0
    n = len(x)
    sx = sum(x)
    sy = sum(y)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    sx2 = sum(xi * xi for xi in x)
    denom = n * sx2 - sx * sx
    if abs(denom) < 1e-10:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _mad(values: list[float]) -> float:
    """Median absolute deviation."""
    if not values:
        return 0.0
    med = sorted(values)[len(values) // 2]
    return sorted(abs(v - med) for v in values)[len(values) // 2]


class SessionSignalSummarizer:
    """Summarize difficulty and performance per session."""

    def __init__(self, short_session_alpha: float = 0.5):
        """short_session if duration < alpha * prescribed_minutes."""
        self.short_session_alpha = short_session_alpha

    def summarize(self, bundle: PatientHistoryBundle) -> dict[int, SessionSignalSummary]:
        """Per session: difficulty end/mean/slope, performance mean/min/max/slope, flags."""
        ticks = build_difficulty_ticks(bundle.difficulty_rows)
        points = build_performance_points(bundle.performance_rows)
        k_points = build_kinematics_points(bundle.kinematics_rows) if bundle.kinematics_rows else []
        session_by_id = {s.session_id: s for s in bundle.sessions}
        presc_by_id = {p.prescription_id: p for p in bundle.prescriptions}
        presc_duration: dict[int, int] = {}
        for s in bundle.sessions:
            p = presc_by_id.get(s.prescription_id)
            if p:
                presc_duration[s.session_id] = p.session_duration_min

        ticks_by_session: dict[int, list[DifficultyTick]] = defaultdict(list)
        for t in ticks:
            ticks_by_session[t.session_id].append(t)

        points_by_session: dict[int, list[PerformancePoint]] = defaultdict(list)
        for p in points:
            points_by_session[p.session_id].append(p)
        k_by_session: dict[int, list] = defaultdict(list)
        for kp in k_points:
            k_by_session[kp.session_id].append(kp)

        result: dict[int, SessionSignalSummary] = {}
        for session_id, sess in session_by_id.items():
            prescribed_min = presc_duration.get(session_id, sess.duration_sec / 60)
            short_session = sess.duration_sec < self.short_session_alpha * prescribed_min * 60
            flags: list[str] = []
            if sess.status == "ABORTED":
                flags.append("aborted")
            if not sess.log_parsed:
                flags.append("log_not_parsed")

            sess_ticks = sorted(ticks_by_session.get(session_id, []), key=lambda t: t.t_sec)
            sess_points = sorted(points_by_session.get(session_id, []), key=lambda p: p.t_sec)

            difficulty_end: dict[str, float] = {}
            difficulty_mean: dict[str, float] = {}
            difficulty_slope: dict[str, float] = {}
            if sess_ticks:
                keys = set()
                for t in sess_ticks:
                    keys.update(t.modulators.keys())
                for k in keys:
                    vals = [t.modulators.get(k, 0.0) for t in sess_ticks]
                    ts = [t.t_sec for t in sess_ticks]
                    difficulty_end[k] = vals[-1] if vals else 0.0
                    difficulty_mean[k] = sum(vals) / len(vals) if vals else 0.0
                    difficulty_slope[k] = _robust_slope(ts, vals)

            perf_vals = [p.performance for p in sess_points]
            perf_ts = [p.t_sec for p in sess_points]
            perf_mean = sum(perf_vals) / len(perf_vals) if perf_vals else 0.0
            perf_min = min(perf_vals) if perf_vals else 0.0
            perf_max = max(perf_vals) if perf_vals else 0.0
            perf_slope = _robust_slope(perf_ts, perf_vals) if len(perf_vals) >= 2 else 0.0

            k_median: dict[str, float] = {}
            k_slope: dict[str, float] = {}
            k_list = sorted(k_by_session.get(session_id, []), key=lambda x: x.t_sec)
            for field in ["mqi", "workspace_volume", "smoothness", "velocity", "pause_ratio"]:
                pairs = [(k.t_sec, getattr(k, field)) for k in k_list if getattr(k, field) is not None]
                if pairs:
                    ts, vals = zip(*pairs)
                    k_median[field] = sum(vals) / len(vals)
                    if len(vals) >= 2:
                        k_slope[field] = _robust_slope(list(ts), list(vals))

            result[session_id] = SessionSignalSummary(
                session_id=session_id,
                protocol_id=sess.protocol_id,
                duration_sec=sess.duration_sec,
                short_session=short_session,
                difficulty_end=difficulty_end,
                difficulty_mean=difficulty_mean,
                difficulty_slope=difficulty_slope,
                performance_mean=perf_mean,
                performance_min=perf_min,
                performance_max=perf_max,
                performance_slope=perf_slope,
                data_quality_flags=flags,
                kinematics_median=k_median,
                kinematics_slope=k_slope,
            )
        return result
