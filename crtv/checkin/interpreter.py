"""CheckInInterpreter - MedGemma schema-locked interpretation of check-in text."""

import json
import re
from typing import Any

from crtv.domain.models import CheckInResult, BarrierCode


def _safe_default() -> CheckInResult:
    """Safe fallback when parsing fails."""
    return CheckInResult(
        barriers=[],
        entities={},
        safety_flags={},
        supporting_snippets=[],
    )


class CheckInInterpreter:
    """
    Interpret check-in text via MedGemma.
    Schema-locked JSON output. On failure -> safe default.
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

    def interpret(
        self,
        text_or_transcript: str,
        context: dict[str, Any] | None = None,
    ) -> CheckInResult:
        """
        Parse text -> JSON: barriers, entities, safety_flags, supporting_snippets.
        Validate; on failure return safe default.
        """
        if not text_or_transcript or not text_or_transcript.strip():
            return _safe_default()
        if self.use_medgemma:
            try:
                self._load_model()
                if self._model is not None:
                    return self._call_medgemma(text_or_transcript, context or {})
            except Exception:
                pass
        return self._rule_based_fallback(text_or_transcript)

    def _call_medgemma(self, text: str, context: dict) -> CheckInResult:
        """Call MedGemma with schema-locked prompt."""
        prompt = f"""Classify this patient/caregiver check-in response. Output JSON only.
Text: {text[:500]}
Output format: {{"barriers":[{{"code":"...","severity":0-3,"confidence":0-1}}],"entities":{{}},"safety_flags":{{}},"supporting_snippets":[]}}"""
        try:
            inputs = self._processor(prompt, return_tensors="pt")
            outputs = self._model.generate(**inputs, max_new_tokens=256)
            out_text = self._processor.decode(outputs[0], skip_special_tokens=True)
            return self._parse_json_response(out_text)
        except Exception:
            return _safe_default()

    def _parse_json_response(self, out_text: str) -> CheckInResult:
        """Extract and validate JSON from model output."""
        match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", out_text, re.DOTALL)
        if not match:
            return _safe_default()
        try:
            data = json.loads(match.group())
            barriers = []
            for b in data.get("barriers", []):
                barriers.append(BarrierCode(
                    code=str(b.get("code", "unknown")),
                    severity=int(b.get("severity", 0)),
                    confidence=float(b.get("confidence", 0)),
                ))
            return CheckInResult(
                barriers=barriers,
                entities=dict(data.get("entities", {})),
                safety_flags=dict(data.get("safety_flags", {})),
                supporting_snippets=list(data.get("supporting_snippets", [])),
            )
        except (json.JSONDecodeError, (KeyError, ValueError, TypeError)):
            return _safe_default()

    def _rule_based_fallback(self, text: str) -> CheckInResult:
        """Simple keyword fallback when MedGemma unavailable."""
        text_lower = text.lower()
        barriers = []
        safety_flags = {}
        if "pain" in text_lower or "hurts" in text_lower:
            barriers.append(BarrierCode(code="pain", severity=1, confidence=0.5))
        if "tired" in text_lower or "fatigue" in text_lower:
            barriers.append(BarrierCode(code="fatigue", severity=1, confidence=0.5))
        if "fall" in text_lower or "fell" in text_lower:
            safety_flags["fall_risk_language"] = True
        return CheckInResult(
            barriers=barriers,
            entities={},
            safety_flags=safety_flags,
            supporting_snippets=[text[:100]] if text else [],
        )
