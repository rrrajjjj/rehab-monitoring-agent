"""Versioned store for the triage card-generation system prompt.

Prompt-only (no model, no KB). The triage pipeline reads the active version
via ``get_active()`` — the on-disk ``prompts/triage_system.txt`` serves as
the seed for v1 when the store is empty.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "triage_config"
VERSIONS_DIR = CONFIG_DIR / "versions"
ACTIVE_FILE = CONFIG_DIR / "active.json"

_SEED_FILE = Path(__file__).parent.parent / "prompts" / "triage_system.txt"


def _seed_prompt() -> str:
    if _SEED_FILE.exists():
        try:
            return _SEED_FILE.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _ensure_init():
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVE_FILE.exists() or not list(VERSIONS_DIR.glob("v*.json")):
        save_new(_seed_prompt(), label="initial")


def _next_id() -> int:
    existing = [
        int(p.stem[1:])
        for p in VERSIONS_DIR.glob("v*.json")
        if p.stem[1:].isdigit()
    ]
    return (max(existing) + 1) if existing else 1


def save_new(system_prompt: str, label: str | None = None) -> dict:
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    vid = _next_id()
    payload = {
        "id": vid,
        "system_prompt": system_prompt,
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
    out = []
    for p in sorted(VERSIONS_DIR.glob("v*.json"), key=lambda x: int(x.stem[1:])):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append(
            {
                "id": d["id"],
                "label": d.get("label", ""),
                "created_at": d["created_at"],
            }
        )
    out.reverse()
    return out


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
