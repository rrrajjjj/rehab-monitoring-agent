"""ClinicianSummaryGenerator - MedGemma schema-locked summarization."""

from typing import Any

from crtv.domain.models import TriageCardText, RecommendationBundle


class ClinicianSummaryGenerator:
    """
    Generate short structured text for triage card.
    MedGemma produces factual summary; no new medical claims.
    """

    def __init__(self, use_medgemma: bool = True):
        self.use_medgemma = use_medgemma
        self._model = None

    def _load_model(self):
        """Lazy load MedGemma."""
        if self._model is not None:
            return
        try:
            from transformers import AutoProcessor, AutoModelForCausalLM
            self._processor = AutoProcessor.from_pretrained("google/medgemma-4b-it")
            self._model = AutoModelForCausalLM.from_pretrained("google/medgemma-4b-it")
        except Exception:
            self.use_medgemma = False

    def generate(
        self,
        recommendation_bundle: RecommendationBundle,
        evidence_pointers: list[Any],
    ) -> TriageCardText:
        """Generate headline, reasons, patient_voice_excerpt, evidence_summary."""
        if self.use_medgemma:
            try:
                self._load_model()
                if self._model is not None:
                    return self._call_medgemma(recommendation_bundle, evidence_pointers)
            except Exception:
                pass
        return self._template_fallback(recommendation_bundle, evidence_pointers)

    def _call_medgemma(
        self,
        bundle: RecommendationBundle,
        evidence: list,
    ) -> TriageCardText:
        """Call MedGemma for structured summary."""
        prompt = f"""Summarize for clinician triage. Output JSON: {{"headline":"...","reasons":["..."],"patient_voice_excerpt":"","evidence_summary":"..."}}
Rationale: {bundle.rationale[:3] if bundle.rationale else []}
Evidence: {str(evidence)[:300]}"""
        try:
            inputs = self._processor(prompt, return_tensors="pt")
            outputs = self._model.generate(**inputs, max_new_tokens=256)
            out_text = self._processor.decode(outputs[0], skip_special_tokens=True)
            import json, re
            match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", out_text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return TriageCardText(
                    headline=data.get("headline", ""),
                    reasons=data.get("reasons", []),
                    patient_voice_excerpt=data.get("patient_voice_excerpt", ""),
                    evidence_summary=data.get("evidence_summary", ""),
                )
        except Exception:
            pass
        return self._template_fallback(bundle, evidence)

    def _template_fallback(
        self,
        bundle: RecommendationBundle,
        evidence: list,
    ) -> TriageCardText:
        """Template-based fallback."""
        headline = f"Disposition: {bundle.disposition}"
        reasons = list(bundle.rationale)[:4]
        return TriageCardText(
            headline=headline,
            reasons=reasons,
            patient_voice_excerpt="",
            evidence_summary="; ".join(str(e)[:80] for e in evidence[:3]) if evidence else "See rationale",
        )
