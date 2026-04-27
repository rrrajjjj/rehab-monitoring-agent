"""
HistoricalTriageRunner - run triage at checkpoints for regressor patients.
Deduplication: no repeated diagnosis within 7 days; OK after 7+ days.
Set CRTV_MAX_TRIAGE_CHECKPOINTS=3 to limit LLM calls for testing.
"""

import os
from datetime import datetime, timedelta, date
from collections import defaultdict

from crtv.features.ppf import PPFComputer
from crtv.pipeline.metrics_builder import MetricsBuilder
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
        self.builder = MetricsBuilder.from_data_dir(data_dir)
        self.adapter = self.builder.adapter
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

    def _conclude_snapshot(self, patient_id: int, checkpoint_d: date, snapshot) -> dict:
        """
        Run the LLM on a pre-built MetricsBuilder snapshot and assemble the
        full in-memory card entry (the dict shape that HistoricalTriageService
        caches and CardStore persists). No dedup, no early-return.
        """
        from crtv.domain.models import RecommendationBundle

        metrics = snapshot.metrics
        drift_events = snapshot.drift_events
        adherence = snapshot.adherence
        diagnosis = _primary_diagnosis(drift_events)

        conclusion = self.medgemma.conclude(metrics)
        rec_bundle = RecommendationBundle(
            disposition=conclusion.disposition,
            rationale=conclusion.reasons,
            expected_effect=[],
            recommended_actions=[
                ActionItem(action_type=a.get("action_type", "message"), params=a.get("params", {}))
                for a in conclusion.recommended_actions
            ],
            audit={"medgemma": True, "confidence": conclusion.confidence},
        )
        evidence_str = f"Adherence {adherence.adherence_minutes:.0%}" if adherence.adherence_minutes else ""
        card = self.card_renderer.render(rec_bundle, patient_id, drift_events, evidence_str)
        observation_items = [
            {"text": o.text, "attention": o.attention, "refs": o.refs} for o in conclusion.observations
        ]
        card = card.model_copy(update={
            "headline": conclusion.headline,
            "reasons": conclusion.reasons,
            "evidence": {"items": observation_items},
        })
        return {
            "patient_id": patient_id,
            "checkpoint_date": checkpoint_d.isoformat(),
            "card": card,
            "drift_events": drift_events,
            "disposition": conclusion.disposition,
            "severity": conclusion.severity,
            "diagnosis": diagnosis,
            "adherence": adherence,
            "metrics": metrics,
        }

    def run_single_checkpoint(self, patient_id: int, checkpoint_d: date, checkpoint_week: int | str | None = None) -> dict | None:
        """
        Run triage for one (patient, checkpoint). No dedup, no skip. Returns the
        card entry dict or None if the patient has no data in the trailing window.
        Used by demo_mining/build_cards.py to batch-build a fixed shortlist.
        """
        checkpoint = datetime.combine(checkpoint_d, datetime.min.time())
        if checkpoint_week is None:
            checkpoint_week = checkpoint_d.isoformat()
        snapshot = self.builder.build(patient_id, checkpoint, checkpoint_week=checkpoint_week)
        if snapshot is None:
            return None
        return self._conclude_snapshot(patient_id, checkpoint_d, snapshot)

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
            checkpoint_week = (d - start).days // 7 + 1
            snapshot = self.builder.build(patient_id, checkpoint, checkpoint_week=checkpoint_week)
            if snapshot is None:
                d += timedelta(days=7)
                continue

            diagnosis = _primary_diagnosis(snapshot.drift_events)
            last = self._last_diagnosis.get((patient_id, diagnosis))
            if last is not None and (d - last).days < dedupe_days:
                d += timedelta(days=7)
                continue

            checkpoints_run += 1
            entry = self._conclude_snapshot(patient_id, d, snapshot)
            if entry["disposition"] == "NO_ACTION" and not snapshot.drift_events:
                d += timedelta(days=7)
                continue

            self._last_diagnosis[(patient_id, diagnosis)] = d
            cards.append(entry)
            d += timedelta(days=7)
        return cards
