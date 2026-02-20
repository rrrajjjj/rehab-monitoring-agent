"""Adherence computed from session + prescription + recording. No adherence table."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from collections import defaultdict

from crtv.domain.models import PatientHistoryBundle


@dataclass
class AdherenceReport:
    """Output of AdherenceCalculator.compute()."""

    patient_id: int
    window_start: date
    window_end: date
    planned_minutes: float
    done_minutes: float
    adherence_minutes: float | None
    adherence_sessions: int
    adherence_days: int
    per_day: dict[date, tuple[float, float]]  # planned, done per day
    evidence_map: dict[str, list[int]]  # occurrence_id -> session_ids


def expand_prescriptions(
    prescriptions: list,
    start_d: date,
    end_d: date,
) -> list[tuple[date, int, int]]:
    """
    Expand prescriptions into (date, protocol_id, session_duration_min) occurrences.
    For each date in [start_d, end_d) where weekday matches prescription.
    """
    occurrences: list[tuple[date, int, int]] = []
    d = start_d
    while d < end_d:
        wd = d.weekday()  # 0=Mon..6=Sun
        for p in prescriptions:
            if p.weekday == wd and p.start_date <= d <= p.end_date:
                occurrences.append((d, p.protocol_id, p.session_duration_min))
        d += timedelta(days=1)
    return occurrences


def match_sessions_to_occurrences(
    sessions: list,
    occurrences: list[tuple[date, int, int]],
) -> dict[tuple[date, int], list[int]]:
    """
    Match sessions to occurrences: same protocol_id, same date.
    Returns (date, protocol_id) -> [session_ids]
    """
    session_by_date_protocol: dict[tuple[date, int], list[int]] = defaultdict(list)
    for s in sessions:
        sess_date = s.start_time.date() if hasattr(s.start_time, 'date') else s.start_time
        session_by_date_protocol[(sess_date, s.protocol_id)].append(s.session_id)
    return dict(session_by_date_protocol)


class AdherenceCalculator:
    """Compute adherence from session + prescription only (duration from Session.duration_sec)."""

    def compute(self, bundle: PatientHistoryBundle) -> AdherenceReport:
        """
        Inputs: sessions, prescriptions from bundle.
        Session duration comes from recording_plus (already in Session.duration_sec).
        """
        start_d = bundle.start.date() if isinstance(bundle.start, datetime) else bundle.start
        end_d = bundle.end.date() if isinstance(bundle.end, datetime) else bundle.end

        occurrences = expand_prescriptions(bundle.prescriptions, start_d, end_d)
        matched = match_sessions_to_occurrences(bundle.sessions, occurrences)

        planned_total = sum(occ[2] for occ in occurrences)
        done_by_day: dict[date, float] = defaultdict(float)
        evidence_map: dict[str, list[int]] = {}
        days_with_any = set()

        for (d, protocol_id, duration_min) in occurrences:
            key = (d, protocol_id)
            sess_ids = matched.get(key, [])
            done_sec = 0.0
            for s in bundle.sessions:
                if s.session_id in sess_ids:
                    done_sec += s.duration_sec
            done_min = done_sec / 60.0
            done_by_day[d] += done_min
            if sess_ids:
                days_with_any.add(d)
            occ_id = f"{d}_{protocol_id}"
            evidence_map[occ_id] = sess_ids

        done_total = sum(done_by_day.values())
        adherence_minutes = done_total / planned_total if planned_total > 0 else None
        per_day = {
            d: (sum(o[2] for o in occurrences if o[0] == d), done_by_day.get(d, 0.0))
            for d in set(o[0] for o in occurrences)
        }

        return AdherenceReport(
            patient_id=bundle.patient_id,
            window_start=start_d,
            window_end=end_d,
            planned_minutes=planned_total,
            done_minutes=done_total,
            adherence_minutes=adherence_minutes,
            adherence_sessions=len(bundle.sessions),
            adherence_days=len(days_with_any),
            per_day=per_day,
            evidence_map=evidence_map,
        )
