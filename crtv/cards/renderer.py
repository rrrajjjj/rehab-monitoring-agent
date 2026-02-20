"""Templated triage card renderer (no MedGemma)."""

from crtv.domain.models import TriageCard, RecommendationBundle, ActionItem


class TriageCardRenderer:
    """Render TriageCard from RecommendationBundle."""

    def render(
        self,
        bundle: RecommendationBundle,
        patient_id: int,
        drift_events: list,
        evidence_summary: str = "",
    ) -> TriageCard:
        """Build TriageCard from recommendation bundle and evidence."""
        headline = "No action needed"
        if bundle.disposition == "TRIAGE":
            headline = "Triage recommended"
        elif bundle.disposition == "ESCALATE":
            headline = "Needs clinician review"

        reasons = list(bundle.rationale)
        if not reasons and drift_events:
            reasons = [f"Detected: {e.type}" for e in drift_events]

        return TriageCard(
            headline=headline,
            reasons=reasons,
            patient_voice_excerpt=getattr(bundle, "patient_voice_excerpt", "") or "",
            recommended_actions=list(bundle.recommended_actions),
            evidence={
                "patient_id": patient_id,
                "evidence_summary": evidence_summary or "See rationale",
                "drift_types": [e.type for e in drift_events] if drift_events else [],
            },
            audit=bundle.audit,
        )
