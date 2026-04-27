"""
Shared metrics-builder: assembles the dict that MedGemmaTriageEngine consumes.

Used by HistoricalTriageRunner (live triage) and demo_mining/mine_features.py
(offline enumeration). Keeping this in one place ensures the inspection artifact
and the live pipeline see identical features.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from crtv.adapters import get_adapter
from crtv.adapters.database import DatabaseAdapter
from crtv.domain.models import PatientHistoryBundle
from crtv.features.adherence import (
    AdherenceCalculator,
    AdherenceReport,
    expand_prescriptions,
    match_sessions_to_occurrences,
)
from crtv.features.learning_rate import LearningRateEstimator
from crtv.features.session_summaries import SessionSignalSummarizer
from crtv.drift.detector import DriftDetector, PatientStateBuilder
from crtv.repositories.patient_history import PatientHistoryRepository

# Diagnostic protocols (not therapeutic): exclude from all triage/check-in reporting.
# 220 = OS_circleAR, 228 = OS_circleAR_horizontal.
DIAGNOSTIC_PROTOCOL_IDS: set[int] = {220, 228}


@dataclass
class CheckpointSnapshot:
    metrics: dict[str, Any]
    drift_events: list
    adherence: AdherenceReport
    bundle: PatientHistoryBundle


@dataclass
class MetricsBuilder:
    """
    Holds stateless calculators and produces a full checkpoint snapshot.
    Callers construct once and reuse across many (patient, checkpoint) pairs.
    """

    adapter: DatabaseAdapter
    repo: PatientHistoryRepository
    adherence_calc: AdherenceCalculator
    summarizer: SessionSignalSummarizer
    lr_estimator: LearningRateEstimator
    state_builder: PatientStateBuilder
    drift_detector: DriftDetector

    @classmethod
    def from_adapter(cls, adapter: DatabaseAdapter) -> "MetricsBuilder":
        return cls(
            adapter=adapter,
            repo=PatientHistoryRepository(adapter),
            adherence_calc=AdherenceCalculator(),
            summarizer=SessionSignalSummarizer(),
            lr_estimator=LearningRateEstimator(),
            state_builder=PatientStateBuilder(),
            drift_detector=DriftDetector(),
        )

    @classmethod
    def from_data_dir(cls, data_dir: str) -> "MetricsBuilder":
        adapter = get_adapter(data_dir)
        return cls(
            adapter=adapter,
            repo=PatientHistoryRepository(adapter),
            adherence_calc=AdherenceCalculator(),
            summarizer=SessionSignalSummarizer(),
            lr_estimator=LearningRateEstimator(),
            state_builder=PatientStateBuilder(),
            drift_detector=DriftDetector(),
        )

    def build(
        self,
        patient_id: int,
        checkpoint: datetime,
        window_days: int = 28,
        checkpoint_week: int | None = None,
    ) -> CheckpointSnapshot | None:
        """
        Build the metrics dict for one (patient, checkpoint).
        Returns None when the patient has no sessions AND no prescriptions in the window.
        """
        window_start = checkpoint - timedelta(days=window_days)
        bundle = self.repo.load(patient_id, window_start, checkpoint)
        # Exclude diagnostic protocols (circleAR): they are not therapeutic exercises,
        # so triage cards and check-ins must not reason over them.
        bundle.sessions = [s for s in bundle.sessions if s.protocol_id not in DIAGNOSTIC_PROTOCOL_IDS]
        bundle.prescriptions = [p for p in bundle.prescriptions if p.protocol_id not in DIAGNOSTIC_PROTOCOL_IDS]
        if not bundle.sessions and not bundle.prescriptions:
            return None

        adherence = self.adherence_calc.compute(bundle)
        summaries = self.summarizer.summarize(bundle)
        learning_rates = self.lr_estimator.compute(bundle, summaries)
        state = self.state_builder.build(
            adherence, learning_rates, summaries, bundle.self_reports, bundle
        )
        drift_events = self.drift_detector.detect(
            state, adherence, learning_rates, summaries, bundle
        )

        fm = self.adapter.get_patient_fm_scores(patient_id)
        fm_bl = fm[0] if fm else None

        session_by_id = {s.session_id: s for s in bundle.sessions}
        protocol_wise: dict[int, dict] = {}
        for sess_id, summ in summaries.items():
            sess = session_by_id.get(sess_id)
            if not sess:
                continue
            proto_id = summ.protocol_id
            if proto_id not in protocol_wise:
                protocol_wise[proto_id] = {
                    "name": self.adapter.get_protocol_name(proto_id),
                    "performance": [],
                    "difficulty": [],
                    "adherence_pct": None,
                }
            dt = (
                sess.start_time.strftime("%Y-%m-%d")
                if hasattr(sess.start_time, "strftime")
                else str(sess.start_time)[:10]
            )
            dm = getattr(summ, "difficulty_mean", {}) or {}
            diff_val = sum(dm.values()) / len(dm) if dm else 0
            protocol_wise[proto_id]["performance"].append(
                {"date": dt, "value": getattr(summ, "performance_mean", 0)}
            )
            protocol_wise[proto_id]["difficulty"].append({"date": dt, "value": diff_val})

        start_d = window_start.date() if hasattr(window_start, "date") else window_start
        end_d = checkpoint.date() if hasattr(checkpoint, "date") else checkpoint
        occurrences = expand_prescriptions(bundle.prescriptions, start_d, end_d)
        matched = match_sessions_to_occurrences(bundle.sessions, occurrences)
        session_duration_by_id = {s.session_id: s.duration_sec / 60.0 for s in bundle.sessions}

        # Per-(protocol, day) planned and done minutes.
        planned_by_proto_day: dict[tuple[int, Any], float] = {}
        for (d, proto_id, duration_min) in occurrences:
            planned_by_proto_day[(proto_id, d)] = (
                planned_by_proto_day.get((proto_id, d), 0.0) + duration_min
            )
        done_by_proto_day: dict[tuple[int, Any], float] = {}
        for (d, proto_id), sess_ids in matched.items():
            if not sess_ids:
                continue
            done_by_proto_day[(proto_id, d)] = sum(
                session_duration_by_id.get(sid, 0.0) for sid in sess_ids
            )

        for proto_id in {occ[1] for occ in occurrences}:
            if proto_id not in protocol_wise:
                protocol_wise[proto_id] = {
                    "name": self.adapter.get_protocol_name(proto_id),
                    "performance": [],
                    "difficulty": [],
                    "adherence_pct": None,
                    "adherence_daily": [],
                }
            if "adherence_daily" not in protocol_wise[proto_id]:
                protocol_wise[proto_id]["adherence_daily"] = []

            # Daily series: only emit points on days the protocol was scheduled,
            # so the line reflects "did you do it on the days you were meant to"
            # rather than interleaving every calendar day with zeros.
            daily_points = []
            planned_total = 0.0
            done_total = 0.0
            proto_days = sorted(
                d for (p, d) in planned_by_proto_day.keys() if p == proto_id
            )
            for d in proto_days:
                planned_min = planned_by_proto_day.get((proto_id, d), 0.0)
                done_min = done_by_proto_day.get((proto_id, d), 0.0)
                planned_total += planned_min
                done_total += done_min
                value = min(done_min / planned_min, 1.0) if planned_min > 0 else None
                if value is not None:
                    daily_points.append({"date": str(d), "value": value})

            protocol_wise[proto_id]["adherence_daily"] = daily_points
            protocol_wise[proto_id]["adherence_pct"] = (
                done_total / planned_total if planned_total > 0 else None
            )

        metrics = {
            "patient_id": patient_id,
            "checkpoint_date": end_d.isoformat() if hasattr(end_d, "isoformat") else str(end_d),
            "checkpoint_week": checkpoint_week,
            "fm_bl": fm_bl,
            "protocol_wise": {str(k): v for k, v in protocol_wise.items()},
            "adherence": {
                "adherence_minutes": adherence.adherence_minutes,
                "done_total": adherence.done_minutes,
                "planned_total": adherence.planned_minutes,
                "days": [
                    {"date": str(k), "planned_min": v[0], "done_min": v[1]}
                    for k, v in adherence.per_day.items()
                ],
            },
            "sessions": [
                {
                    "session_id": s.session_id,
                    "protocol_id": s.protocol_id,
                    "start_time": s.start_time.isoformat(),
                    "duration_sec": s.duration_sec,
                }
                for s in bundle.sessions
            ],
            "performance": [
                {"session_id": x.session_id, "performance_mean": getattr(x, "performance_mean", 0)}
                for x in summaries.values()
            ],
            "difficulty": [
                {
                    "session_id": x.session_id,
                    "difficulty_mean": (
                        (sum(dm.values()) / len(dm))
                        if (dm := getattr(x, "difficulty_mean", {}))
                        else 0
                    ),
                }
                for x in summaries.values()
            ],
            "learning_rates": [
                {"protocol_id": lr.protocol_id, "learning_rate": lr.learning_rate}
                for lr in learning_rates.values()
            ],
            "self_reports": [
                {
                    "key": r.key,
                    "value": r.value,
                    "timestamp": (
                        r.timestamp.isoformat()
                        if hasattr(r.timestamp, "isoformat")
                        else str(r.timestamp)
                    ),
                }
                for r in bundle.self_reports
            ],
            "drift_events": [{"type": e.type, "severity": e.severity} for e in drift_events],
        }

        return CheckpointSnapshot(
            metrics=metrics,
            drift_events=drift_events,
            adherence=adherence,
            bundle=bundle,
        )
