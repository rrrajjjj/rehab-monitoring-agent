"""Domain models for CRTV."""

from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field


class Session(BaseModel):
    """Session from session_plus + recording_plus."""

    session_id: int
    prescription_id: int
    patient_id: int
    protocol_id: int
    start_time: datetime
    duration_sec: float
    status: str
    platform: str
    device: str
    log_parsed: bool


class PrescriptionItem(BaseModel):
    """Prescription from prescription_plus."""

    prescription_id: int
    patient_id: int
    protocol_id: int
    start_date: date
    end_date: date
    weekday: int  # 0=Mon..6=Sun
    session_duration_min: int
    ar_mode: Optional[str] = None


class DifficultyTick(BaseModel):
    """Difficulty modulator tick at a point in session time."""

    session_id: int
    patient_id: int
    protocol_id: int
    game_mode: str
    t_sec: int
    modulators: dict[str, float]


class PerformancePoint(BaseModel):
    """Performance measurement at a point in session time."""

    session_id: int
    protocol_id: int
    game_mode: str
    t_sec: int
    performance: float


class KinematicsPoint(BaseModel):
    """Kinematics measurement (AR protocols)."""

    session_id: int
    protocol_id: int
    t_sec: int
    mqi: Optional[float] = None
    workspace_volume: Optional[float] = None
    smoothness: Optional[float] = None
    velocity: Optional[float] = None
    pause_ratio: Optional[float] = None


class SelfReport(BaseModel):
    """Patient self-report (mood, pain, etc.)."""

    patient_id: int
    key: str
    value: str | int | float
    timestamp: datetime


class Assessment(BaseModel):
    """Clinical assessment (Fugl-Meyer, MoCA, etc.)."""

    patient_id: int
    type: str
    score: float
    timestamp: datetime
    subscores: Optional[dict[str, float]] = None


class ProtocolInfo(BaseModel):
    """Protocol catalog entry."""

    protocol_id: int
    modality: str
    targets: list[str] = Field(default_factory=list)
    supports_kinematics: bool = False
    difficulty_modulator_keys: list[str] = Field(default_factory=list)


# --- MedGemma output schemas ---


class BarrierCode(BaseModel):
    """Barrier from check-in interpretation."""

    code: str
    severity: int  # 0-3
    confidence: float


class CheckInResult(BaseModel):
    """Schema-locked output from CheckInInterpreter (MedGemma)."""

    barriers: list[BarrierCode] = Field(default_factory=list)
    entities: dict[str, str | bool] = Field(default_factory=dict)
    safety_flags: dict[str, bool] = Field(default_factory=dict)
    supporting_snippets: list[str] = Field(default_factory=list)


class TriageCardText(BaseModel):
    """Schema-locked output from ClinicianSummaryGenerator (MedGemma)."""

    headline: str = ""
    reasons: list[str] = Field(default_factory=list)
    patient_voice_excerpt: str = ""
    evidence_summary: str = ""


# --- Raw table row types (for adapter) ---


class DifficultyRow(BaseModel):
    """Raw difficulty_modulators_plus row."""

    session_id: int
    patient_id: int
    protocol_id: int
    game_mode: str
    seconds_from_start: int
    parameter_key: str
    parameter_value: str


class PerformanceRow(BaseModel):
    """Raw performance_estimators_plus row."""

    session_id: int
    patient_id: int
    protocol_id: int
    game_mode: str
    seconds_from_start: int
    parameter_key: str
    parameter_value: str


class KinematicsRow(BaseModel):
    """Raw kinematics row."""

    session_id: int
    patient_id: int
    protocol_id: int
    seconds_from_start: int
    metric_key: str
    metric_value: float


class DataIntegrityEvent(BaseModel):
    """Emitted when session->patient validation fails."""

    session_id: int
    message: str
    recording_patient_id: Optional[int] = None
    prescription_patient_id: Optional[int] = None


# --- Drift and recommendations ---


class DriftEvent(BaseModel):
    """Detected drift event."""

    type: str  # ADHERENCE_DRIFT, PLATEAU, REGRESSION, OVERCHALLENGE, UNDERCHALLENGE, FATIGUE_CYCLE, DATA_ISSUE
    severity: int  # 0-3
    confidence: float
    window_start: datetime
    window_end: datetime
    evidence: dict = Field(default_factory=dict)
    session_ids: list[int] = Field(default_factory=list)


class PatientState(BaseModel):
    """Patient state from DriftDetector."""

    engagement_state: str  # stable, declining, dropout-risk
    challenge_state: str  # underchallenged, appropriate, overchallenged
    trajectory_state: str  # improving, plateau, regressing
    barrier_priors: dict = Field(default_factory=dict)
    confidence: float = 0.0
    evidence_pointers: list = Field(default_factory=list)


class ActionItem(BaseModel):
    """Action from action library."""

    action_type: str
    params: dict = Field(default_factory=dict)


class RecommendationBundle(BaseModel):
    """Output of RecommendationEngine."""

    disposition: str  # NO_ACTION, SUGGEST, ESCALATE
    rationale: list[str] = Field(default_factory=list)
    expected_effect: list[str] = Field(default_factory=list)
    recommended_actions: list[ActionItem] = Field(default_factory=list)
    audit: dict = Field(default_factory=dict)


class TriageCard(BaseModel):
    """Clinician-facing triage card."""

    headline: str
    reasons: list[str] = Field(default_factory=list)
    patient_voice_excerpt: str = ""
    recommended_actions: list[ActionItem] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    audit: dict = Field(default_factory=dict)


# --- Repository bundle ---


class PatientHistoryBundle(BaseModel):
    """Bundle returned by PatientHistoryRepository.load()."""

    patient_id: int
    start: datetime
    end: datetime
    sessions: list[Session] = Field(default_factory=list)
    prescriptions: list[PrescriptionItem] = Field(default_factory=list)
    difficulty_rows: list[DifficultyRow] = Field(default_factory=list)
    performance_rows: list[PerformanceRow] = Field(default_factory=list)
    kinematics_rows: list[KinematicsRow] = Field(default_factory=list)
    self_reports: list[SelfReport] = Field(default_factory=list)
    assessments: list[Assessment] = Field(default_factory=list)
    protocol_catalog: dict[int, ProtocolInfo] = Field(default_factory=dict)
