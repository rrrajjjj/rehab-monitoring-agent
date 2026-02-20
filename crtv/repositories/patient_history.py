"""PatientHistoryRepository - loads all data for a patient in a time window."""

from datetime import datetime

from crtv.adapters.database import DatabaseAdapter
from crtv.domain.models import PatientHistoryBundle


class PatientHistoryRepository:
    """One call per patient per window; adapter-agnostic."""

    def __init__(self, adapter: DatabaseAdapter):
        self._adapter = adapter

    def load(
        self, patient_id: int, start: datetime, end: datetime
    ) -> PatientHistoryBundle:
        """
        Load bundle: sessions, prescriptions, timeseries rows,
        self_reports, assessments, protocol catalog.
        """
        sessions = self._adapter.get_sessions(patient_id, start, end)
        prescriptions = self._adapter.get_prescriptions(patient_id, start, end)
        session_ids = [s.session_id for s in sessions]
        difficulty_rows = self._adapter.get_difficulty_rows(session_ids)
        performance_rows = self._adapter.get_performance_rows(session_ids)
        kinematics_rows = self._adapter.get_kinematics_rows(session_ids)
        self_reports = self._adapter.get_self_reports(patient_id, start, end)
        assessments = self._adapter.get_assessments(patient_id, start, end)
        protocol_catalog = self._adapter.get_protocol_catalog()
        return PatientHistoryBundle(
            patient_id=patient_id,
            start=start,
            end=end,
            sessions=sessions,
            prescriptions=prescriptions,
            difficulty_rows=difficulty_rows,
            performance_rows=performance_rows,
            kinematics_rows=kinematics_rows,
            self_reports=self_reports,
            assessments=assessments,
            protocol_catalog=protocol_catalog,
        )
