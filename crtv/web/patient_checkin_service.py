"""
PatientCheckInService - serves pre-built patient check-ins from the CardStore.

In real-data mode this is now read-only: check-ins are generated offline by
`python -m demo_mining.build_cards` and stored alongside the triage card. If
`demo_mining/shortlist.json` exists, the cache is filtered to just the
shortlisted patient_ids.

Mock mode still generates on demand from the MockAdapter for local dev.
"""

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from crtv.reasoning.patient_checkin import PatientCheckInEngine
from demo_mining.card_store import CardStore

logger = logging.getLogger("crtv.web.patient_checkin_service")


class PatientCheckInService:
    """Load patient check-ins from the card store (real) or mock adapter (mock)."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        adapter=None,
        use_medgemma: bool = False,
        card_store_path: str | Path = "demo_mining/card_store",
        demo_selection_path: str | Path = "demo_mining/demo_selection.json",
    ):
        self.use_medgemma = use_medgemma
        self.engine = PatientCheckInEngine(use_medgemma=use_medgemma)
        self._cache: list[dict] = []
        self._cache_done = False
        self.store = CardStore(card_store_path)
        self.demo_selection_path = Path(demo_selection_path)

        if data_dir is not None:
            self.data_dir = Path(data_dir)
            self._real_data = True
        elif adapter is not None:
            self._adapter = adapter
            self._real_data = False
        else:
            from crtv.adapters.mock_adapter import MockAdapter
            self._adapter = MockAdapter()
            self._real_data = False

    def _load_patient_view_patients(self) -> set[int] | None:
        if not self.demo_selection_path.exists():
            return None
        try:
            raw = json.loads(self.demo_selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("could not read demo_selection %s: %s", self.demo_selection_path, e)
            return None
        return {int(p) for p in raw.get("patient", [])}

    def _ensure_cache(self):
        if self._cache_done:
            return
        if self._real_data:
            self._build_real_data_cache()
        else:
            self._build_mock_cache()
        self._cache_done = True

    def _build_real_data_cache(self):
        """Load check-ins from the offline card store."""
        entries = self.store.load_all()
        allowed = self._load_patient_view_patients()
        if allowed is not None:
            entries = [e for e in entries if int(e["patient_id"]) in allowed]
        for e in entries:
            checkin = e.get("checkin")
            if not checkin:
                continue  # card built without a check-in
            self._cache.append({
                "patient_id": e["patient_id"],
                "checkpoint_date": e["checkpoint_date"],
                "checkin": checkin,
                "metrics_summary": self._summarize_metrics(e.get("metrics") or {}),
            })
        self._cache.sort(key=lambda x: (x["patient_id"], x["checkpoint_date"]))
        logger.info("loaded %d check-ins (demo_selection=%s)", len(self._cache), allowed is not None)

    def _build_mock_cache(self):
        """Generate check-ins from mock adapter data."""
        from crtv.repositories.patient_history import PatientHistoryRepository
        from crtv.features.adherence import AdherenceCalculator
        from crtv.features.session_summaries import SessionSignalSummarizer
        from crtv.features.learning_rate import LearningRateEstimator

        repo = PatientHistoryRepository(self._adapter)
        adh_calc = AdherenceCalculator()
        summarizer = SessionSignalSummarizer()
        lr_estimator = LearningRateEstimator()

        run_date = datetime(2024, 1, 25)
        window_start = run_date - timedelta(days=28)

        patient_ids = self._adapter.get_patient_ids_in_window(window_start, run_date)
        for pid in patient_ids:
            bundle = repo.load(pid, window_start, run_date)
            if not bundle.sessions:
                continue

            adherence = adh_calc.compute(bundle)
            summaries = summarizer.summarize(bundle)
            learning_rates = lr_estimator.compute(bundle, summaries)

            session_by_id = {s.session_id: s for s in bundle.sessions}
            protocol_wise: dict = {}
            for sess_id, summ in summaries.items():
                sess = session_by_id.get(sess_id)
                if not sess:
                    continue
                proto_id = summ.protocol_id
                if proto_id not in protocol_wise:
                    catalog = bundle.protocol_catalog.get(proto_id)
                    name = catalog.modality if catalog else f"Exercise {proto_id}"
                    protocol_wise[proto_id] = {"name": name, "performance": [], "difficulty": [], "adherence_pct": None}
                dt = sess.start_time.strftime("%Y-%m-%d")
                dm = getattr(summ, "difficulty_mean", {}) or {}
                diff_val = sum(dm.values()) / len(dm) if dm else 0
                protocol_wise[proto_id]["performance"].append({"date": dt, "value": getattr(summ, "performance_mean", 0)})
                protocol_wise[proto_id]["difficulty"].append({"date": dt, "value": diff_val})

            metrics = {
                "patient_id": pid,
                "checkpoint_date": run_date.date().isoformat(),
                "checkpoint_week": 4,
                "protocol_wise": {str(k): v for k, v in protocol_wise.items()},
                "adherence": {
                    "adherence_minutes": adherence.adherence_minutes,
                    "done_total": adherence.done_minutes,
                    "planned_total": adherence.planned_minutes,
                    "days": [{"date": str(k), "planned_min": v[0], "done_min": v[1]} for k, v in adherence.per_day.items()],
                },
                "sessions": [{"session_id": s.session_id, "protocol_id": s.protocol_id, "start_time": s.start_time.isoformat(), "duration_sec": s.duration_sec} for s in bundle.sessions],
                "learning_rates": [{"protocol_id": lr.protocol_id, "learning_rate": lr.learning_rate} for lr in learning_rates.values()],
                "self_reports": [
                    {"key": r.key, "value": r.value, "timestamp": r.timestamp.isoformat()}
                    for r in bundle.self_reports
                ],
            }

            checkin = self.engine.generate(metrics)
            self._cache.append({
                "patient_id": pid,
                "checkpoint_date": run_date.date().isoformat(),
                "checkin": asdict(checkin),
                "metrics_summary": self._summarize_metrics(metrics),
            })

        self._cache.sort(key=lambda x: (x["patient_id"], x["checkpoint_date"]))

    def _summarize_metrics(self, metrics: dict) -> dict:
        """Small summary for the API response (no raw data dump)."""
        adh = metrics.get("adherence", {})
        sessions = metrics.get("sessions", [])
        return {
            "adherence_pct": adh.get("adherence_minutes"),
            "done_minutes": adh.get("done_total", 0),
            "planned_minutes": adh.get("planned_total", 0),
            "session_count": len(sessions),
        }

    def list_checkins(self) -> list[dict]:
        """All check-ins: one entry per (patient, checkpoint)."""
        self._ensure_cache()
        return [
            {
                "patient_id": c["patient_id"],
                "checkpoint_date": c["checkpoint_date"],
                "overall_tone": c["checkin"]["tone"],
                "greeting_preview": (c["checkin"].get("wins") or "")[:80],
            }
            for c in self._cache
        ]

    def get_patient_checkins(self, patient_id: int) -> dict | None:
        """All weekly check-ins for a patient."""
        self._ensure_cache()
        patient_entries = [c for c in self._cache if c["patient_id"] == patient_id]
        if not patient_entries:
            return None
        return {
            "patient_id": patient_id,
            "checkins": [
                self._format_checkin(c)
                for c in sorted(patient_entries, key=lambda x: x["checkpoint_date"])
            ],
        }

    def get_checkin_detail(self, patient_id: int, checkpoint_date: str) -> dict | None:
        """Single check-in for a patient at a specific checkpoint."""
        self._ensure_cache()
        for c in self._cache:
            if c["patient_id"] == patient_id and c["checkpoint_date"] == checkpoint_date:
                return self._format_checkin(c)
        return None

    def _format_checkin(self, c: dict) -> dict:
        ci = c["checkin"]
        return {
            "patient_id": c["patient_id"],
            "checkpoint_date": c["checkpoint_date"],
            "wins": ci.get("wins", []),
            "to_improve": ci.get("to_improve", []),
            "check_in": ci.get("check_in", ""),
            "tone": ci.get("tone", "steady"),
            "progress": ci.get("progress", []),
            "metrics_summary": c["metrics_summary"],
        }
