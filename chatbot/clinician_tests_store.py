"""Ground-truth Q/A tests for the clinician chatbot.

Cases are card-scoped — each binds to a specific (patient_id, checkpoint_date)
so the chat has a card to reason about. One latest run per prompt-version at
``clinician_tests/runs/v{vid}.json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent / "clinician_tests"
CASES_FILE = ROOT / "cases.json"
RUNS_DIR = ROOT / "runs"


def _ensure():
    ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if not CASES_FILE.exists():
        CASES_FILE.write_text("[]", encoding="utf-8")


def list_cases() -> list[dict]:
    _ensure()
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def _write_cases(cases: list[dict]):
    CASES_FILE.write_text(
        json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def add_case(patient_id: int, checkpoint_date: str, question: str, gt_answer: str) -> dict:
    cases = list_cases()
    next_id = (max((c["id"] for c in cases), default=0) + 1)
    case = {
        "id": next_id,
        "patient_id": int(patient_id),
        "checkpoint_date": checkpoint_date.strip()[:10],
        "question": question.strip(),
        "gt_answer": gt_answer.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    cases.append(case)
    _write_cases(cases)
    return case


def update_case(case_id: int, patient_id: int, checkpoint_date: str, question: str, gt_answer: str) -> dict | None:
    cases = list_cases()
    for c in cases:
        if c["id"] == case_id:
            c["patient_id"] = int(patient_id)
            c["checkpoint_date"] = checkpoint_date.strip()[:10]
            c["question"] = question.strip()
            c["gt_answer"] = gt_answer.strip()
            _write_cases(cases)
            return c
    return None


def delete_case(case_id: int) -> bool:
    cases = list_cases()
    new = [c for c in cases if c["id"] != case_id]
    if len(new) == len(cases):
        return False
    _write_cases(new)
    return True


def _run_path(version_id: int) -> Path:
    return RUNS_DIR / f"v{version_id}.json"


def save_run(version_id: int, run: dict):
    _ensure()
    _run_path(version_id).write_text(
        json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_run(version_id: int) -> dict | None:
    p = _run_path(version_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def all_run_summaries() -> dict[int, dict]:
    _ensure()
    out: dict[int, dict] = {}
    for p in RUNS_DIR.glob("v*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = data.get("version_id")
        if vid is None:
            continue
        out[int(vid)] = {
            "avg_score": data.get("avg_score"),
            "ran_at": data.get("ran_at"),
            "n": len(data.get("results", [])),
        }
    return out


def override_rating(version_id: int, case_id: int, score: int) -> dict | None:
    run = get_run(version_id)
    if run is None:
        return None
    found = False
    for r in run.get("results", []):
        if r["case_id"] == case_id:
            r["score"] = score
            r["manual_override"] = True
            found = True
            break
    if not found:
        return None
    scores = [r["score"] for r in run["results"] if isinstance(r.get("score"), (int, float))]
    run["avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
    save_run(version_id, run)
    return run
