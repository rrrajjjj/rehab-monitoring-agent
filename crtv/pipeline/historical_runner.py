"""
HistoricalTriageRunner - run triage at checkpoints for regressor patients.
Deduplication: no repeated diagnosis within 7 days; OK after 7+ days.
Set CRTV_MAX_TRIAGE_CHECKPOINTS=3 to limit LLM calls for testing.
"""

import os
from datetime import datetime, timedelta, date
from collections import defaultdict

from crtv.adapters.csv_adapter import CSVDataAdapter
from crtv.repositories.patient_history import PatientHistoryRepository
from crtv.features.adherence import AdherenceCalculator
from crtv.features.session_summaries import SessionSignalSummarizer
from crtv.features.learning_rate import LearningRateEstimator
from crtv.drift.detector import DriftDetector, PatientStateBuilder
from crtv.features.ppf import PPFComputer
from crtv.reasoning.medgemma_triage import MedGemmaTriageEngine
from crtv.domain.models import TriageCard, ActionItem
from crtv.cards.renderer import TriageCardRenderer


def _primary_diagnosis(drift_events: list) -> str:
    """Single diagnosis type for deduplication."""
    if not drift_events:
        return "STABLE"
    types = [e.type for e in drift_events]
    if "ADHERENCE_DRIFT" in types:
        return "ADHERENCE_DRIFT"
    if "REGRESSION" in types:
        return "REGRESSION"
    if "PLATEAU" in types:
        return "PLATEAU"
    if "OVERCHALLENGE" in types:
        return "OVERCHALLENGE"
    if "UNDERCHALLENGE" in types:
        return "UNDERCHALLENGE"
    return types[0] if types else "STABLE"


class HistoricalTriageRunner:
    """
    Run triage at weekly checkpoints for patients with FM_EoT < FM_BL.
    Deduplication: same diagnosis not repeated within 7 days.
    """

    def __init__(self, data_dir: str, use_medgemma: bool = False):
        self.adapter = CSVDataAdapter(data_dir)
        self.repo = PatientHistoryRepository(self.adapter)
        self.adherence_calc = AdherenceCalculator()
        self.summarizer = SessionSignalSummarizer()
        self.lr_estimator = LearningRateEstimator()
        self.state_builder = PatientStateBuilder()
        self.drift_detector = DriftDetector()
        self.ppf_computer = PPFComputer()
        self.medgemma = MedGemmaTriageEngine(use_medgemma=use_medgemma)
        self.card_renderer = TriageCardRenderer()

        self._last_diagnosis: dict[tuple[int, str], date] = {}  # (patient_id, diagnosis_type) -> date

    def get_regressor_patients(self) -> list[int]:
        """Patients whose FM score dropped (EoT < BL)."""
        return self.adapter.get_clinical_scores_regressors()

    def get_regressor_with_largest_delta(self) -> int | None:
        """Patient with largest FM decline (for trial: single exemplar)."""
        return self.adapter.get_regressor_with_largest_delta()

    def get_patient_date_range(self, patient_id: int) -> tuple[date | None, date | None]:
        """Earliest and latest session date for patient."""
        sessions = self.adapter.get_sessions(patient_id, datetime(2020, 1, 1), datetime(2030, 1, 1))
        if not sessions:
            return None, None
        dates = [s.start_time.date() for s in sessions]
        return min(dates), max(dates)

    def run_weekly_checkpoints(
        self,
        patient_id: int,
        start: date,
        end: date,
        dedupe_days: int = 7,
    ) -> list[dict]:
        """
        Run triage at each week; deduplicate: same diagnosis not within dedupe_days.
        Returns list of triage cards with checkpoint_date.
        """
        max_checkpoints = int(os.environ.get("CRTV_MAX_TRIAGE_CHECKPOINTS", "0") or 0)
        cards = []
        d = start
        checkpoints_run = 0
        while d <= end:
            if max_checkpoints > 0 and checkpoints_run >= max_checkpoints:
                break
            checkpoint = datetime.combine(d, datetime.min.time())
            window_start = checkpoint - timedelta(days=28)
            bundle = self.repo.load(patient_id, window_start, checkpoint)
            if not bundle.sessions and not bundle.prescriptions:
                d += timedelta(days=7)
                continue

            adherence = self.adherence_calc.compute(bundle)
            summaries = self.summarizer.summarize(bundle)
            learning_rates = self.lr_estimator.compute(bundle, summaries)
            state = self.state_builder.build(
                adherence, learning_rates, summaries, bundle.self_reports, bundle
            )
            drift_events = self.drift_detector.detect(
                state, adherence, learning_rates, summaries, bundle
            )
            diagnosis = _primary_diagnosis(drift_events)

            last = self._last_diagnosis.get((patient_id, diagnosis))
            if last is not None and (d - last).days < dedupe_days:
                d += timedelta(days=7)
                continue

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
                dt = sess.start_time.strftime("%Y-%m-%d") if hasattr(sess.start_time, "strftime") else str(sess.start_time)[:10]
                dm = getattr(summ, "difficulty_mean", {}) or {}
                diff_val = sum(dm.values()) / len(dm) if dm else 0
                protocol_wise[proto_id]["performance"].append({"date": dt, "value": getattr(summ, "performance_mean", 0)})
                protocol_wise[proto_id]["difficulty"].append({"date": dt, "value": diff_val})

            from crtv.features.adherence import expand_prescriptions, match_sessions_to_occurrences
            start_d = window_start.date() if hasattr(window_start, "date") else window_start
            end_d = checkpoint.date() if hasattr(checkpoint, "date") else checkpoint
            occurrences = expand_prescriptions(bundle.prescriptions, start_d, end_d)
            matched = match_sessions_to_occurrences(bundle.sessions, occurrences)
            for proto_id in set(occ[1] for occ in occurrences):
                if proto_id not in protocol_wise:
                    protocol_wise[proto_id] = {"name": self.adapter.get_protocol_name(proto_id), "performance": [], "difficulty": [], "adherence_pct": None}
                planned = sum(occ[2] for occ in occurrences if occ[1] == proto_id)
                done = 0.0
                for (d, pid), sess_ids in matched.items():
                    if pid != proto_id:
                        continue
                    for s in bundle.sessions:
                        if s.session_id in sess_ids:
                            done += s.duration_sec / 60.0
                protocol_wise[proto_id]["adherence_pct"] = done / planned if planned > 0 else None

            metrics = {
                "patient_id": patient_id,
                "checkpoint_date": d.isoformat(),
                "fm_bl": fm_bl,
                "protocol_wise": {str(k): v for k, v in protocol_wise.items()},
                "adherence": {
                    "adherence_minutes": adherence.adherence_minutes,
                    "done_total": adherence.done_minutes,
                    "planned_total": adherence.planned_minutes,
                    "days": [{"date": str(k), "planned_min": v[0], "done_min": v[1]} for k, v in adherence.per_day.items()],
                },
                "sessions": [{"session_id": s.session_id, "protocol_id": s.protocol_id, "start_time": s.start_time.isoformat(), "duration_sec": s.duration_sec} for s in bundle.sessions],
                "performance": [{"session_id": x.session_id, "performance_mean": getattr(x, "performance_mean", 0)} for x in summaries.values()],
                "difficulty": [{"session_id": x.session_id, "difficulty_mean": (sum(dm.values()) / len(dm)) if (dm := getattr(x, "difficulty_mean", {})) else 0} for x in summaries.values()],
                "learning_rates": [{"protocol_id": lr.protocol_id, "learning_rate": lr.learning_rate} for lr in learning_rates.values()],
                "self_reports": [
                    {"key": r.key, "value": r.value, "timestamp": r.timestamp.isoformat() if hasattr(r.timestamp, "isoformat") else str(r.timestamp)}
                    for r in bundle.self_reports
                ],
                "drift_events": [{"type": e.type, "severity": e.severity} for e in drift_events],
            }

            checkpoints_run += 1
            conclusion = self.medgemma.conclude(metrics)
            if conclusion.disposition == "NO_ACTION" and not drift_events:
                d += timedelta(days=7)
                continue

            self._last_diagnosis[(patient_id, diagnosis)] = d
            from crtv.domain.models import RecommendationBundle
            rec_bundle = RecommendationBundle(
                disposition=conclusion.disposition,
                rationale=conclusion.reasons,
                expected_effect=[],
                recommended_actions=[ActionItem(action_type=a.get("action_type", "message"), params=a.get("params", {})) for a in conclusion.recommended_actions],
                audit={"medgemma": True, "confidence": conclusion.confidence},
            )
            evidence_str = f"Adherence {adherence.adherence_minutes:.0%}" if adherence.adherence_minutes else ""
            card = self.card_renderer.render(rec_bundle, patient_id, drift_events, evidence_str)
            observation_items = [{"text": o.text, "attention": o.attention, "refs": o.refs} for o in conclusion.observations]
            card = card.model_copy(update={
                "headline": conclusion.headline,
                "reasons": conclusion.reasons,
                "evidence": {"items": observation_items},  # keep key for backward compat; stores observations
            })
            cards.append({
                "patient_id": patient_id,
                "checkpoint_date": d.isoformat(),
                "card": card,
                "drift_events": drift_events,
                "disposition": conclusion.disposition,
                "severity": conclusion.severity,
                "diagnosis": diagnosis,
                "adherence": adherence,
                "metrics": metrics,
            })
            d += timedelta(days=7)
        return cards
