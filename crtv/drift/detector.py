"""DriftDetector and PatientStateBuilder."""

from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Any

from crtv.domain.models import (
    PatientState,
    DriftEvent,
    PatientHistoryBundle,
)
from crtv.features.adherence import AdherenceReport
from crtv.features.session_summaries import SessionSignalSummary
from crtv.features.learning_rate import LearningRateReport


@dataclass
class DriftConfig:
    """Configurable thresholds for drift detection."""

    adherence_threshold: float = 0.6
    plateau_learning_rate_threshold: float = 0.02
    regression_baseline_drop: float = 0.15
    min_sessions_for_trend: int = 3


class PatientStateBuilder:
    """Build PatientState from adherence, learning rate, summaries, self_reports."""

    def build(
        self,
        adherence: AdherenceReport | None,
        learning_rates: dict[tuple[int, int], LearningRateReport],
        summaries: dict[int, SessionSignalSummary],
        self_reports: list,
        bundle: PatientHistoryBundle,
    ) -> PatientState:
        engagement = "stable"
        challenge = "appropriate"
        trajectory = "improving"
        evidence: list[str] = []
        conf = 0.5

        if adherence and adherence.adherence_minutes is not None:
            if adherence.adherence_minutes < 0.4:
                engagement = "dropout-risk"
                conf = 0.8
                evidence.append(f"Adherence {adherence.adherence_minutes:.0%}")
            elif adherence.adherence_minutes < 0.6:
                engagement = "declining"
                conf = 0.6
                evidence.append(f"Adherence {adherence.adherence_minutes:.0%}")

        lr_vals = [r.learning_rate for r in learning_rates.values() if r.window_length >= 2]
        if lr_vals:
            avg_lr = sum(lr_vals) / len(lr_vals)
            if abs(avg_lr) < 0.02:
                trajectory = "plateau"
                evidence.append("Learning rate near zero")
            elif avg_lr < -0.02:
                trajectory = "regressing"
                evidence.append("Negative learning trend")

        return PatientState(
            engagement_state=engagement,
            challenge_state=challenge,
            trajectory_state=trajectory,
            barrier_priors={},
            confidence=conf,
            evidence_pointers=evidence,
        )


class DriftDetector:
    """Detect drift events from PatientState and config."""

    def __init__(self, config: DriftConfig | None = None):
        self.config = config or DriftConfig()

    def detect(
        self,
        state: PatientState,
        adherence: AdherenceReport | None,
        learning_rates: dict[tuple[int, int], LearningRateReport],
        summaries: dict[int, SessionSignalSummary],
        bundle: PatientHistoryBundle,
    ) -> list[DriftEvent]:
        events: list[DriftEvent] = []
        end = bundle.end
        start = bundle.start
        if isinstance(end, datetime) and isinstance(start, datetime):
            window_end = end
            window_start = start
        else:
            window_end = datetime.now()
            window_start = window_end - timedelta(days=28)

        if adherence and adherence.adherence_minutes is not None:
            if adherence.adherence_minutes < self.config.adherence_threshold:
                events.append(DriftEvent(
                    type="ADHERENCE_DRIFT",
                    severity=2 if adherence.adherence_minutes < 0.4 else 1,
                    confidence=0.8,
                    window_start=window_start,
                    window_end=window_end,
                    evidence={"adherence_minutes": adherence.adherence_minutes},
                    session_ids=[s.session_id for s in bundle.sessions],
                ))

        for (pid, proto_id), lr in learning_rates.items():
            if lr.window_length >= self.config.min_sessions_for_trend:
                if abs(lr.learning_rate) < self.config.plateau_learning_rate_threshold:
                    events.append(DriftEvent(
                        type="PLATEAU",
                        severity=1,
                        confidence=lr.confidence,
                        window_start=window_start,
                        window_end=window_end,
                        evidence={"learning_rate": lr.learning_rate, "protocol_id": proto_id},
                        session_ids=lr.supporting_sessions,
                    ))

        for sid, summ in summaries.items():
            k_slope = getattr(summ, "kinematics_slope", None) or {}
            if k_slope.get("mqi", 0) < -0.05:
                events.append(DriftEvent(
                    type="REGRESSION",
                    severity=1,
                    confidence=0.6,
                    window_start=window_start,
                    window_end=window_end,
                    evidence={"kinematics_mqi_slope": k_slope.get("mqi"), "session_id": sid},
                    session_ids=[sid],
                ))
            if k_slope.get("workspace_volume", 0) < -0.05:
                events.append(DriftEvent(
                    type="REGRESSION",
                    severity=1,
                    confidence=0.5,
                    window_start=window_start,
                    window_end=window_end,
                    evidence={"kinematics_workspace_slope": k_slope.get("workspace_volume"), "session_id": sid},
                    session_ids=[sid],
                ))

        return events
