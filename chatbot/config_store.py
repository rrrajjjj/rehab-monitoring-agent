"""Versioned config store for the chatbot: system prompt, KB, and model."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "config"
VERSIONS_DIR = CONFIG_DIR / "versions"
ACTIVE_FILE = CONFIG_DIR / "active.json"
KB_SEED_FILE = Path(__file__).parent / "kb.md"

ALLOWED_MODELS = ["gpt-5.4-nano", "gpt-5.4-mini"]

DEFAULT_SYSTEM_PROMPT = """\
You are a clinical assistant helping a rehabilitation clinician interpret a patient's weekly triage card from a stroke telerehabilitation platform.

You have two sources of information:
1. REFERENCE — definitions of metrics, protocols, and clinical interpretation cues.
2. CARD DATA — the specific patient's full 4-week metrics, observations, drift events, adherence, and self-reports.

Rules:
- Answer by reading the data directly. Quote specific values, dates, and protocol names.
- If something is not in the data, say so plainly.
- Do not recommend clinical actions. Explain what the data shows and let the clinician decide.
- Keep answers concise — a few sentences, not paragraphs. Use bullet points for comparisons.
- When referencing protocols, include both the name and ID."""

DEFAULT_MODEL = "gpt-5.4-nano"


def _ensure_init():
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVE_FILE.exists() or not list(VERSIONS_DIR.glob("v*.json")):
        kb = KB_SEED_FILE.read_text(encoding="utf-8") if KB_SEED_FILE.exists() else ""
        save_new(DEFAULT_SYSTEM_PROMPT, kb, DEFAULT_MODEL, label="initial")


def _next_id() -> int:
    existing = [
        int(p.stem[1:])
        for p in VERSIONS_DIR.glob("v*.json")
        if p.stem[1:].isdigit()
    ]
    return (max(existing) + 1) if existing else 1


def save_new(
    system_prompt: str, kb: str, model: str, label: str | None = None
) -> dict:
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    vid = _next_id()
    payload = {
        "id": vid,
        "system_prompt": system_prompt,
        "kb": kb,
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


def get_active_id() -> int:
    _ensure_init()
    active = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    return active["active_id"]


def activate(vid: int) -> dict | None:
    v = get_version(vid)
    if v is None:
        return None
    ACTIVE_FILE.write_text(json.dumps({"active_id": vid}), encoding="utf-8")
    return v
