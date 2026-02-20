"""Unit tests for AdherenceCalculator."""

from datetime import datetime, date
from crtv.domain.models import (
    PatientHistoryBundle,
    Session,
    PrescriptionItem,
)
from crtv.features.adherence import (
    AdherenceCalculator,
    expand_prescriptions,
    match_sessions_to_occurrences,
)


def test_expand_prescriptions():
    """Prescription expansion across date ranges and weekdays."""
    presc = PrescriptionItem(
        prescription_id=1,
        patient_id=1,
        protocol_id=101,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        weekday=0,  # Monday
        session_duration_min=25,
        ar_mode=None,
    )
    occs = expand_prescriptions([presc], date(2024, 1, 1), date(2024, 1, 15))
    mondays = [d for d, _, _ in occs if d.weekday() == 0]
    assert len(mondays) >= 2  # Jan 1, 8 are Mondays


def test_session_to_occurrence_matching():
    """Session-to-occurrence matching."""
    sessions = [
        Session(session_id=1, prescription_id=1, patient_id=1, protocol_id=101, start_time=datetime(2024, 1, 8, 10, 0),
                duration_sec=600, status="CLOSED", platform="MOBILE", device="", log_parsed=True),
    ]
    occurrences = [(date(2024, 1, 8), 101, 25)]
    matched = match_sessions_to_occurrences(sessions, occurrences)
    assert (date(2024, 1, 8), 101) in matched
    assert 1 in matched[(date(2024, 1, 8), 101)]
