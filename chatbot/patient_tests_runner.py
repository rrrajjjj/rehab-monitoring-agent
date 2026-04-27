"""Run Q/GT test cases against a specific prompt version with an LLM judge.

Standalone mode only — no patient context. For each case we call the chat API
using the target version's system prompt + model, then a judge LLM scores the
answer 1-5 on tone, usefulness, and inclusion of the ground-truth answer.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from chatbot import patient_config_store, patient_tests_store
from chatbot.patient_chat_service import (
    _citations_from_hits,
    _format_passages,
    _make_client,
    TOP_K,
)
from chatbot.retriever import get_retriever

logger = logging.getLogger("crtv.chatbot.tests")

JUDGE_MODEL = os.environ.get("CRTV_TEST_JUDGE_MODEL", "gpt-5.4-mini")

JUDGE_SYSTEM = """\
You are an expert evaluator of a stroke-care patient chatbot.

You will be given a question, a ground-truth answer, and the chatbot's answer.
Score the chatbot's answer on a single integer scale from 1 to 5, based on:
- Tone: warm, plain-spoken, non-clinical, emotionally supportive.
- Usefulness: gives the user something practical and actionable.
- Ground-truth inclusion: includes the essential facts from the ground-truth answer. Extra correct information is fine; missing key facts is not.

Scale:
5 = excellent on all three.
4 = good, minor weakness on one dimension.
3 = acceptable, clear weakness on one dimension OR partial GT coverage.
2 = poor — wrong tone, low usefulness, or missing most of the GT.
1 = unusable — harmful, wrong, or unrelated.

Respond with ONLY the integer score (1, 2, 3, 4, or 5). No other text."""


def _run_chat_for_version(question: str, version: dict) -> tuple[str, list[dict]]:
    """Minimal standalone chat, using the given version's prompt + model."""
    client, err = _make_client()
    if err:
        raise RuntimeError(err)

    hits = []
    try:
        hits = get_retriever().search(question, k=TOP_K)
    except Exception as e:
        logger.warning("retrieval failed: %s", e)

    system_content = version["system_prompt"]
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
    system_content += "\n\n## PATIENT CONTEXT\n\n(none — speak generally)"

    resp = client.chat.completions.create(
        model=version["model"],
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": question},
        ],
        max_completion_tokens=1024,
    )
    text = ""
    if resp.choices:
        text = (resp.choices[0].message.content or "").strip()
    return text, _citations_from_hits(hits)


def _judge(question: str, gt: str, answer: str) -> int | None:
    if not answer.strip():
        return 1
    client, err = _make_client()
    if err:
        raise RuntimeError(err)
    user = (
        f"QUESTION:\n{question}\n\n"
        f"GROUND-TRUTH ANSWER:\n{gt}\n\n"
        f"CHATBOT ANSWER:\n{answer}\n\n"
        "Score (1-5 only):"
    )
    try:
        resp = client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            max_completion_tokens=8,
        )
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        m = re.search(r"[1-5]", raw)
        return int(m.group(0)) if m else None
    except Exception as e:
        logger.warning("judge call failed: %s", e, exc_info=True)
        return None


def run_tests(version_id: int) -> dict:
    version = patient_config_store.get_version(version_id)
    if version is None:
        raise ValueError(f"Unknown version_id {version_id}")
    cases = patient_tests_store.list_cases()
    results: list[dict] = []
    for c in cases:
        try:
            answer, citations = _run_chat_for_version(c["question"], version)
        except Exception as e:
            logger.warning("chat failed for case %s: %s", c["id"], e)
            answer, citations = "", []
        score = _judge(c["question"], c["gt_answer"], answer) if answer else 1
        results.append(
            {
                "case_id": c["id"],
                "question": c["question"],
                "gt_answer": c["gt_answer"],
                "answer": answer,
                "citations": citations,
                "score": score,
                "manual_override": False,
            }
        )
    scored = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    avg = round(sum(scored) / len(scored), 2) if scored else None
    run = {
        "version_id": version_id,
        "model": version["model"],
        "judge_model": JUDGE_MODEL,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "avg_score": avg,
        "results": results,
    }
    patient_tests_store.save_run(version_id, run)
    return run
