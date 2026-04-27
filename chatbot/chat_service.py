"""Chat service: answers clinician follow-up questions about a triage card."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from chatbot.config_store import get_active

logger = logging.getLogger("crtv.chatbot")

CARD_STORE_DIRS = [
    Path(__file__).parent.parent / "card_store_all",
    Path(__file__).parent.parent / "demo_mining" / "card_store",
]


def _load_card(patient_id: int, checkpoint_date: str) -> dict | None:
    fname = f"p{patient_id}_w{checkpoint_date[:10]}.json"
    for d in CARD_STORE_DIRS:
        fp = d / fname
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    return None


def _build_card_context(card_json: dict) -> str:
    return json.dumps(card_json, indent=2, default=str)


class ChatService:
    def chat(
        self,
        patient_id: int,
        checkpoint_date: str,
        messages: list[dict],
    ) -> dict:
        card = _load_card(patient_id, checkpoint_date)
        if card is None:
            return {
                "response": "",
                "error": f"Card not found: p{patient_id} w{checkpoint_date}",
            }

        cfg = get_active()
        system_content = (
            cfg["system_prompt"]
            + "\n\n## REFERENCE\n\n"
            + cfg["kb"]
            + "\n\n## CARD DATA\n\n"
            + _build_card_context(card)
        )

        api_key = os.environ.get("CRTV_OPENAI_API_KEY", "")
        if not api_key:
            return {"response": "", "error": "CRTV_OPENAI_API_KEY not set"}

        try:
            from openai import OpenAI
        except ImportError:
            return {
                "response": "",
                "error": "openai package not installed",
            }

        base_url = os.environ.get("CRTV_OPENAI_BASE_URL")
        timeout = float(os.environ.get("CRTV_CHAT_TIMEOUT", "30"))

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=2,
        )

        api_messages = [{"role": "system", "content": system_content}]
        for m in messages:
            if m.get("content"):
                api_messages.append(
                    {"role": m["role"], "content": m["content"]}
                )

        model = cfg.get("model", "gpt-5.4-nano")

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=api_messages,
                max_completion_tokens=2048,
            )
            text = ""
            if resp.choices:
                text = (resp.choices[0].message.content or "").strip()
            return {
                "response": text,
                "model": model,
                "version_id": cfg["id"],
            }
        except Exception as e:
            logger.warning("chatbot LLM call failed: %s", e, exc_info=True)
            return {"response": "", "error": str(e)}
