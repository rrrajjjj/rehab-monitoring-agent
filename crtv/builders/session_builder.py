"""Build Session from session_plus + recording_plus. Duration from recording only."""

from datetime import datetime
from typing import Any

from crtv.domain.models import Session


def build_sessions(
    session_rows: list[dict[str, Any]],
    recording_rows: list[dict[str, Any]],
    prescription_by_id: dict[int, dict[str, Any]],
) -> list[Session]:
    """
    Build Session list from raw session_plus and recording_plus.
    Duration ONLY from recording_plus where RECORDING_KEY='sessionDuration(seconds)'.
    Never use ENDING_DATE - STARTING_DATE.
    """
    durations: dict[int, float] = {}
    for r in recording_rows:
        if r.get("RECORDING_KEY") == "sessionDuration(seconds)":
            try:
                durations[int(r["SESSION_ID"])] = float(r["RECORDING_VALUE"])
            except (ValueError, TypeError, KeyError):
                pass

    sessions: list[Session] = []
    for s in session_rows:
        pid = s.get("PRESCRIPTION_ID")
        if pid not in prescription_by_id:
            continue
        presc = prescription_by_id[pid]
        patient_id = presc["PATIENT_ID"]
        protocol_id = presc["PROTOCOL_ID"]

        start_str = s.get("STARTING_DATE")
        if isinstance(start_str, str):
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(str(start_str))

        duration = durations.get(int(s["SESSION_ID"]), 0.0)
        sessions.append(Session(
            session_id=int(s["SESSION_ID"]),
            prescription_id=int(s["PRESCRIPTION_ID"]),
            patient_id=patient_id,
            protocol_id=protocol_id,
            start_time=dt,
            duration_sec=duration,
            status=str(s.get("STATUS", "UNKNOWN")),
            platform=str(s.get("PLATFORM", "")),
            device=str(s.get("DEVICE", "")),
            log_parsed=bool(s.get("SESSION_LOG_PARSED", 0)),
        ))
    return sorted(sessions, key=lambda x: x.start_time)
