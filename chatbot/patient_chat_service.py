"""Patient-facing RAG chat.

Combines retrieved passages from ``chatbot/index/`` with an optional, compact
patient-card summary, and calls the LLM with the patient-chatbot system prompt.
Designed to run standalone (no patient attached) or grounded in a specific
triage card + week.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from chatbot import patient_config_store
from chatbot.retriever import get_retriever

logger = logging.getLogger("crtv.chatbot.patient")

CARD_STORE_DIRS = [
    Path(__file__).parent.parent / "card_store_all",
    Path(__file__).parent.parent / "demo_mining" / "card_store",
]
TOP_K = 3


def _load_card(patient_id: int, checkpoint_date: str) -> dict | None:
    fname = f"p{patient_id}_w{checkpoint_date[:10]}.json"
    for d in CARD_STORE_DIRS:
        fp = d / fname
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    return None


def _summarize_card(card: dict) -> str:
    """Compact patient summary — just the numbers and headline findings, no time series."""
    out: list[str] = []
    triage = card.get("card") or {}
    out.append(f"Patient #{card.get('patient_id')} · week ending {card.get('checkpoint_date')}")
    if triage.get("headline"):
        out.append(f"Clinician headline: {triage['headline']}")
    disp = card.get("disposition")
    if disp:
        out.append(f"Disposition: {disp}")
    adh = card.get("adherence") or {}
    if adh:
        done = round(adh.get("done_total") or 0)
        planned = round(adh.get("planned_total") or 0)
        pct = adh.get("adherence_minutes")
        pct_s = f"{round(pct * 100)}%" if isinstance(pct, (int, float)) else "—"
        out.append(f"Adherence: {done}/{planned} min ({pct_s})")
    n_sessions = len(card.get("sessions") or [])
    if n_sessions:
        out.append(f"Sessions this week: {n_sessions}")
    drift = card.get("drift_events") or []
    if drift:
        types = ", ".join(sorted({d.get("type") for d in drift if d.get("type")}))
        out.append(f"Drift signals: {types}")
    raw_obs = triage.get("observations")
    if not raw_obs:
        ev = triage.get("evidence")
        raw_obs = ev.get("items") if isinstance(ev, dict) else ev
    obs = [o for o in (raw_obs or []) if isinstance(o, dict)][:4]
    if obs:
        out.append("Key observations:")
        for o in obs:
            t = (o.get("text") or "").strip()
            if t:
                out.append(f"  - {t}")
    checkin = card.get("checkin") or {}
    if isinstance(checkin, dict) and checkin.get("message"):
        out.append(f"Most recent patient self-report theme: {checkin['message'][:200]}")
    return "\n".join(out)


def _format_passages(hits: list[dict]) -> str:
    if not hits:
        return "(no passages retrieved)"
    blocks = []
    for h in hits:
        header = f"[{h.get('source_label', 'source')} — {h.get('section_title', '')}]"
        blocks.append(f"{header}\n{h.get('text', '').strip()}")
    return "\n\n---\n\n".join(blocks)


def _build_api_messages(
    messages: list[dict],
    patient_id: int | None,
    checkpoint_date: str | None,
) -> tuple[list[dict], list[dict], bool, dict]:
    """Shared setup: retrieval, patient context, system prompt. Returns
    (api_messages, hits, patient_grounded, cfg)."""
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user" and (m.get("content") or "").strip():
            last_user = m["content"].strip()
            break

    hits: list[dict] = []
    if last_user:
        try:
            hits = get_retriever().search(last_user, k=TOP_K)
        except Exception as e:
            logger.warning("retrieval failed: %s", e)

    cfg = patient_config_store.get_active()

    patient_context = ""
    if patient_id is not None and checkpoint_date:
        card = _load_card(int(patient_id), checkpoint_date)
        if card:
            patient_context = _summarize_card(card)

    system_content = cfg["system_prompt"]
    if hits:
        system_content += (
            "\n\n## REFERENCE GUIDELINES\n\n"
            "The excerpts below are drawn from trusted stroke-care guidelines and "
            "patient handbooks. Treat them as background guidance that should shape "
            "your answer when relevant — not as a script to quote or a required "
            "citation. Use your judgement about when they apply; ignore any that "
            "don't fit the user's question.\n\n"
            + _format_passages(hits)
        )
    if patient_context:
        system_content += "\n\n## PATIENT CONTEXT\n\n" + patient_context
    else:
        system_content += "\n\n## PATIENT CONTEXT\n\n(none — speak generally)"

    api_messages = [{"role": "system", "content": system_content}]
    for m in messages:
        if m.get("content"):
            api_messages.append({"role": m["role"], "content": m["content"]})

    return api_messages, hits, bool(patient_context), cfg


def _citations_from_hits(hits: list[dict]) -> list[dict]:
    return [
        {
            "source_label": h.get("source_label"),
            "section_title": h.get("section_title"),
            "source_file": h.get("source_file"),
            "page_start": h.get("page_start"),
            "page_end": h.get("page_end"),
            "score": h.get("score"),
        }
        for h in hits
    ]


def _make_client():
    api_key = os.environ.get("CRTV_OPENAI_API_KEY", "")
    if not api_key:
        return None, "CRTV_OPENAI_API_KEY not set"
    try:
        from openai import OpenAI
    except ImportError:
        return None, "openai package not installed"
    return OpenAI(
        api_key=api_key,
        base_url=os.environ.get("CRTV_OPENAI_BASE_URL") or None,
        timeout=float(os.environ.get("CRTV_CHAT_TIMEOUT", "30")),
        max_retries=2,
    ), None


class PatientChatService:
    def chat(
        self,
        messages: list[dict],
        patient_id: int | None = None,
        checkpoint_date: str | None = None,
    ) -> dict:
        api_messages, hits, grounded, cfg = _build_api_messages(
            messages, patient_id, checkpoint_date
        )
        if len(api_messages) <= 1:
            return {"response": "", "error": "No user message"}

        client, err = _make_client()
        if err:
            return {"response": "", "error": err}

        model = cfg.get("model", "gpt-5.4-nano")
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=api_messages,
                max_completion_tokens=1024,
            )
            text = ""
            if resp.choices:
                text = (resp.choices[0].message.content or "").strip()
            return {
                "response": text,
                "model": model,
                "version_id": cfg["id"],
                "citations": _citations_from_hits(hits),
                "patient_grounded": grounded,
            }
        except Exception as e:
            logger.warning("patient chat llm failed: %s", e, exc_info=True)
            return {"response": "", "error": str(e)}

    def chat_stream(
        self,
        messages: list[dict],
        patient_id: int | None = None,
        checkpoint_date: str | None = None,
    ) -> Iterator[dict]:
        """Yield SSE-ready event dicts: 'token', 'citations', 'done', or 'error'."""
        api_messages, hits, grounded, cfg = _build_api_messages(
            messages, patient_id, checkpoint_date
        )
        if len(api_messages) <= 1:
            yield {"type": "error", "error": "No user message"}
            return

        client, err = _make_client()
        if err:
            yield {"type": "error", "error": err}
            return

        model = cfg.get("model", "gpt-5.4-nano")
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=api_messages,
                max_completion_tokens=1024,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield {"type": "token", "text": piece}
            yield {
                "type": "citations",
                "citations": _citations_from_hits(hits),
                "patient_grounded": grounded,
                "model": model,
                "version_id": cfg["id"],
            }
            yield {"type": "done"}
        except Exception as e:
            logger.warning("patient chat stream failed: %s", e, exc_info=True)
            yield {"type": "error", "error": str(e)}
