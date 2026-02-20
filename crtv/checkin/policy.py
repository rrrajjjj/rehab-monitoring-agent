"""CheckInPolicy - decide when to request patient/caregiver check-in."""

from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from crtv.domain.models import DriftEvent


@dataclass
class CheckInRequest:
    """Request for patient/caregiver check-in."""

    patient_id: int
    target: str  # PATIENT | CAREGIVER
    modality: str  # TEXT | VOICE
    question_set_id: str
    expiry: datetime
    drift_types: list[str] = None

    def __post_init__(self):
        if self.drift_types is None:
            self.drift_types = []


TRIGGER_EVENT_TYPES = {"ADHERENCE_DRIFT", "OVERCHALLENGE", "REGRESSION", "PLATEAU"}


class CheckInPolicy:
    """Trigger check-in only for actionable events; cooldown per patient."""

    def __init__(self, cooldown_hours: int = 72):
        self.cooldown_hours = cooldown_hours

    def decide(
        self,
        drift_events: list[DriftEvent],
        recent_checkin_history: list[dict],
    ) -> Optional[CheckInRequest]:
        """
        Trigger for: adherence drift, overchallenge, regression, pain/mood concerns.
        Cooldown window (e.g. 72h) per patient.
        """
        if not drift_events:
            return None
        actionable = [e for e in drift_events if e.type in TRIGGER_EVENT_TYPES]
        if not actionable:
            return None
        now = datetime.now()
        cutoff = now - timedelta(hours=self.cooldown_hours)
        for h in recent_checkin_history:
            ts = h.get("created_at") or h.get("timestamp")
            if ts and ts > cutoff:
                return None  # within cooldown
        return CheckInRequest(
            patient_id=0,  # caller fills
            target="PATIENT",
            modality="TEXT",
            question_set_id="default",
            expiry=now + timedelta(days=7),
            drift_types=[e.type for e in actionable],
        )
