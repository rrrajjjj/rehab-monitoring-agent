"""RecommendationEngine - action library and rationale."""

from crtv.domain.models import (
    PatientState,
    DriftEvent,
    RecommendationBundle,
    ActionItem,
    CheckInResult,
)

ACTION_TYPES = [
    "pause_prescription",
    "adjust_prescribed_minutes",
    "swap_protocol",
    "assign_questionnaire",
    "message",
    "request_diagnostic",
    "escalate_to_clinician",
]


class RecommendationEngine:
    """
    Recommend actions from vetted library.
    Disposition: NO_ACTION | SUGGEST | ESCALATE.
    Conservative escalation on safety flags.
    """

    def recommend(
        self,
        state: PatientState,
        drift_events: list[DriftEvent],
        checkin_result: CheckInResult | None,
        ppf: dict[int, float] | None,
    ) -> RecommendationBundle:
        """Produce recommendations with rationale."""
        disposition = "NO_ACTION"
        rationale: list[str] = []
        actions: list[ActionItem] = []
        safety_flags = checkin_result.safety_flags if checkin_result else {}
        if safety_flags and any(safety_flags.values()):
            disposition = "ESCALATE"
            rationale.append("Safety flags present from check-in")
            actions.append(ActionItem(action_type="escalate_to_clinician", params={"reason": "safety_flags"}))

        if disposition != "ESCALATE":
            for e in drift_events:
                if e.type == "ADHERENCE_DRIFT" and e.severity >= 2:
                    disposition = "SUGGEST"
                    rationale.append(f"Adherence drift (severity {e.severity})")
                    actions.append(ActionItem(action_type="message", params={"template": "adherence_nudge"}))
                    break
                if e.type == "REGRESSION":
                    barriers = checkin_result.barriers if checkin_result else []
                    if any(b.severity >= 2 for b in barriers):
                        disposition = "ESCALATE"
                        rationale.append("Regression with high barrier severity")
                        actions.append(ActionItem(action_type="escalate_to_clinician", params={"reason": "regression"}))
                        break

        if not rationale and drift_events:
            rationale = [f"{e.type} (confidence {e.confidence:.0%})" for e in drift_events[:3]]

        return RecommendationBundle(
            disposition=disposition,
            rationale=rationale,
            expected_effect=[],
            recommended_actions=actions[:3],
            audit={"rules_version": "1.0"},
        )
