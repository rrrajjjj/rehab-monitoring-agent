"""
CSVDataAdapter - loads NEST tables from data/, merges app + plus.
App and plus have same structure; data distributed across both.
"""

import csv
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

_WEEKDAY_MAP = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6}


def _weekday_to_int(val: Any) -> int:
    if val is None:
        raise ValueError("WEEKDAY is NULL")
    if isinstance(val, int) and 0 <= val <= 6:
        return val
    if isinstance(val, int) and 1 <= val <= 7:
        return (val - 1) % 7
    if isinstance(val, str) and val.upper().strip() in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[val.upper().strip()]
    raise ValueError(f"Unsupported WEEKDAY: {val!r}")


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append({k.strip(): v for k, v in row.items() if k})
    return rows


def _merge_tables(app_path: Path, plus_path: Path) -> list[dict]:
    """Load and merge app + plus; prefer plus columns if both have same row."""
    app = _load_csv(app_path)
    plus = _load_csv(plus_path)
    if not app and not plus:
        return []
    if not app:
        return plus
    if not plus:
        return app
    all_rows = app + plus
    return all_rows


class CSVDataAdapter:
    """Load NEST data from data/; merge app and plus tables."""

    def __init__(self, data_dir: Path | str):
        self._dir = Path(data_dir)
        self._session: list[dict] = []
        self._recording: list[dict] = []
        self._prescription: list[dict] = []
        self._difficulty: list[dict] = []
        self._performance: list[dict] = []
        self._self_reports: list[dict] = []
        self._clinical_scores: list[dict] = []
        self._integrity_events: list[DataIntegrityEvent] = []
        self._session_duration_seconds = True  # NEST prescription SESSION_DURATION in seconds
        self._load_all()

    def _load_all(self) -> None:
        self._session = _merge_tables(
            self._dir / "NEST_session_app.csv",
            self._dir / "NEST_session_plus.csv",
        )
        self._recording = _merge_tables(
            self._dir / "NEST_recording_app.csv",
            self._dir / "NEST_recording_plus.csv",
        )
        self._prescription = _merge_tables(
            self._dir / "NEST_prescription_app.csv",
            self._dir / "NEST_prescription_plus.csv",
        )
        self._difficulty = _merge_tables(
            self._dir / "NEST_dm_app.csv",
            self._dir / "NEST_dm_plus.csv",
        )
        self._performance = _merge_tables(
            self._dir / "NEST_performance_estimators_app.csv",
            self._dir / "NEST_performance_estimators_plus.csv",
        )
        self._self_reports = _load_csv(self._dir / "nest_self_reports.csv")
        self._clinical_scores = _load_csv(self._dir / "NEST_clinical_scores.csv")
        self._protocols: dict[int, str] = {}
        for r in _load_csv(self._dir / "protocol.csv"):
            try:
                pid = int(float(r.get("PROTOCOL_ID", 0) or 0))
                name = (r.get("NAME_KEY") or r.get("name_key") or "").strip()
                if pid and name:
                    self._protocols[pid] = name
            except (ValueError, TypeError):
                pass

        for row in self._session:
            if "PLATFORM" not in row:
                row["PLATFORM"] = ""
            if "DEVICE" not in row:
                row["DEVICE"] = ""

    def get_clinical_scores_regressors(self) -> list[int]:
        """Patient IDs where FM_EoT < FM_BL (score dropped during trial)."""
        regressors = []
        for r in self._clinical_scores:
            try:
                pid = r.get("PATIENT_ID")
                if not pid or str(pid).strip() == "":
                    continue
                pid = int(float(pid))
                bl = float(r.get("FM_BL", 0) or 0)
                eot = float(r.get("FM_EoT", 0) or 0)
                if eot < bl:
                    regressors.append(pid)
            except (ValueError, TypeError):
                continue
        return regressors

    def get_regressor_with_largest_delta(self) -> int | None:
        """Patient ID with largest FM decline (FM_BL - FM_EoT). For trial/demo: single exemplar."""
        best_pid: int | None = None
        best_delta = 0.0
        for r in self._clinical_scores:
            try:
                pid = r.get("PATIENT_ID")
                if not pid or str(pid).strip() == "":
                    continue
                pid = int(float(pid))
                bl = float(r.get("FM_BL", 0) or 0)
                eot = float(r.get("FM_EoT", 0) or 0)
                delta = bl - eot
                if delta > 0 and delta > best_delta:
                    best_delta = delta
                    best_pid = pid
            except (ValueError, TypeError):
                continue
        return best_pid

    def get_protocol_name(self, protocol_id: int) -> str:
        """Protocol display name from protocol.csv."""
        return self._protocols.get(protocol_id, "") or f"Protocol {protocol_id}"

    def get_patient_fm_scores(self, patient_id: int) -> tuple[float, float] | None:
        """(FM_BL, FM_EoT) for patient, or None if not found."""
        for r in self._clinical_scores:
            if int(float(r.get("PATIENT_ID", 0) or 0)) != patient_id:
                continue
            try:
                bl = float(r.get("FM_BL", 0) or 0)
                eot = float(r.get("FM_EoT", 0) or 0)
                return (bl, eot)
            except (ValueError, TypeError):
                pass
        return None

    def get_patient_ids_in_window(self, start, end) -> list[int]:
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        ids = set()
        for p in self._prescription:
            try:
                pid = int(float(p.get("PATIENT_ID", 0) or 0))
                s = p.get("STARTING_DATE") or p.get("start_date")
                e = p.get("ENDING_DATE") or p.get("end_date")
                if not s or not e:
                    continue
                if isinstance(s, str):
                    s = datetime.fromisoformat(s.replace("Z", "+00:00").split(".")[0])
                if isinstance(e, str):
                    e = datetime.fromisoformat(e.replace("Z", "+00:00").split(".")[0])
                p_start = s.date() if hasattr(s, "date") else s
                p_end = e.date() if hasattr(e, "date") else min(e.date(), date(2100, 1, 1))
                if p_end >= start_d and p_start < end_d:
                    ids.add(pid)
            except (ValueError, TypeError):
                continue
        return list(ids) if ids else []

    def get_sessions(self, patient_id: int, start: datetime, end: datetime) -> list[Session]:
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        presc_by_id = {int(float(p.get("PRESCRIPTION_ID") or 0)): p for p in self._prescription
                       if int(float(p.get("PATIENT_ID") or 0)) == patient_id}
        recording_durations = {}
        for r in self._recording:
            if r.get("RECORDING_KEY") == "sessionDuration(seconds)" and int(float(r.get("PATIENT_ID") or 0)) == patient_id:
                try:
                    recording_durations[int(r["SESSION_ID"])] = float(r["RECORDING_VALUE"])
                except (ValueError, TypeError, KeyError):
                    pass

        sessions = []
        for s in self._session:
            if int(float(s.get("PATIENT_ID") or 0)) != patient_id:
                continue
            pid = int(float(s.get("PRESCRIPTION_ID") or 0))
            if pid not in presc_by_id:
                continue
            presc = presc_by_id[pid]
            start_str = s.get("STARTING_DATE") or s.get("start_date")
            if not start_str:
                continue
            dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00").split(".")[0])
            if not (start_d <= dt.date() < end_d):
                continue
            duration = recording_durations.get(int(s["SESSION_ID"]), 0.0)
            try:
                wd = _weekday_to_int(presc.get("WEEKDAY"))
            except ValueError:
                wd = 0
            sd = presc.get("SESSION_DURATION") or 30
            try:
                sd_val = int(float(sd))
            except (ValueError, TypeError):
                sd_val = 30
            session_duration_min = max(1, sd_val // 60) if sd_val > 120 else max(1, sd_val)
            sessions.append(Session(
                session_id=int(s["SESSION_ID"]),
                prescription_id=pid,
                patient_id=patient_id,
                protocol_id=int(float(presc.get("PROTOCOL_ID") or 0)),
                start_time=dt,
                duration_sec=duration,
                status=str(s.get("STATUS", "UNKNOWN")),
                platform=str(s.get("PLATFORM", "")),
                device=str(s.get("DEVICE", "")),
                log_parsed=bool(int(s.get("SESSION_LOG_PARSED", 0) or 0)),
            ))
        return sorted(sessions, key=lambda x: x.start_time)

    def get_prescriptions(self, patient_id: int, start: datetime, end: datetime) -> list[PrescriptionItem]:
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        result = []
        for p in self._prescription:
            if int(float(p.get("PATIENT_ID") or 0)) != patient_id:
                continue
            try:
                s = p.get("STARTING_DATE") or p.get("start_date")
                e = p.get("ENDING_DATE") or p.get("end_date")
                p_start = datetime.fromisoformat(str(s).split(".")[0].replace("Z", "")).date()
                p_end = datetime.fromisoformat(str(e).split(".")[0].replace("Z", "")).date()
                if p_end > date(2099, 1, 1):
                    p_end = end_d
                if p_end < start_d or p_start >= end_d:
                    continue
                wd = _weekday_to_int(p.get("WEEKDAY"))
                sd = int(float(p.get("SESSION_DURATION") or 30))
                session_duration_min = max(1, sd // 60) if sd > 120 else max(1, sd)
                result.append(PrescriptionItem(
                    prescription_id=int(float(p.get("PRESCRIPTION_ID") or 0)),
                    patient_id=patient_id,
                    protocol_id=int(float(p.get("PROTOCOL_ID") or 0)),
                    start_date=p_start,
                    end_date=p_end,
                    weekday=wd,
                    session_duration_min=session_duration_min,
                    ar_mode=p.get("AR_MODE"),
                ))
            except (ValueError, TypeError):
                continue
        return result

    def get_difficulty_rows(self, session_ids: list[int]) -> list[DifficultyRow]:
        sids = set(session_ids)
        rows = []
        for d in self._difficulty:
            if int(d.get("SESSION_ID") or 0) not in sids:
                continue
            try:
                rows.append(DifficultyRow(
                    session_id=int(d["SESSION_ID"]),
                    patient_id=int(float(d.get("PATIENT_ID") or 0)),
                    protocol_id=int(float(d.get("PROTOCOL_ID") or 0)),
                    game_mode=str(d.get("GAME_MODE", "default")),
                    seconds_from_start=int(float(d.get("SECONDS_FROM_START") or 0)),
                    parameter_key=str(d.get("PARAMETER_KEY", "")),
                    parameter_value=str(d.get("PARAMETER_VALUE", "")),
                ))
            except (ValueError, TypeError, KeyError):
                continue
        return rows

    def get_performance_rows(self, session_ids: list[int]) -> list[PerformanceRow]:
        sids = set(session_ids)
        rows = []
        for p in self._performance:
            if int(p.get("SESSION_ID") or 0) not in sids:
                continue
            try:
                rows.append(PerformanceRow(
                    session_id=int(p["SESSION_ID"]),
                    patient_id=int(float(p.get("PATIENT_ID") or 0)),
                    protocol_id=int(float(p.get("PROTOCOL_ID") or 0)),
                    game_mode=str(p.get("GAME_MODE", "default")),
                    seconds_from_start=int(float(p.get("SECONDS_FROM_START") or 0)),
                    parameter_key=str(p.get("PARAMETER_KEY", "")),
                    parameter_value=str(p.get("PARAMETER_VALUE", "")),
                ))
            except (ValueError, TypeError, KeyError):
                continue
        return rows

    def get_kinematics_rows(self, session_ids: list[int]) -> list[KinematicsRow]:
        return []

    def get_self_reports(self, patient_id: int, start: datetime, end: datetime) -> list[SelfReport]:
        result = []
        for r in self._self_reports:
            if int(float(r.get("PATIENT_ID") or 0)) != patient_id:
                continue
            ts = datetime.fromisoformat(str(r.get("CREATION_TIME", "")).split(".")[0].replace("Z", ""))
            if start <= ts < end:
                result.append(SelfReport(
                    patient_id=patient_id,
                    key=str(r.get("EMOTIONAL_QUESTION_KEY", "")),
                    value=r.get("EMOTIONAL_ANSWER", ""),
                    timestamp=ts,
                ))
        return sorted(result, key=lambda x: x.timestamp)

    def get_assessments(self, patient_id: int, start: datetime, end: datetime, types: list[str] | None = None) -> list[Assessment]:
        result = []
        for c in self._clinical_scores:
            if int(float(c.get("PATIENT_ID") or 0)) != patient_id:
                continue
            bl = float(c.get("FM_BL") or 0)
            eot = float(c.get("FM_EoT") or 0)
            result.append(Assessment(
                patient_id=patient_id,
                type="Fugl-Meyer",
                score=bl,
                timestamp=start,
                subscores={"EoT": eot} if eot else None,
            ))
        return result

    def get_protocol_catalog(self) -> dict[int, ProtocolInfo]:
        pids = set()
        for p in self._prescription:
            try:
                pids.add(int(float(p.get("PROTOCOL_ID") or 0)))
            except (ValueError, TypeError):
                pass
        return {pid: ProtocolInfo(protocol_id=pid, modality="", targets=[], supports_kinematics=False, difficulty_modulator_keys=[])
                for pid in pids if pid > 0}

    def get_integrity_events(self) -> list[DataIntegrityEvent]:
        return list(self._integrity_events)

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
