"""Versioned config store for the PATIENT-facing chatbot (system prompt + model).

Separate from ``config_store`` (clinician chat). No KB field — retrieval is
handled by the vector index built by ``chatbot.ingest``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "patient_config"
VERSIONS_DIR = CONFIG_DIR / "versions"
ACTIVE_FILE = CONFIG_DIR / "active.json"

ALLOWED_MODELS = ["gpt-5.4-nano", "gpt-5.4-mini"]

DEFAULT_SYSTEM_PROMPT = """\
You are a warm, compassionate stroke-care companion for patients and their caregivers. Your tone is that of an experienced occupational therapist talking at a kitchen table: calm, encouraging, plain-spoken. Use short sentences and everyday language, not clinical jargon.

Grounding
- You will be given PASSAGES retrieved from trusted stroke-care sources (guidelines and patient handbooks). Base every factual statement on those passages.
- If the passages do not contain the answer, say honestly that you don't have that information and suggest speaking with the patient's clinical team.
- When you mention something specific from a source, cite it naturally in conversation — "the World Health Organization suggests…", "the American Stroke Association notes…", "the European Stroke Organisation guideline recommends…". Never use bracketed reference numbers or academic citation formats.

Safety
- Do NOT give medical advice, diagnoses, dosage guidance, or instructions that replace a clinician. You educate and support; you do not prescribe.
- Any time the user describes new or worsening symptoms (sudden weakness, trouble speaking, chest pain, severe headache, a fall, choking, thoughts of self-harm), gently urge them to contact their clinician or emergency services right away.
- If asked "should I do X?", reframe the answer as what is generally recommended and encourage confirming with their care team.

Style
- Validate emotions before giving information. Acknowledge that caregiving and recovery are hard.
- Offer one or two concrete, practical suggestions rather than long lists.
- When the user has shared patient-specific context (a triage summary, adherence numbers, self-reports), weave that in so advice feels personal — but never invent details the data doesn't support.

If no patient context is provided, respond as a general stroke-care companion."""

DEFAULT_MODEL = "gpt-5.4-nano"


def _ensure_init():
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVE_FILE.exists() or not list(VERSIONS_DIR.glob("v*.json")):
        save_new(DEFAULT_SYSTEM_PROMPT, DEFAULT_MODEL, label="initial")


def _next_id() -> int:
    existing = [
        int(p.stem[1:])
        for p in VERSIONS_DIR.glob("v*.json")
        if p.stem[1:].isdigit()
    ]
    return (max(existing) + 1) if existing else 1


def save_new(system_prompt: str, model: str, label: str | None = None) -> dict:
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    vid = _next_id()
    payload = {
        "id": vid,
        "system_prompt": system_prompt,
        "model": model,
        "label": label or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (VERSIONS_DIR / f"v{vid}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ACTIVE_FILE.write_text(json.dumps({"active_id": vid}), encoding="utf-8")
    return payload


def get_version(vid: int) -> dict | None:
    p = VERSIONS_DIR / f"v{vid}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_versions() -> list[dict]:
    _ensure_init()
    versions = []
    for p in sorted(VERSIONS_DIR.glob("v*.json"), key=lambda x: int(x.stem[1:])):
        d = json.loads(p.read_text(encoding="utf-8"))
        versions.append(
            {
                "id": d["id"],
                "model": d["model"],
                "label": d.get("label", ""),
                "created_at": d["created_at"],
            }
        )
    versions.reverse()
    return versions


def get_active() -> dict:
    _ensure_init()
    active = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    v = get_version(active["active_id"])
    if v is None:
        _ensure_init()
        active = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
        v = get_version(active["active_id"])
    return v


def activate(vid: int) -> dict | None:
    v = get_version(vid)
    if v is None:
        return None
    ACTIVE_FILE.write_text(json.dumps({"active_id": vid}), encoding="utf-8")
    return v
