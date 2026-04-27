"""MySQLDataAdapter — live NEST database backend.

Reads the same domain models as CSVDataAdapter but from the production MySQL DB.
Connection config via env: DB_USER, DB_PASS, DB_HOST, DB_PORT (default 3306), DB_NAME.

Schema assumptions (derived from the companion monitoring_system project):
  - session_plus          : SESSION_ID, STARTING_DATE, STATUS, PRESCRIPTION_ID
                            (no PATIENT_ID — routed via prescription)
  - prescription_plus     : PRESCRIPTION_ID, PATIENT_ID, PROTOCOL_ID, STARTING_DATE,
                            ENDING_DATE, WEEKDAY, SESSION_DURATION
  - recording_plus        : SESSION_ID, PATIENT_ID, RECORDING_KEY, RECORDING_VALUE
                            ('sessionDuration(seconds)' is the per-session total)
  - difficulty_modulators_plus : SESSION_ID, PATIENT_ID, PROTOCOL_ID, GAME_MODE,
                            SECONDS_FROM_START, PARAMETER_KEY, PARAMETER_VALUE
  - performance_estimators_plus : same shape as above
  - emotional_question          : EMOTIONAL_QUESTION_ID, EMOTIONAL_QUESTION_KEY
  - emotional_question_patient  : EMOTIONAL_QUESTION_PATIENT_ID, PATIENT_ID, EMOTIONAL_QUESTION_ID
  - emotional_answer            : EMOTIONAL_QUESTION_PATIENT_ID, EMOTIONAL_ANSWER, CREATION_TIME
  - protocol                    : PROTOCOL_ID, NAME_KEY
  - patient                     : PATIENT_ID, PATIENT_USER
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

from crtv.adapters.database import DatabaseAdapter
from crtv.domain.models import (
    Assessment,
    DifficultyRow,
    KinematicsRow,
    PerformanceRow,
    PrescriptionItem,
    ProtocolInfo,
    SelfReport,
    Session,
)

logger = logging.getLogger("crtv.adapters.mysql")

_WEEKDAY_MAP = {
    "MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3,
    "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6,
}


def _weekday_to_int(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val % 7 if 0 <= val <= 6 else (val - 1) % 7
    s = str(val).upper().strip()
    if s in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[s]
    try:
        return int(float(s)) % 7
    except ValueError:
        return 0


def _to_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time())
    try:
        return datetime.fromisoformat(str(val).replace("Z", "").split(".")[0])
    except (ValueError, TypeError):
        return None


class MySQLDataAdapter(DatabaseAdapter):
    """Live MySQL-backed adapter."""

    def __init__(self, engine=None):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL

        if engine is None:
            url = URL.create(
                drivername="mysql+pymysql",
                username=os.environ["DB_USER"],
                password=os.environ["DB_PASS"],
                host=os.environ["DB_HOST"],
                port=int(os.environ.get("DB_PORT", "3306")),
                database=os.environ["DB_NAME"],
                query={"charset": "utf8mb4"},
            )
            engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)
        self._engine = engine
        self._protocol_cache: dict[int, str] | None = None

    # --- helpers ---------------------------------------------------------

    def _rows(self, sql: str, **params) -> list[dict]:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            result = conn.execute(text(sql), params)
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]

    # --- domain reads ----------------------------------------------------

    def get_sessions(self, patient_id: int, start: datetime, end: datetime) -> list[Session]:
        sql = """
        SELECT
          s.SESSION_ID                AS SESSION_ID,
          s.PRESCRIPTION_ID           AS PRESCRIPTION_ID,
          s.STARTING_DATE             AS STARTING_DATE,
          s.STATUS                    AS STATUS,
          p.PATIENT_ID                AS PATIENT_ID,
          p.PROTOCOL_ID               AS PROTOCOL_ID,
          (
            SELECT COALESCE(SUM(CAST(r.RECORDING_VALUE AS DOUBLE)), 0)
            FROM recording_plus r
            WHERE r.SESSION_ID = s.SESSION_ID
              AND r.RECORDING_KEY = 'sessionDuration(seconds)'
          ) AS DURATION_SEC
        FROM session_plus s
        JOIN prescription_plus p ON p.PRESCRIPTION_ID = s.PRESCRIPTION_ID
        WHERE p.PATIENT_ID = :pid
          AND s.STARTING_DATE >= :start_dt
          AND s.STARTING_DATE <  :end_dt
        ORDER BY s.STARTING_DATE ASC
        """
        rows = self._rows(sql, pid=patient_id, start_dt=start, end_dt=end)
        out: list[Session] = []
        for r in rows:
            dt = _to_datetime(r.get("STARTING_DATE"))
            if dt is None:
                continue
            try:
                out.append(Session(
                    session_id=int(r["SESSION_ID"]),
                    prescription_id=int(r.get("PRESCRIPTION_ID") or 0),
                    patient_id=patient_id,
                    protocol_id=int(r.get("PROTOCOL_ID") or 0),
                    start_time=dt,
                    duration_sec=float(r.get("DURATION_SEC") or 0.0),
                    status=str(r.get("STATUS") or "UNKNOWN"),
                    platform="",
                    device="",
                    log_parsed=False,
                ))
            except (ValueError, TypeError):
                continue
        return out

    def get_prescriptions(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[PrescriptionItem]:
        sql = """
        SELECT
          p.PRESCRIPTION_ID   AS PRESCRIPTION_ID,
          p.PATIENT_ID        AS PATIENT_ID,
          p.PROTOCOL_ID       AS PROTOCOL_ID,
          p.STARTING_DATE     AS STARTING_DATE,
          p.ENDING_DATE       AS ENDING_DATE,
          p.WEEKDAY           AS WEEKDAY,
          p.SESSION_DURATION  AS SESSION_DURATION,
          p.AR_MODE           AS AR_MODE
        FROM prescription_plus p
        WHERE p.PATIENT_ID = :pid
          AND p.STARTING_DATE <= :end_dt
          AND (p.ENDING_DATE IS NULL OR p.ENDING_DATE >= :start_dt)
        """
        rows = self._rows(sql, pid=patient_id, start_dt=start, end_dt=end)
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        out: list[PrescriptionItem] = []
        for r in rows:
            s_dt = _to_datetime(r.get("STARTING_DATE"))
            e_dt = _to_datetime(r.get("ENDING_DATE"))
            if s_dt is None:
                continue
            p_start = s_dt.date()
            p_end = e_dt.date() if e_dt else end_d
            if p_end > date(2099, 1, 1):
                p_end = end_d
            # SESSION_DURATION is always seconds in the NEST DB; convert to minutes.
            try:
                sd_sec = int(float(r.get("SESSION_DURATION") or 1800))
            except (ValueError, TypeError):
                sd_sec = 1800
            session_duration_min = max(1, sd_sec // 60)
            try:
                out.append(PrescriptionItem(
                    prescription_id=int(r.get("PRESCRIPTION_ID") or 0),
                    patient_id=patient_id,
                    protocol_id=int(r.get("PROTOCOL_ID") or 0),
                    start_date=p_start,
                    end_date=p_end,
                    weekday=_weekday_to_int(r.get("WEEKDAY")),
                    session_duration_min=session_duration_min,
                    ar_mode=r.get("AR_MODE"),
                ))
            except (ValueError, TypeError):
                continue
        return out

    def _timeseries_rows(self, table: str, session_ids: list[int]) -> list[dict]:
        if not session_ids:
            return []
        sql = f"""
        SELECT
          SESSION_ID, PATIENT_ID, PROTOCOL_ID, GAME_MODE,
          SECONDS_FROM_START, PARAMETER_KEY, PARAMETER_VALUE
        FROM {table}
        WHERE SESSION_ID IN :sids
        ORDER BY SESSION_ID, SECONDS_FROM_START
        """
        from sqlalchemy import text, bindparam
        with self._engine.connect() as conn:
            stmt = text(sql).bindparams(bindparam("sids", expanding=True))
            result = conn.execute(stmt, {"sids": [int(s) for s in session_ids]})
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]

    def get_difficulty_rows(self, session_ids: list[int]) -> list[DifficultyRow]:
        rows = self._timeseries_rows("difficulty_modulators_plus", session_ids)
        out: list[DifficultyRow] = []
        for r in rows:
            try:
                out.append(DifficultyRow(
                    session_id=int(r["SESSION_ID"]),
                    patient_id=int(r.get("PATIENT_ID") or 0),
                    protocol_id=int(r.get("PROTOCOL_ID") or 0),
                    game_mode=str(r.get("GAME_MODE") or "default"),
                    seconds_from_start=int(float(r.get("SECONDS_FROM_START") or 0)),
                    parameter_key=str(r.get("PARAMETER_KEY") or ""),
                    parameter_value=str(r.get("PARAMETER_VALUE") or ""),
                ))
            except (ValueError, TypeError, KeyError):
                continue
        return out

    def get_performance_rows(self, session_ids: list[int]) -> list[PerformanceRow]:
        rows = self._timeseries_rows("performance_estimators_plus", session_ids)
        out: list[PerformanceRow] = []
        for r in rows:
            try:
                out.append(PerformanceRow(
                    session_id=int(r["SESSION_ID"]),
                    patient_id=int(r.get("PATIENT_ID") or 0),
                    protocol_id=int(r.get("PROTOCOL_ID") or 0),
                    game_mode=str(r.get("GAME_MODE") or "default"),
                    seconds_from_start=int(float(r.get("SECONDS_FROM_START") or 0)),
                    parameter_key=str(r.get("PARAMETER_KEY") or ""),
                    parameter_value=str(r.get("PARAMETER_VALUE") or ""),
                ))
            except (ValueError, TypeError, KeyError):
                continue
        return out

    def get_kinematics_rows(self, session_ids: list[int]) -> list[KinematicsRow]:
        return []

    def get_self_reports(
        self, patient_id: int, start: datetime, end: datetime
    ) -> list[SelfReport]:
        sql = """
        SELECT
          q.EMOTIONAL_QUESTION_KEY AS EMOTIONAL_QUESTION_KEY,
          a.EMOTIONAL_ANSWER       AS EMOTIONAL_ANSWER,
          a.CREATION_TIME          AS CREATION_TIME
        FROM emotional_answer a
        JOIN emotional_question_patient qp
          ON qp.EMOTIONAL_QUESTION_PATIENT_ID = a.EMOTIONAL_QUESTION_PATIENT_ID
        JOIN emotional_question q
          ON q.EMOTIONAL_QUESTION_ID = qp.EMOTIONAL_QUESTION_ID
        WHERE qp.PATIENT_ID = :pid
          AND a.CREATION_TIME >= :start_dt
          AND a.CREATION_TIME <  :end_dt
        ORDER BY a.CREATION_TIME ASC
        """
        rows = self._rows(sql, pid=patient_id, start_dt=start, end_dt=end)
        out: list[SelfReport] = []
        for r in rows:
            ts = _to_datetime(r.get("CREATION_TIME"))
            if ts is None:
                continue
            out.append(SelfReport(
                patient_id=patient_id,
                key=str(r.get("EMOTIONAL_QUESTION_KEY") or ""),
                value=r.get("EMOTIONAL_ANSWER") or "",
                timestamp=ts,
            ))
        return out

    def get_assessments(
        self,
        patient_id: int,
        start: datetime,
        end: datetime,
        types: list[str] | None = None,
    ) -> list[Assessment]:
        # clinical_scores not in DB for this demo.
        return []

    def _load_protocols(self) -> dict[int, str]:
        if self._protocol_cache is not None:
            return self._protocol_cache
        try:
            rows = self._rows("SELECT PROTOCOL_ID, NAME_KEY FROM protocol")
        except Exception as e:  # pragma: no cover - surfaces on bad schema
            logger.warning("protocol table read failed: %s", e)
            self._protocol_cache = {}
            return self._protocol_cache
        out: dict[int, str] = {}
        for r in rows:
            try:
                pid = int(r.get("PROTOCOL_ID") or 0)
                name = (r.get("NAME_KEY") or "").strip()
                if pid and name:
                    out[pid] = name
            except (ValueError, TypeError):
                continue
        self._protocol_cache = out
        return out

    def get_protocol_catalog(self) -> dict[int, ProtocolInfo]:
        return {
            pid: ProtocolInfo(
                protocol_id=pid, modality="", targets=[],
                supports_kinematics=False, difficulty_modulator_keys=[],
            )
            for pid in self._load_protocols()
        }

    def get_protocol_name(self, protocol_id: int) -> str:
        return self._load_protocols().get(protocol_id) or f"Protocol {protocol_id}"

    # --- lookups ---------------------------------------------------------

    def list_patient_ids(self) -> list[int]:
        try:
            rows = self._rows("SELECT PATIENT_ID FROM patient ORDER BY PATIENT_ID")
        except Exception as e:
            logger.warning("patient table read failed: %s", e)
            return []
        return [int(r["PATIENT_ID"]) for r in rows if r.get("PATIENT_ID") is not None]

    def resolve_patient(self, query: str) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        if q.isdigit():
            sql = """
            SELECT PATIENT_ID, COALESCE(PATIENT_USER, '') AS PATIENT_USER
            FROM patient WHERE PATIENT_ID = :pid
            """
            rows = self._rows(sql, pid=int(q))
        else:
            sql = """
            SELECT PATIENT_ID, COALESCE(PATIENT_USER, '') AS PATIENT_USER
            FROM patient
            WHERE PATIENT_USER LIKE :q
            ORDER BY PATIENT_USER
            LIMIT 25
            """
            rows = self._rows(sql, q=f"%{q}%")
        return [
            {"patient_id": int(r["PATIENT_ID"]), "patient_user": str(r.get("PATIENT_USER") or "")}
            for r in rows
        ]

    def patient_active_weeks(self, patient_id: int) -> list[date]:
        sql = """
        SELECT DISTINCT DATE(s.STARTING_DATE) AS D
        FROM session_plus s
        JOIN prescription_plus p ON p.PRESCRIPTION_ID = s.PRESCRIPTION_ID
        WHERE p.PATIENT_ID = :pid
          AND s.STARTING_DATE IS NOT NULL
        """
        rows = self._rows(sql, pid=patient_id)
        weeks: set[date] = set()
        for r in rows:
            d = r.get("D")
            if isinstance(d, datetime):
                d = d.date()
            if not isinstance(d, date):
                continue
            weeks.add(d + timedelta(days=(6 - d.weekday())))
        return sorted(weeks)

    def get_patient_ids_in_window(self, start: datetime, end: datetime) -> list[int]:
        sql = """
        SELECT DISTINCT PATIENT_ID
        FROM prescription_plus
        WHERE STARTING_DATE <= :end_dt
          AND (ENDING_DATE IS NULL OR ENDING_DATE >= :start_dt)
        """
        rows = self._rows(sql, start_dt=start, end_dt=end)
        return [int(r["PATIENT_ID"]) for r in rows if r.get("PATIENT_ID") is not None]
