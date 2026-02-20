"""TriagePipeline - batch run per date."""

from datetime import datetime, timedelta

from crtv.adapters.database import DatabaseAdapter
from crtv.repositories.patient_history import PatientHistoryRepository
from crtv.features.adherence import AdherenceCalculator
from crtv.features.session_summaries import SessionSignalSummarizer
from crtv.features.learning_rate import LearningRateEstimator
from crtv.drift.detector import DriftDetector, PatientStateBuilder
from crtv.checkin.policy import CheckInPolicy
from crtv.checkin.interpreter import CheckInInterpreter
from crtv.recommendations.engine import RecommendationEngine
from crtv.cards.renderer import TriageCardRenderer
from crtv.features.ppf import PPFComputer


class TriagePipeline:
    """Batch pipeline: load bundle, compute features, detect drift, recommend, render card."""

    def __init__(self, adapter: DatabaseAdapter):
        self.adapter = adapter
        self.repo = PatientHistoryRepository(adapter)
        self.adherence_calc = AdherenceCalculator()
        self.summarizer = SessionSignalSummarizer()
        self.lr_estimator = LearningRateEstimator()
        self.state_builder = PatientStateBuilder()
        self.drift_detector = DriftDetector()
        self.checkin_policy = CheckInPolicy()
        self.interpreter = CheckInInterpreter(use_medgemma=False)  # Avoid load in tests
        self.rec_engine = RecommendationEngine()
        self.card_renderer = TriageCardRenderer()
        self.ppf_computer = PPFComputer()

    def run(self, run_date: datetime | None = None) -> list[dict]:
        """Run pipeline for all patients with data in last 28 days."""
        run_date = run_date or datetime.now()
        start = run_date - timedelta(days=28)
        end = run_date
        patient_ids = self._get_patient_ids(start, end)
        results: list[dict] = []
        for pid in patient_ids:
            try:
                out = self._process_patient(pid, start, end)
                if out:
                    results.append(out)
            except Exception:
                continue
        return results

    def _get_patient_ids(self, start: datetime, end: datetime) -> list[int]:
        """Get patient IDs with prescriptions in window."""
        return self.adapter.get_patient_ids_in_window(start, end)

    def _process_patient(self, patient_id: int, start: datetime, end: datetime) -> dict | None:
        """Process single patient: bundle -> features -> drift -> recommendations -> card."""
        bundle = self.repo.load(patient_id, start, end)
        if not bundle.sessions and not bundle.prescriptions:
            return None
        adherence = self.adherence_calc.compute(bundle)
        summaries = self.summarizer.summarize(bundle)
        learning_rates = self.lr_estimator.compute(bundle, summaries)
        state = self.state_builder.build(
            adherence,
            learning_rates,
            summaries,
            bundle.self_reports,
            bundle,
        )
        drift_events = self.drift_detector.detect(
            state,
            adherence,
            learning_rates,
            summaries,
            bundle,
        )
        checkin_req = self.checkin_policy.decide(drift_events, [])
        checkin_result = None
        if checkin_req:
            checkin_req.patient_id = patient_id
            checkin_result = self.interpreter.interpret("", {})
        ppf_report = self.ppf_computer.compute(bundle.assessments, bundle.protocol_catalog)
        ppf_map = ppf_report.ppf
        rec_bundle = self.rec_engine.recommend(state, drift_events, checkin_result, ppf_map)
        evidence_str = f"Adherence {adherence.adherence_minutes:.0%}" if adherence.adherence_minutes else ""
        card = self.card_renderer.render(
            rec_bundle,
            patient_id,
            drift_events,
            evidence_str,
        )
        run_id = self.adapter.write_pipeline_run({"run_date": str(end), "patient_id": patient_id})
        return {
            "patient_id": patient_id,
            "card": card,
            "drift_events": drift_events,
            "disposition": rec_bundle.disposition,
            "pipeline_run_id": run_id,
        }
