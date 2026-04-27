"""Run clinician-chat Q/GT tests against a specific version with an LLM judge."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from chatbot import clinician_tests_store, config_store
from chatbot.chat_service import _load_card, _build_card_context
from chatbot.patient_tests_runner import JUDGE_MODEL, JUDGE_SYSTEM
from chatbot.patient_chat_service import _make_client

logger = logging.getLogger("crtv.chatbot.clinician_tests")


def _run_chat_for_version(case: dict, version: dict) -> str:
    card = _load_card(int(case["patient_id"]), case["checkpoint_date"])
    if card is None:
        return ""
    client, err = _make_client()
    if err:
        raise RuntimeError(err)
    system_content = (
        version["system_prompt"]
        + "\n\n## REFERENCE\n\n"
        + (version.get("kb") or "")
        + "\n\n## CARD DATA\n\n"
        + _build_card_context(card)
    )
    resp = client.chat.completions.create(
        model=version["model"],
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": case["question"]},
        ],
        max_completion_tokens=2048,
    )
    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()


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
    version = config_store.get_version(version_id)
    if version is None:
        raise ValueError(f"Unknown version_id {version_id}")
    cases = clinician_tests_store.list_cases()
    results: list[dict] = []
    for c in cases:
        try:
            answer = _run_chat_for_version(c, version)
        except Exception as e:
            logger.warning("chat failed for case %s: %s", c["id"], e)
            answer = ""
        score = _judge(c["question"], c["gt_answer"], answer) if answer else 1
        results.append(
            {
                "case_id": c["id"],
                "patient_id": c["patient_id"],
                "checkpoint_date": c["checkpoint_date"],
                "question": c["question"],
                "gt_answer": c["gt_answer"],
                "answer": answer,
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
    clinician_tests_store.save_run(version_id, run)
    return run
