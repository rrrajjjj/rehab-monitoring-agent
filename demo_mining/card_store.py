"""
On-disk card store: one JSON file per (patient_id, checkpoint_date).

The `HistoricalTriageRunner` produces an in-memory card entry dict holding
Pydantic models (`TriageCard`) and dataclasses (`AdherenceReport`, `DriftEvent`).
CardStore serializes those into plain JSON and reconstructs them on load so
the web service can stay shape-compatible with the old "in-memory cache".
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from crtv.domain.models import TriageCard
from crtv.features.adherence import AdherenceReport

logger = logging.getLogger("demo_mining.card_store")


def _key(patient_id: int, checkpoint_date: str) -> str:
    return f"p{int(patient_id)}_w{str(checkpoint_date)[:10]}"


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if is_dataclass(o):
        return asdict(o)
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def _serialize_adherence(adh: AdherenceReport | None) -> dict | None:
    if adh is None:
        return None
    return {
        "patient_id": adh.patient_id,
        "window_start": adh.window_start.isoformat(),
        "window_end": adh.window_end.isoformat(),
        "planned_minutes": adh.planned_minutes,
        "done_minutes": adh.done_minutes,
        "adherence_minutes": adh.adherence_minutes,
        "adherence_sessions": adh.adherence_sessions,
        "adherence_days": adh.adherence_days,
        "per_day": {d.isoformat(): list(v) for d, v in adh.per_day.items()},
        "evidence_map": {k: list(v) for k, v in adh.evidence_map.items()},
    }


def _deserialize_adherence(raw: dict | None) -> AdherenceReport | None:
    if raw is None:
        return None
    return AdherenceReport(
        patient_id=raw["patient_id"],
        window_start=date.fromisoformat(raw["window_start"][:10]),
        window_end=date.fromisoformat(raw["window_end"][:10]),
        planned_minutes=raw["planned_minutes"],
        done_minutes=raw["done_minutes"],
        adherence_minutes=raw.get("adherence_minutes"),
        adherence_sessions=raw["adherence_sessions"],
        adherence_days=raw["adherence_days"],
        per_day={date.fromisoformat(k): tuple(v) for k, v in (raw.get("per_day") or {}).items()},
        evidence_map={k: list(v) for k, v in (raw.get("evidence_map") or {}).items()},
    )


class _DriftEventLite:
    """Read-side stub so downstream code that reads .type / .severity keeps working."""

    __slots__ = ("type", "severity")

    def __init__(self, type: str, severity: int | str | None):
        self.type = type
        self.severity = severity


def _serialize_entry(entry: dict) -> dict:
    card = entry["card"]
    card_dict = card.model_dump() if hasattr(card, "model_dump") else card
    drift_raw = entry.get("drift_events") or []
    drift_list = [
        {"type": getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else None),
         "severity": getattr(e, "severity", None) if not isinstance(e, dict) else e.get("severity")}
        for e in drift_raw
    ]
    return {
        "patient_id": entry["patient_id"],
        "checkpoint_date": entry["checkpoint_date"],
        "disposition": entry["disposition"],
        "severity": entry.get("severity"),
        "diagnosis": entry.get("diagnosis"),
        "card": card_dict,
        "drift_events": drift_list,
        "adherence": _serialize_adherence(entry.get("adherence")),
        "metrics": entry.get("metrics") or {},
        "checkin": entry.get("checkin"),
    }


def _deserialize_entry(raw: dict) -> dict:
    card = TriageCard(**raw["card"]) if raw.get("card") else None
    drift = [_DriftEventLite(e["type"], e.get("severity")) for e in (raw.get("drift_events") or [])]
    return {
        "patient_id": raw["patient_id"],
        "checkpoint_date": raw["checkpoint_date"],
        "disposition": raw["disposition"],
        "severity": raw.get("severity"),
        "diagnosis": raw.get("diagnosis"),
        "card": card,
        "full_card": card,                   # historical_service accesses both names
        "drift_events": drift,
        "drift_types": [e.type for e in drift],
        "adherence": _deserialize_adherence(raw.get("adherence")),
        "adherence_pct": (raw.get("adherence") or {}).get("adherence_minutes"),
        "headline": card.headline if card else None,
        "metrics": raw.get("metrics") or {},
        "reasons": card.reasons if card else [],
        "checkin": raw.get("checkin"),
    }


class CardStore:
    """Flat-file JSON card store rooted at `path`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, entry: dict) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        p = self.path / f"{_key(entry['patient_id'], entry['checkpoint_date'])}.json"
        payload = _serialize_entry(entry)
        p.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
        return p

    def load(self, patient_id: int, checkpoint_date: str) -> dict | None:
        p = self.path / f"{_key(patient_id, checkpoint_date)}.json"
        if not p.exists():
            return None
        return _deserialize_entry(json.loads(p.read_text(encoding="utf-8")))

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for p in sorted(self.path.glob("p*_w*.json")):
            try:
                out.append(_deserialize_entry(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("skipping malformed card %s: %s", p.name, e)
        return out

    def keys(self) -> list[tuple[int, str]]:
        return [(e["patient_id"], e["checkpoint_date"]) for e in self.load_all()]

    def __contains__(self, pair: tuple[int, str]) -> bool:
        pid, dt = pair
        return (self.path / f"{_key(pid, dt)}.json").exists()

    def __len__(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for _ in self.path.glob("p*_w*.json"))
