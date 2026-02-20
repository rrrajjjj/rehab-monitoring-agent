"""
ClinicianViewService - aggregates pipeline output with full evidence for frontend.
"""

from datetime import datetime, timedelta, date
from dataclasses import dataclass, asdict
from typing import Any

from crtv.adapters.mock_adapter import MockAdapter
from crtv.adapters.database import DatabaseAdapter
from crtv.pipeline.runner import TriagePipeline
from crtv.repositories.patient_history import PatientHistoryRepository
from crtv.features.adherence import AdherenceCalculator
from crtv.features.session_summaries import SessionSignalSummarizer
from crtv.features.learning_rate import LearningRateEstimator


@dataclass
class PatientListItem:
    patient_id: int
    disposition: str
    attention_level: int  # 0=none, 1=low, 2=medium, 3=high
    headline: str
    drift_types: list[str]


@dataclass
class EvidencePointer:
    reason: str
    type: str  # adherence | drift | performance | difficulty | learning_rate
    ref: str
    data_ref: str  # key for drill-down (e.g. "adherence", "performance", "drift_ADHERENCE_DRIFT")


class ClinicianViewService:
    """Build clinician-facing data with evidence pointers."""

    def __init__(self, adapter: DatabaseAdapter):
        self.adapter = adapter
        self.pipeline = TriagePipeline(adapter)
        self.repo = PatientHistoryRepository(adapter)

    def _attention_level(self, disposition: str, max_severity: int) -> int:
        if disposition == "ESCALATE":
            return 3
        if disposition == "SUGGEST":
            return 2 if max_severity >= 2 else 1
        return 0

    def list_patients(self, run_date: datetime | None = None) -> list[dict]:
        """Patients with attention level for list view."""
        run_date = run_date or datetime(2024, 1, 25)
        results = self.pipeline.run(run_date)
        items = []
        for r in results:
            max_sev = max((e.severity for e in r["drift_events"]), default=0)
            items.append({
                "patient_id": r["patient_id"],
                "disposition": r["disposition"],
                "attention_level": self._attention_level(r["disposition"], max_sev),
                "headline": r["card"].headline,
                "drift_types": [e.type for e in r["drift_events"]],
            })
        return items

    def get_patient_detail(self, patient_id: int, run_date: datetime | None = None) -> dict | None:
        """Full triage card + evidence for drill-down."""
        run_date = run_date or datetime(2024, 1, 25)
        start = run_date - timedelta(days=28)
        end = run_date

        bundle = self.repo.load(patient_id, start, end)
        if not bundle.sessions and not bundle.prescriptions:
            return None

        adherence_calc = AdherenceCalculator()
        summarizer = SessionSignalSummarizer()
        lr_estimator = LearningRateEstimator()

        adherence = adherence_calc.compute(bundle)
        summaries = summarizer.summarize(bundle)
        learning_rates = lr_estimator.compute(bundle, summaries)

        from crtv.drift.detector import DriftDetector, PatientStateBuilder
        from crtv.recommendations.engine import RecommendationEngine
        from crtv.cards.renderer import TriageCardRenderer
        from crtv.features.ppf import PPFComputer

        state_builder = PatientStateBuilder()
        drift_detector = DriftDetector()
        rec_engine = RecommendationEngine()
        card_renderer = TriageCardRenderer()
        ppf_computer = PPFComputer()

        state = state_builder.build(adherence, learning_rates, summaries, bundle.self_reports, bundle)
        drift_events = drift_detector.detect(state, adherence, learning_rates, summaries, bundle)
        ppf_report = ppf_computer.compute(bundle.assessments, bundle.protocol_catalog)
        rec_bundle = rec_engine.recommend(state, drift_events, None, ppf_report.ppf)
        evidence_str = f"Adherence {adherence.adherence_minutes:.0%}" if adherence.adherence_minutes else ""
        card = card_renderer.render(rec_bundle, patient_id, drift_events, evidence_str)

        evidence_pointers = self._build_evidence_pointers(
            card, drift_events, adherence, rec_bundle
        )

        adherence_calendar = self._adherence_to_calendar(adherence)
        metrics_series = self._summaries_to_metrics_series(bundle, summaries, learning_rates)

        return {
            "patient_id": patient_id,
            "card": {
                "headline": card.headline,
                "reasons": card.reasons,
                "patient_voice_excerpt": card.patient_voice_excerpt,
                "recommended_actions": [
                    {"action_type": a.action_type, "params": a.params}
                    for a in card.recommended_actions
                ],
                "evidence": card.evidence,
                "audit": card.audit,
            },
            "drift_events": [
                {
                    "type": e.type,
                    "severity": e.severity,
                    "confidence": e.confidence,
                    "evidence": e.evidence,
                    "session_ids": e.session_ids,
                }
                for e in drift_events
            ],
            "disposition": rec_bundle.disposition,
            "evidence_pointers": evidence_pointers,
            "adherence": adherence_calendar,
            "metrics": metrics_series,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "protocol_id": s.protocol_id,
                    "start_time": s.start_time.isoformat(),
                    "duration_sec": s.duration_sec,
                    "status": s.status,
                }
                for s in bundle.sessions
            ],
        }

    def _build_evidence_pointers(
        self,
        card,
        drift_events,
        adherence,
        rec_bundle,
    ) -> list[dict]:
        """Link each reason/bullet to evidence data."""
        pointers = []
        for r in card.reasons:
            if "Adherence" in r or "adherence" in r.lower():
                pointers.append({
                    "reason": r,
                    "type": "adherence",
                    "ref": "adherence",
                    "data_ref": "adherence",
                    "value": f"{adherence.adherence_minutes:.0%}" if adherence.adherence_minutes else "N/A",
                })
            elif any(e.type in r for e in drift_events):
                ev = next((e for e in drift_events if e.type in r), None)
                if ev:
                    pointers.append({
                        "reason": r,
                        "type": "drift",
                        "ref": ev.type,
                        "data_ref": f"drift_{ev.type}",
                        "value": ev.evidence,
                        "session_ids": ev.session_ids,
                    })
            else:
                pointers.append({"reason": r, "type": "general", "ref": "rationale", "data_ref": "rationale"})
        return pointers

    def _adherence_to_calendar(self, adherence) -> dict:
        """Format adherence for calendar view."""
        days = []
        for d, (planned, done) in sorted(adherence.per_day.items()):
            days.append({
                "date": d.isoformat(),
                "planned_min": planned,
                "done_min": done,
                "ratio": done / planned if planned > 0 else 0,
            })
        return {
            "window_start": adherence.window_start.isoformat(),
            "window_end": adherence.window_end.isoformat(),
            "adherence_minutes": adherence.adherence_minutes,
            "planned_total": adherence.planned_minutes,
            "done_total": adherence.done_minutes,
            "days": days,
            "evidence_map": {k: v for k, v in adherence.evidence_map.items()},
        }

    def _summaries_to_metrics_series(self, bundle, summaries, learning_rates) -> dict:
        """Format session summaries for charts."""
        sessions = sorted(bundle.sessions, key=lambda s: s.start_time)
        performance_by_session = []
        difficulty_by_session = []
        for s in sessions:
            summ = summaries.get(s.session_id)
            if summ:
                perf = getattr(summ, "performance_mean", 0) or 0
                perf_slope = getattr(summ, "performance_slope", 0) or 0
                performance_by_session.append({
                    "session_id": s.session_id,
                    "date": s.start_time.date().isoformat(),
                    "performance_mean": perf,
                    "performance_slope": perf_slope,
                    "duration_sec": s.duration_sec,
                })
                dm = getattr(summ, "difficulty_mean", {}) or {}
                diff_val = sum(dm.values()) / len(dm) if dm else 0
                difficulty_by_session.append({
                    "session_id": s.session_id,
                    "date": s.start_time.date().isoformat(),
                    "difficulty_mean": diff_val,
                    "modulators": dm,
                })
        lr_list = []
        for (pid, proto_id), lr in learning_rates.items():
            lr_list.append({
                "protocol_id": proto_id,
                "learning_rate": lr.learning_rate,
                "confidence": lr.confidence,
                "window_length": lr.window_length,
                "supporting_sessions": lr.supporting_sessions,
            })
        return {
            "performance": performance_by_session,
            "difficulty": difficulty_by_session,
            "learning_rates": lr_list,
        }
