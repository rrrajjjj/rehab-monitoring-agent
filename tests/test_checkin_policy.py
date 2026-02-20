"""Unit tests for CheckInPolicy."""

from datetime import datetime, timedelta

from crtv.domain.models import DriftEvent
from crtv.checkin.policy import CheckInPolicy


def test_checkin_cooldown():
    """Check-in cooldown logic."""
    policy = CheckInPolicy(cooldown_hours=72)
    events = [
        DriftEvent(type="ADHERENCE_DRIFT", severity=1, confidence=0.8, window_start=datetime.now(), window_end=datetime.now(), evidence={}, session_ids=[]),
    ]
    req1 = policy.decide(events, [])
    assert req1 is not None
    recent = [{"created_at": datetime.now() - timedelta(hours=24)}]
    req2 = policy.decide(events, recent)
    assert req2 is None
