"""
Swappable LLM providers for triage reasoning.
Implement TriageLLM to plug in MedGemma (local), OpenAI, or any API-based model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("crtv.llm")

# Truncate for log lines (full content at DEBUG)
_TRUNCATE_LEN = 500


def _truncate(s: str, max_len: int = _TRUNCATE_LEN) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... [{len(s)} chars total]"


@runtime_checkable
class TriageLLM(Protocol):
    """Interface for LLM-backed triage reasoning. Swappable by design."""

    def generate(self, prompt: str) -> str:
        """Send prompt to LLM; return raw text response."""
        ...


class CachingProvider:
    """Wraps another provider; saves responses to disk. Same prompt -> load from cache, skip LLM call.
    Cache key = hash(full prompt). Any change to the prompt (including template) causes a cache miss."""

    def __init__(self, inner: TriageLLM, cache_dir: str | Path | None = None):
        self._inner = inner
        self._inner_name = type(inner).__name__
        self._dir = Path(cache_dir or os.environ.get("CRTV_LLM_CACHE_DIR", ".llm_cache"))
        self._dir.mkdir(parents=True, exist_ok=True)

    def generate(self, prompt: str) -> str:
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        path = self._dir / f"{h}.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                out = data.get("response", "")
                meta = data.get("_meta", {})
                if not out:
                    logger.warning("LLM cache: stale empty entry %s - deleting and retrying API call", h[:12])
                    sys.stderr.write(f"[crtv] Cache had empty response (hash {h[:12]}) - retrying API\n")
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    # fall through to call inner
                else:
                    logger.info("LLM cache hit %s response_len=%d", h[:12], len(out))
                    return out
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("Cache read failed %s: %s", h[:12], e)
        # Fallback for testing: use any cached response when exact key misses (e.g. after prompt change)
        if os.environ.get("CRTV_LLM_CACHE_FALLBACK", "").lower() in ("1", "true", "yes"):
            for p in sorted(self._dir.glob("*.json")):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                    out = data.get("response", "")
                    if out:
                        logger.info("LLM cache fallback used %s (exact key missed)", p.stem[:12])
                        return out
                except (json.JSONDecodeError, OSError):
                    pass
        raw = self._inner.generate(prompt)
        if not raw:
            logger.warning(
                "LLM returned empty (provider=%s) - NOT caching; next run will retry. Check logs above for error.",
                self._inner_name,
            )
            sys.stderr.write(f"[crtv] LLM returned empty ({self._inner_name}) - check logs for error\n")
            return raw
        try:
            from datetime import datetime
            payload = {
                "response": raw,
                "_meta": {
                    "provider": self._inner_name,
                    "prompt_hash": h[:12],
                    "response_len": len(raw),
                    "cached_at": datetime.utcnow().isoformat() + "Z",
                },
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info("LLM cached %s len=%d", h[:12], len(raw))
        except OSError as e:
            logger.debug("Cache write failed %s: %s", h[:12], e)
        return raw


class RuleBasedProvider:
    """No-LLM fallback: always returns empty, prompting engine to use rule-based logic."""

    def generate(self, prompt: str) -> str:
        logger.debug("Skipping LLM (rule-based provider); prompt len=%d", len(prompt))
        return ""


class MedGemmaProvider:
    """Local MedGemma via Hugging Face transformers."""

    def __init__(self, model_id: str = "google/medgemma-4b-it"):
        self.model_id = model_id
        self._model = None
        self._processor = None

    def _load(self):
        if self._model is not None:
            return True
        try:
            from transformers import AutoProcessor, AutoModelForCausalLM
            self._processor = AutoProcessor.from_pretrained(self.model_id)
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id)
            return True
        except Exception:
            return False

    def generate(self, prompt: str) -> str:
        if not self._load():
            logger.warning("MedGemmaProvider: model load failed")
            return ""
        logger.info("LLM call attempt provider=medgemma model=%s prompt_len=%d", self.model_id, len(prompt))
        logger.debug("LLM prompt: %s", _truncate(prompt))
        try:
            inputs = self._processor(prompt, return_tensors="pt")
            outputs = self._model.generate(**inputs, max_new_tokens=384)
            out = self._processor.decode(outputs[0], skip_special_tokens=True)
            logger.info("LLM call ok provider=medgemma response_len=%d", len(out))
            logger.debug("LLM response: %s", _truncate(out))
            return out
        except Exception as e:
            logger.warning("LLM call failed provider=medgemma error=%s", e, exc_info=True)
            return ""


class OpenAICompatibleProvider:
    """
    Any OpenAI-compatible API: OpenAI, Azure, local vLLM, Together, Groq, etc.
    Set CRTV_OPENAI_API_KEY and optionally CRTV_OPENAI_BASE_URL, CRTV_OPENAI_MODEL.
    For GPT-5.2 reasoning: CRTV_OPENAI_REASONING_EFFORT=medium (uses Responses API).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("CRTV_OPENAI_API_KEY", "")
        self.base_url = base_url or os.environ.get("CRTV_OPENAI_BASE_URL")
        self.model = model or os.environ.get("CRTV_OPENAI_MODEL", "gpt-5-mini")
        self.reasoning_effort = os.environ.get("CRTV_OPENAI_REASONING_EFFORT", "").lower()
        self._use_responses_api = bool(
            self.reasoning_effort in ("none", "low", "medium", "high", "xhigh")
        )
        _max = os.environ.get("CRTV_OPENAI_MAX_TOKENS", "")
        self.max_tokens = int(_max) if _max and str(_max).isdigit() else (
            8192 if ("gpt-5" in self.model or self.model.startswith("o")) else 512
        )

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            logger.warning("OpenAICompatibleProvider: no API key, skipping")
            return ""
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("OpenAICompatibleProvider: openai not installed (pip install -e \".[api]\" from project dir)")
            return ""
        logger.info("LLM call attempt provider=openai model=%s prompt_len=%d", self.model, len(prompt))
        logger.debug("LLM prompt: %s", _truncate(prompt))
        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            if self._use_responses_api:
                kwargs = {"model": self.model, "input": prompt, "max_output_tokens": self.max_tokens}
                if self.reasoning_effort:
                    kwargs["reasoning"] = {"effort": self.reasoning_effort}
                resp = client.responses.create(**kwargs)
                out = (getattr(resp, "output_text", None) or "").strip()
            else:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=self.max_tokens,
                )
                out = ""
                if resp.choices:
                    out = (resp.choices[0].message.content or "").strip()
            if out:
                logger.info("LLM call ok provider=openai response_len=%d", len(out))
                logger.debug("LLM response: %s", _truncate(out))
                return out
            logger.warning("LLM call: OpenAI returned empty")
            sys.stderr.write("[crtv] OpenAI returned empty response\n")
        except Exception as e:
            logger.warning("LLM call failed provider=openai error=%s", e, exc_info=True)
            sys.stderr.write(f"[crtv] OpenAI API failed: {e}\n")
        return ""


def get_provider(use_medgemma: bool | None = None) -> TriageLLM:
    """
    Factory: select provider from CRTV_LLM_PROVIDER (or hint).
    Values: rule | medgemma | openai
    Caching: enabled by default for medgemma/openai; set CRTV_LLM_CACHE=0 to disable.
    """
    explicit = os.environ.get("CRTV_LLM_PROVIDER")
    if explicit:
        provider = explicit.lower()
    elif use_medgemma:
        provider = "medgemma"
    else:
        provider = "rule"
    use_cache = os.environ.get("CRTV_LLM_CACHE", "1").lower() not in ("0", "false", "no")

    base: TriageLLM
    if provider == "medgemma":
        model_id = os.environ.get("MEDGEMMA_MODEL", "google/medgemma-4b-it")
        base = MedGemmaProvider(model_id=model_id)
    elif provider == "openai":
        base = OpenAICompatibleProvider()
    else:
        return RuleBasedProvider()

    if use_cache:
        cache_dir = os.environ.get("CRTV_LLM_CACHE_DIR", ".llm_cache")
        return CachingProvider(base, cache_dir=cache_dir)
    return base
