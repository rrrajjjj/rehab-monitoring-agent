"""Database adapter interface."""

from abc import ABC, abstractmethod
from datetime import datetime
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
    """Swappable DB adapter; implement MockAdapter or PostgresAdapter."""

    @abstractmethod
    def get_sessions(self, patient_id: int, start: datetime, end: datetime) -> list[Session]:
        """Fetch sessions for patient in [start, end)."""
        ...

    @abstractmethod
    def get_prescriptions(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[PrescriptionItem]:
        """Fetch prescriptions overlapping [start, end)."""
        ...

    @abstractmethod
    def get_difficulty_rows(self, session_ids: list[int]) -> list[DifficultyRow]:
        """Fetch difficulty_modulators_plus rows for sessions."""
        ...

    @abstractmethod
    def get_performance_rows(self, session_ids: list[int]) -> list[PerformanceRow]:
        """Fetch performance_estimators_plus rows for sessions."""
        ...

    @abstractmethod
    def get_kinematics_rows(self, session_ids: list[int]) -> list[KinematicsRow]:
        """Fetch kinematics rows for sessions (AR protocols)."""
        ...

    @abstractmethod
    def get_self_reports(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[SelfReport]:
        """Fetch self-reports (emotional_answer etc.) in window."""
        ...

    @abstractmethod
    def get_assessments(
        self,
        patient_id: int,
        start: datetime,
        end: datetime,
        types: list[str] | None = None,
    ) -> list[Assessment]:
        """Fetch assessments (clinical_data) in window."""
        ...

    @abstractmethod
    def get_protocol_catalog(self) -> dict[int, ProtocolInfo]:
        """Fetch protocol catalog."""
        ...

    def write_checkin_request(self, req: Any) -> int:
        """Persist check-in request. Returns id."""
        return 0

    def write_checkin_response(self, resp: Any) -> int:
        """Persist check-in response. Returns id."""
        return 0

    def write_triage_event(self, event: Any) -> int:
        """Persist triage event. Returns id."""
        return 0

    def write_recommendation(self, rec: Any) -> int:
        """Persist recommendation. Returns id."""
        return 0

    def write_pipeline_run(self, run_meta: Any) -> int:
        """Persist pipeline run. Returns id."""
        return 0

    def get_patient_ids_in_window(self, start: datetime, end: datetime) -> list[int]:
        """Return patient IDs with prescriptions overlapping [start, end)."""
        return []
