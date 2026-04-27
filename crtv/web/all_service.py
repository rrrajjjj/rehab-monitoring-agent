"""AllPatientsService — on-demand triage card generation for any patient + week.

Unlike the curated demo store (`demo_mining/card_store/`), the /all store
persists cards produced interactively on the `/all` page. It is:
  - global (no per-user separation)
  - backend-agnostic (CSV or MySQL, via get_adapter)
  - stored separately so it doesn't mix with the frozen demo cohort
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from crtv.adapters import get_adapter
from crtv.adapters.database import DatabaseAdapter
from crtv.pipeline.historical_runner import HistoricalTriageRunner
from crtv.reasoning.patient_checkin import PatientCheckInEngine
from crtv.web.historical_service import HistoricalTriageService
from demo_mining.card_store import CardStore

logger = logging.getLogger("crtv.web.all_service")


class AllPatientsService:
    """Generate-on-demand triage cards, persisted to a dedicated CardStore."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        use_medgemma: bool = False,
        card_store_path: str | Path = "card_store_all",
    ):
        self.data_dir = Path(data_dir)
        self.use_medgemma = use_medgemma
        self.card_store_path = Path(card_store_path)
        self._runner: HistoricalTriageRunner | None = None
        self._checkin: PatientCheckInEngine | None = None
        self.store = CardStore(self.card_store_path)
        # Detail rendering: reuse HistoricalTriageService but point it at our
        # store and bypass the demo-selection filter.
        self._detail_service = HistoricalTriageService(
            data_dir=self.data_dir,
            use_medgemma=use_medgemma,
            card_store_path=self.card_store_path,
            demo_selection_path=Path("__missing__"),
        )

    @property
    def adapter(self) -> DatabaseAdapter:
        return self._detail_service.adapter

    # --- patient lookup --------------------------------------------------

    def search_patients(self, query: str) -> list[dict]:
        return self.adapter.resolve_patient(query)

    def patient_weeks(self, patient_id: int) -> list[str]:
        return [d.isoformat() for d in self.adapter.patient_active_weeks(patient_id)]

    # --- card CRUD -------------------------------------------------------

    def _ensure_runner(self) -> HistoricalTriageRunner:
        if self._runner is None:
            self._runner = HistoricalTriageRunner(
                str(self.data_dir), use_medgemma=self.use_medgemma
            )
        return self._runner

    def _ensure_checkin(self) -> PatientCheckInEngine:
        if self._checkin is None:
            self._checkin = PatientCheckInEngine(use_medgemma=self.use_medgemma)
        return self._checkin

    def generate(self, patient_id: int, checkpoint_date: str) -> dict | None:
        """
        Build a fresh triage card + patient check-in for (patient, week-ending-date).
        Persists to the /all card store and returns the list-summary shape.
        """
        try:
            cp = date.fromisoformat(checkpoint_date[:10])
        except ValueError:
            return None

        runner = self._ensure_runner()
        entry = runner.run_single_checkpoint(patient_id, cp)
        if entry is None:
            return None

        try:
            checkin = self._ensure_checkin().generate(entry["metrics"])
            entry["checkin"] = asdict(checkin)
        except Exception as e:  # pragma: no cover — surfaces in UI as warning
            logger.warning("checkin failed pid=%s cp=%s: %s", patient_id, cp, e)
            entry["checkin"] = None

        self.store.save(entry)
        # invalidate detail-service cache so the newly written card is visible
        self._detail_service._cache_done = False
        return {
            "patient_id": entry["patient_id"],
            "checkpoint_date": entry["checkpoint_date"],
            "headline": entry["card"].headline if entry.get("card") else "",
            "disposition": entry["disposition"],
            "diagnosis": entry.get("diagnosis"),
            "created_at": datetime.utcnow().isoformat(),
        }

    def list_cards(self) -> list[dict]:
        self._detail_service._cache_done = False
        return self._detail_service.list_triage_cards()

    def get_card_detail(self, patient_id: int, checkpoint_date: str) -> dict | None:
        self._detail_service._cache_done = False
        return self._detail_service.get_card_detail(patient_id, checkpoint_date)

    def delete_card(self, patient_id: int, checkpoint_date: str) -> bool:
        fp = self.card_store_path / f"p{int(patient_id)}_w{checkpoint_date[:10]}.json"
        if not fp.exists():
            return False
        fp.unlink()
        self._detail_service._cache_done = False
        return True
