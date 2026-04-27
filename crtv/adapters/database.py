"""Data adapter interface.

Implementations live alongside this file:
  - CSVDataAdapter   (csv_adapter.py)   — NEST CSV dump
  - MySQLDataAdapter (mysql_adapter.py) — live MySQL DB
  - MockAdapter      (mock_adapter.py)  — fixture JSON

All methods that return domain rows (sessions, prescriptions, self-reports, …)
return the same Pydantic models regardless of backend so the repository +
feature builders are backend-agnostic.
"""

from abc import ABC, abstractmethod
from datetime import datetime, date
from typing import Any

from crtv.domain.models import (
    Session,
    PrescriptionItem,
    SelfReport,
    Assessment,
    ProtocolInfo,
    DifficultyRow,
    PerformanceRow,
    KinematicsRow,
)


class DatabaseAdapter(ABC):
    """Swappable data-access adapter."""

    # --- domain reads ----------------------------------------------------

    @abstractmethod
    def get_sessions(self, patient_id: int, start: datetime, end: datetime) -> list[Session]:
        """Sessions for patient in [start, end)."""

    @abstractmethod
    def get_prescriptions(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[PrescriptionItem]:
        """Prescriptions overlapping [start, end)."""

    @abstractmethod
    def get_difficulty_rows(self, session_ids: list[int]) -> list[DifficultyRow]:
        """difficulty_modulators_plus rows for the given sessions."""

    @abstractmethod
    def get_performance_rows(self, session_ids: list[int]) -> list[PerformanceRow]:
        """performance_estimators_plus rows for the given sessions."""

    @abstractmethod
    def get_kinematics_rows(self, session_ids: list[int]) -> list[KinematicsRow]:
        """Kinematics rows (AR protocols). May return []."""

    @abstractmethod
    def get_self_reports(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[SelfReport]:
        """Self-reports from emotional_answer (joined with emotional_question)."""

    @abstractmethod
    def get_assessments(
        self,
        patient_id: int,
        start: datetime,
        end: datetime,
        types: list[str] | None = None,
    ) -> list[Assessment]:
        """Assessments (clinical data) in window. May return []."""

    @abstractmethod
    def get_protocol_catalog(self) -> dict[int, ProtocolInfo]:
        """Full protocol catalog."""

    @abstractmethod
    def get_protocol_name(self, protocol_id: int) -> str:
        """Display name for a protocol id. Fallback to 'Protocol {id}'."""

    # --- lookups ---------------------------------------------------------

    def get_patient_ids_in_window(self, start: datetime, end: datetime) -> list[int]:
        """Patient ids with a prescription overlapping [start, end). Default: []."""
        return []

    def list_patient_ids(self) -> list[int]:
        """All patient ids known to the backend. Default: []."""
        return []

    def resolve_patient(self, query: str) -> list[dict]:
        """
        Resolve a free-text query to candidate patients.
        Numeric → match PATIENT_ID; otherwise → match PATIENT_USER (substring, case-insensitive).
        Each result: {'patient_id': int, 'patient_user': str}.
        Default implementation: numeric match against list_patient_ids().
        """
        q = (query or "").strip()
        if not q:
            return []
        if q.isdigit():
            pid = int(q)
            return [{"patient_id": pid, "patient_user": ""}] if pid in set(self.list_patient_ids()) else []
        return []

    def patient_active_weeks(self, patient_id: int) -> list[date]:
        """
        Week-ending dates (Sundays) for weeks in which the patient has ≥1 session.
        Default: derive from get_sessions over a very wide window.
        """
        from datetime import timedelta
        sessions = self.get_sessions(patient_id, datetime(2020, 1, 1), datetime(2035, 1, 1))
        weeks: set[date] = set()
        for s in sessions:
            d = s.start_time.date() if hasattr(s.start_time, "date") else s.start_time
            # week ending on Sunday
            week_end = d + timedelta(days=(6 - d.weekday()))
            weeks.add(week_end)
        return sorted(weeks)

    # --- clinical scores (CSV-only; MySQL stubs to []) --------------------

    def get_clinical_scores_regressors(self) -> list[int]:
        return []

    def get_regressor_with_largest_delta(self) -> int | None:
        return None

    def get_patient_fm_scores(self, patient_id: int) -> tuple[float, float] | None:
        return None

    # --- writes (currently no-ops everywhere) ----------------------------

    def write_checkin_request(self, req: Any) -> int:
        return 0

    def write_checkin_response(self, resp: Any) -> int:
        return 0

    def write_triage_event(self, event: Any) -> int:
        return 0

    def write_recommendation(self, rec: Any) -> int:
        return 0

    def write_pipeline_run(self, run_meta: Any) -> int:
        return 0

    # --- integrity events ------------------------------------------------

    def get_integrity_events(self):
        return []
