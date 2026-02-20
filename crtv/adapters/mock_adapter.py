"""MockAdapter - in-memory fixtures from raw tables only."""

import json
from datetime import datetime, date

from pathlib import Path
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
    DataIntegrityEvent,
)

# Default fixtures path
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# Weekday mapping: DB often uses 1=Mon..7=Sun or 0=Mon..6=Sun
_WEEKDAY_MAP = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6}


def _weekday_to_int(val: Any) -> int:
    """Normalize WEEKDAY to 0=Mon..6=Sun."""
    if val is None:
        raise ValueError("WEEKDAY is NULL")
    if isinstance(val, int):
        if 0 <= val <= 6:
            return val
        if 1 <= val <= 7:
            return (val - 1) % 7
    if isinstance(val, str) and val.upper() in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[val.upper()]
    raise ValueError(f"Unsupported WEEKDAY: {val!r}")


class MockAdapter:
    """Mock DB using raw tables from fixtures. Adherence is never loaded."""

    def __init__(self, fixtures_dir: Path | str | None = None):
        self._dir = Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR
        self._session: list[dict] = []
        self._recording: list[dict] = []
        self._prescription: list[dict] = []
        self._difficulty: list[dict] = []
        self._performance: list[dict] = []
        self._self_reports: list[dict] = []
        self._assessments: list[dict] = []
        self._kinematics: list[dict] = []
        self._protocol_catalog: dict[str, dict] = {}
        self._integrity_events: list[DataIntegrityEvent] = []
        self._load_fixtures()

    def _load_fixtures(self) -> None:
        """Load all fixture files."""
        mapping = {
            "session_plus": "_session",
            "recording_plus": "_recording",
            "prescription_plus": "_prescription",
            "difficulty_modulators_plus": "_difficulty",
            "performance_estimators_plus": "_performance",
            "self_reports": "_self_reports",
            "assessments": "_assessments",
            "kinematics": "_kinematics",
        }
        for file_stem, attr in mapping.items():
            path = self._dir / f"{file_stem}.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                setattr(self, attr, data if isinstance(data, list) else [data])
        path = self._dir / "protocol_catalog.json"
        if path.exists():
            self._protocol_catalog = json.loads(path.read_text(encoding="utf-8"))

    def get_sessions(self, patient_id: int, start: datetime, end: datetime) -> list[Session]:
        """Sessions for patient in [start, end). Duration from recording_plus only."""
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        presc_by_id = {p["PRESCRIPTION_ID"]: p for p in self._prescription if p["PATIENT_ID"] == patient_id}
        recording_durations: dict[int, float] = {}
        for r in self._recording:
            if r["RECORDING_KEY"] == "sessionDuration(seconds)" and r["PATIENT_ID"] == patient_id:
                try:
                    recording_durations[r["SESSION_ID"]] = float(r["RECORDING_VALUE"])
                except (ValueError, TypeError):
                    pass

        sessions: list[Session] = []
        for s in self._session:
            pid = s["PRESCRIPTION_ID"]
            if pid not in presc_by_id:
                continue
            presc = presc_by_id[pid]
            if presc["PATIENT_ID"] != patient_id:
                continue
            start_str = s["STARTING_DATE"]
            if isinstance(start_str, str) and "T" in start_str:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(str(start_str))
            sess_date = dt.date()
            if not (start_d <= sess_date < end_d):
                continue

            rec_patient = next((r["PATIENT_ID"] for r in self._recording if r["SESSION_ID"] == s["SESSION_ID"]), None)
            presc_patient = presc["PATIENT_ID"]
            if rec_patient is not None and rec_patient != presc_patient:
                self._integrity_events.append(DataIntegrityEvent(
                    session_id=s["SESSION_ID"],
                    message="Patient ID mismatch",
                    recording_patient_id=rec_patient,
                    prescription_patient_id=presc_patient,
                ))
                continue

            duration = recording_durations.get(s["SESSION_ID"], 0.0)
            sessions.append(Session(
                session_id=s["SESSION_ID"],
                prescription_id=s["PRESCRIPTION_ID"],
                patient_id=patient_id,
                protocol_id=presc["PROTOCOL_ID"],
                start_time=dt,
                duration_sec=duration,
                status=s.get("STATUS", "UNKNOWN"),
                platform=s.get("PLATFORM", ""),
                device=s.get("DEVICE", ""),
                log_parsed=bool(s.get("SESSION_LOG_PARSED", 0)),
            ))
        return sorted(sessions, key=lambda x: x.start_time)

    def get_prescriptions(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[PrescriptionItem]:
        """Prescriptions overlapping [start, end)."""
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        result: list[PrescriptionItem] = []
        for p in self._prescription:
            if p["PATIENT_ID"] != patient_id:
                continue
            p_start = date.fromisoformat(str(p["STARTING_DATE"]).split("T")[0])
            p_end = date.fromisoformat(str(p["ENDING_DATE"]).split("T")[0]) if p.get("ENDING_DATE") else p_start
            if p_end < start_d or p_start >= end_d:
                continue
            try:
                wd = _weekday_to_int(p.get("WEEKDAY", 0))
            except ValueError:
                wd = 0
            result.append(PrescriptionItem(
                prescription_id=p["PRESCRIPTION_ID"],
                patient_id=p["PATIENT_ID"],
                protocol_id=p["PROTOCOL_ID"],
                start_date=p_start,
                end_date=p_end,
                weekday=wd,
                session_duration_min=int(p.get("SESSION_DURATION", 0)),
                ar_mode=p.get("AR_MODE"),
            ))
        return result

    def get_difficulty_rows(self, session_ids: list[int]) -> list[DifficultyRow]:
        """Difficulty modulators for given sessions."""
        sids = set(session_ids)
        rows: list[DifficultyRow] = []
        for d in self._difficulty:
            if d["SESSION_ID"] in sids:
                rows.append(DifficultyRow(
                    session_id=d["SESSION_ID"],
                    patient_id=d["PATIENT_ID"],
                    protocol_id=d["PROTOCOL_ID"],
                    game_mode=d.get("GAME_MODE", "default"),
                    seconds_from_start=int(d.get("SECONDS_FROM_START", 0)),
                    parameter_key=d["PARAMETER_KEY"],
                    parameter_value=str(d["PARAMETER_VALUE"]),
                ))
        return rows

    def get_performance_rows(self, session_ids: list[int]) -> list[PerformanceRow]:
        """Performance estimators for given sessions."""
        sids = set(session_ids)
        rows: list[PerformanceRow] = []
        for p in self._performance:
            if p["SESSION_ID"] in sids:
                rows.append(PerformanceRow(
                    session_id=p["SESSION_ID"],
                    patient_id=p["PATIENT_ID"],
                    protocol_id=p["PROTOCOL_ID"],
                    game_mode=p.get("GAME_MODE", "default"),
                    seconds_from_start=int(p.get("SECONDS_FROM_START", 0)),
                    parameter_key=p["PARAMETER_KEY"],
                    parameter_value=str(p["PARAMETER_VALUE"]),
                ))
        return rows

    def get_kinematics_rows(self, session_ids: list[int]) -> list[KinematicsRow]:
        """Kinematics for given sessions."""
        sids = set(session_ids)
        rows: list[KinematicsRow] = []
        for k in self._kinematics:
            if k["SESSION_ID"] in sids:
                rows.append(KinematicsRow(
                    session_id=k["SESSION_ID"],
                    patient_id=k["PATIENT_ID"],
                    protocol_id=k["PROTOCOL_ID"],
                    seconds_from_start=int(k.get("SECONDS_FROM_START", 0)),
                    metric_key=k["METRIC_KEY"],
                    metric_value=float(k["METRIC_VALUE"]),
                ))
        return rows

    def get_self_reports(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[SelfReport]:
        """Self-reports in window."""
        result: list[SelfReport] = []
        for r in self._self_reports:
            if r["PATIENT_ID"] != patient_id:
                continue
            ts = datetime.fromisoformat(str(r["TIMESTAMP"]).replace("Z", "+00:00"))
            if start <= ts < end:
                result.append(SelfReport(
                    patient_id=r["PATIENT_ID"],
                    key=r["KEY"],
                    value=r["VALUE"],
                    timestamp=ts,
                ))
        return sorted(result, key=lambda x: x.timestamp)

    def get_assessments(
        self,
        patient_id: int,
        start: datetime,
        end: datetime,
        types: list[str] | None = None,
    ) -> list[Assessment]:
        """Assessments in window."""
        result: list[Assessment] = []
        for a in self._assessments:
            if a["PATIENT_ID"] != patient_id:
                continue
            ts = datetime.fromisoformat(str(a["TIMESTAMP"]).replace("Z", "+00:00"))
            if start <= ts < end:
                if types and a.get("TYPE") not in types:
                    continue
                result.append(Assessment(
                    patient_id=a["PATIENT_ID"],
                    type=a.get("TYPE", "unknown"),
                    score=float(a.get("SCORE", 0)),
                    timestamp=ts,
                    subscores=a.get("SUBSCORES"),
                ))
        return sorted(result, key=lambda x: x.timestamp)

    def get_protocol_catalog(self) -> dict[int, ProtocolInfo]:
        """Protocol catalog from fixtures."""
        catalog: dict[int, ProtocolInfo] = {}
        for k, v in self._protocol_catalog.items():
            pid = int(k) if isinstance(k, str) else k
            catalog[pid] = ProtocolInfo(
                protocol_id=pid,
                modality=v.get("modality", ""),
                targets=v.get("targets", []),
                supports_kinematics=v.get("supports_kinematics", False),
                difficulty_modulator_keys=v.get("difficulty_modulator_keys", []),
            )
        return catalog

    def get_patient_ids_in_window(self, start, end) -> list[int]:
        """Patient IDs with prescriptions overlapping window."""
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        ids = set()
        for p in self._prescription:
            p_start = date.fromisoformat(str(p["STARTING_DATE"]).split("T")[0])
            p_end = date.fromisoformat(str(p["ENDING_DATE"]).split("T")[0]) if p.get("ENDING_DATE") else p_start
            if p_end >= start_d and p_start < end_d:
                ids.add(p["PATIENT_ID"])
        return list(ids) if ids else [1]

    def write_checkin_request(self, req) -> int:
        return 0

    def write_checkin_response(self, resp) -> int:
        return 0

    def write_triage_event(self, event) -> int:
        return 0

    def write_recommendation(self, rec) -> int:
        return 0

    def write_pipeline_run(self, run_meta) -> int:
        return 0

    def get_integrity_events(self) -> list[DataIntegrityEvent]:
        """Return any DataIntegrityEvents from session validation."""
        return list(self._integrity_events)
