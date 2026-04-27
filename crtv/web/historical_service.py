"""
HistoricalTriageService - serves pre-built triage cards from the CardStore.

Cards are built offline by `python -m demo_mining.build_cards` and persisted
under `demo_mining/card_store/`. This service is read-only: no LLM calls at
request time. If `demo_mining/shortlist.json` exists, the cache is filtered
to just the shortlisted (patient, checkpoint) pairs.
"""

import json
import logging
from collections import defaultdict
from pathlib import Path

from crtv.adapters import get_adapter
from demo_mining.card_store import CardStore

logger = logging.getLogger("crtv.web.historical_service")


class HistoricalTriageService:
    """Load pre-built triage cards from CardStore and serve them."""

    def __init__(
        self,
        data_dir: str | Path,
        use_medgemma: bool = False,  # retained for API compat; unused in read-only mode
        card_store_path: str | Path = "demo_mining/card_store",
        demo_selection_path: str | Path = "demo_mining/demo_selection.json",
    ):
        self.data_dir = Path(data_dir)
        self.adapter = get_adapter(self.data_dir)
        self.store = CardStore(card_store_path)
        self.demo_selection_path = Path(demo_selection_path)
        self._cache: list[dict] = []
        self._cache_done = False

    def _load_clinician_patients(self) -> set[int] | None:
        """Frozen clinician-view patient ids. Returns None if the file is missing
        (meaning no filter — show everything in the store)."""
        if not self.demo_selection_path.exists():
            return None
        try:
            raw = json.loads(self.demo_selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("could not read demo_selection %s: %s", self.demo_selection_path, e)
            return None
        return {int(p) for p in raw.get("clinician", [])}

    def _ensure_cache(self):
        if self._cache_done:
            return
        entries = self.store.load_all()
        allowed = self._load_clinician_patients()
        if allowed is not None:
            entries = [e for e in entries if int(e["patient_id"]) in allowed]
            logger.info("loaded %d cards for %d clinician-view patients", len(entries), len(allowed))
        else:
            logger.info("loaded %d cards (no demo_selection filter)", len(entries))
        self._cache = sorted(entries, key=lambda x: (x["patient_id"], x["checkpoint_date"]))
        self._cache_done = True

    def _max_attention_for_card(self, c: dict) -> int:
        """Max observation.attention (1-3) for this card; 0 if none."""
        fc = c.get("full_card")
        ev = getattr(fc, "evidence", {}) if fc else {}
        items = ev.get("items", []) if isinstance(ev, dict) else []
        if not items:
            return 2 if c.get("disposition") == "TRIAGE" else 1
        return max((x.get("attention", 1) for x in items if isinstance(x, dict)), default=1)

    def list_triage_cards(self) -> list[dict]:
        """All triage cards (patient, checkpoint) for clinician window."""
        self._ensure_cache()
        return [
            {
                "patient_id": c["patient_id"],
                "checkpoint_date": c["checkpoint_date"],
                "headline": c["headline"],
                "disposition": c["disposition"],
                "diagnosis": c["diagnosis"],
                "attention_level": self._max_attention_for_card(c),
            }
            for c in self._cache
        ]

    def list_patients_grouped(self) -> list[dict]:
        """Patients grouped: one row per patient, max attention across their triages."""
        self._ensure_cache()
        by_patient: dict[int, dict] = {}
        for c in self._cache:
            pid = c["patient_id"]
            att = self._max_attention_for_card(c)
            if pid not in by_patient or att > by_patient[pid].get("attention_level", 0):
                by_patient[pid] = {
                    "patient_id": pid,
                    "headline": c["headline"],
                    "disposition": c["disposition"],
                    "attention_level": att,
                    "checkpoint_count": 0,
                }
            by_patient[pid]["checkpoint_count"] = by_patient[pid].get("checkpoint_count", 0) + 1
        return sorted(by_patient.values(), key=lambda x: (-x["attention_level"], x["patient_id"]))

    def get_patient_detail(self, patient_id: int) -> dict | None:
        """All triages for patient, sorted by checkpoint_date (oldest first)."""
        self._ensure_cache()
        patient_cards = [c for c in self._cache if c["patient_id"] == patient_id]
        if not patient_cards:
            return None
        triages = []
        for c in sorted(patient_cards, key=lambda x: x["checkpoint_date"]):
            detail = self.get_card_detail(patient_id, c["checkpoint_date"])
            if detail:
                triages.append(detail)
        return {"patient_id": patient_id, "triages": triages} if triages else None

    def get_card_detail(self, patient_id: int, checkpoint_date: str) -> dict | None:
        """Full detail for a specific triage card."""
        self._ensure_cache()
        adapter = self.adapter
        for c in self._cache:
            if c["patient_id"] != patient_id or c["checkpoint_date"] != checkpoint_date:
                continue
            adh = c.get("adherence")
            adh_calendar = None
            if adh:
                days = []
                for d, (planned, done) in sorted(adh.per_day.items()):
                    days.append({"date": str(d), "planned_min": planned, "done_min": done, "ratio": done / planned if planned > 0 else 0})
                adh_calendar = {"days": days, "adherence_minutes": adh.adherence_minutes, "planned_total": adh.planned_minutes, "done_total": adh.done_minutes}
            metrics = c.get("metrics", {})
            sessions_raw = metrics.get("sessions", [])
            session_by_id = {s["session_id"]: s for s in sessions_raw}
            sessions = []
            for s in sessions_raw:
                sid = s["session_id"]
                proto_id = s.get("protocol_id", 0)
                sessions.append({
                    "session_id": sid, "start_time": s["start_time"], "duration_sec": s["duration_sec"],
                    "status": "CLOSED", "protocol_id": proto_id,
                    "protocol_name": adapter.get_protocol_name(proto_id) or f"Protocol {proto_id}",
                })
            perf_raw = metrics.get("performance", [])
            diff_raw = metrics.get("difficulty", [])
            perf_by_date = defaultdict(list)
            for p in perf_raw:
                sid = p.get("session_id")
                sess = session_by_id.get(sid)
                dt = (sess.get("start_time", "")[:10]) if sess else ""
                if dt:
                    perf_by_date[dt].append(p.get("performance_mean", 0))
            perf_with_date = [
                {"date": d, "performance_mean": sum(v) / len(v)}
                for d, v in sorted(perf_by_date.items())
            ]
            diff_by_date = defaultdict(list)
            for d in diff_raw:
                sid = d.get("session_id")
                sess = session_by_id.get(sid)
                dt = (sess.get("start_time", "")[:10]) if sess else ""
                if dt:
                    diff_by_date[dt].append(d.get("difficulty_mean", 0))
            diff_with_date = [
                {"date": d, "difficulty_mean": sum(v) / len(v)}
                for d, v in sorted(diff_by_date.items())
            ]
            protocol_wise = metrics.get("protocol_wise", {})
            for pwid, data in protocol_wise.items():
                if isinstance(data, dict) and "name" not in data:
                    data["name"] = adapter.get_protocol_name(int(pwid) if isinstance(pwid, str) and pwid.isdigit() else pwid)
            drift_types = c.get("drift_types", [])
            evidence_items = c["full_card"].evidence.get("items", []) if c["full_card"].evidence else []

            # Adherence as daily line: ratio per day
            adherence_line = []
            if adh_calendar and adh_calendar.get("days"):
                adherence_line = [
                    {"date": str(d["date"])[:10], "value": d.get("ratio", 0)}
                    for d in sorted(adh_calendar["days"], key=lambda x: x["date"])
                ]

            # Self-reports as lines by key (date -> value)
            self_reports_raw = metrics.get("self_reports", [])
            self_reports_by_key = defaultdict(list)
            for r in self_reports_raw:
                ts = r.get("timestamp", "")
                dt = ts[:10] if isinstance(ts, str) else ""
                try:
                    val = float(r.get("value", 0))
                except (TypeError, ValueError):
                    val = 0
                if dt:
                    self_reports_by_key[r.get("key", "value")].append({"date": dt, "value": val})
            for k in self_reports_by_key:
                self_reports_by_key[k].sort(key=lambda x: x["date"])

            # Protocol-wise: aggregate by date per protocol
            pw_lines = {}
            for pwid, data in protocol_wise.items():
                if not isinstance(data, dict):
                    continue
                perf_list = data.get("performance", [])
                diff_list = data.get("difficulty", [])
                adh_pct = data.get("adherence_pct")
                adh_daily = data.get("adherence_daily") or []
                pw_lines[str(pwid)] = {
                    "name": data.get("name", f"Protocol {pwid}"),
                    "performance": [{"date": x.get("date", ""), "value": x.get("value", x.get("performance_mean", 0))} for x in perf_list],
                    "difficulty": [{"date": x.get("date", ""), "value": x.get("value", x.get("difficulty_mean", 0))} for x in diff_list],
                    "adherence": [{"date": str(x.get("date", ""))[:10], "value": x.get("value")} for x in adh_daily],
                    "adherence_pct": adh_pct,
                }

            return {
                "patient_id": patient_id,
                "checkpoint_date": checkpoint_date,
                "card": {
                    "headline": c["full_card"].headline,
                    "observations": evidence_items,
                    "evidence": evidence_items,
                    "recommended_actions": [{"action_type": a.action_type, "params": a.params} for a in c["full_card"].recommended_actions],
                    "severity": c.get("severity"),
                    "disposition": c["disposition"],
                },
                "adherence": adh_calendar,
                "metrics": {
                    "performance": perf_with_date,
                    "difficulty": diff_with_date,
                    "adherence_line": adherence_line,
                    "learning_rates": metrics.get("learning_rates", []),
                    "protocol_wise": pw_lines,
                    "self_reports": dict(self_reports_by_key),
                },
                "sessions": sessions,
                "drift_events": [{"type": t} for t in drift_types],
            }
        return None
