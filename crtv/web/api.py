"""FastAPI app for clinician frontend."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root before reading env vars
load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Logging: ensure crtv.* always output (uvicorn may override root logger)
import logging
import sys
_log_level = getattr(logging, (os.environ.get("CRTV_LOG_LEVEL") or "INFO").upper(), logging.INFO)
_log_fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
for _name in ("crtv.llm", "crtv.triage"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(_log_level)
    if not _lg.handlers:
        _h = logging.StreamHandler(sys.stderr)
        _h.setFormatter(logging.Formatter(_log_fmt))
        _lg.addHandler(_h)

from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from crtv.adapters.mock_adapter import MockAdapter
from crtv.web.service import ClinicianViewService
from crtv.web.historical_service import HistoricalTriageService

app = FastAPI(title="CRTV Clinician View", version="0.1.0")

# Data dir: medgemma agent/data or env CRTV_DATA_DIR
DATA_DIR = Path(__file__).parent.parent.parent / "data"
if os.environ.get("CRTV_DATA_DIR"):
    DATA_DIR = Path(os.environ["CRTV_DATA_DIR"])
USE_REAL_DATA = DATA_DIR.exists()
USE_MEDGEMMA = os.environ.get("CRTV_USE_MEDGEMMA", "").lower() in ("1", "true", "yes")

if USE_REAL_DATA:
    from crtv.adapters.csv_adapter import CSVDataAdapter
    csv_adapter = CSVDataAdapter(DATA_DIR)
    historical_service = HistoricalTriageService(DATA_DIR, use_medgemma=USE_MEDGEMMA)
    service = ClinicianViewService(csv_adapter)
else:
    historical_service = None
    adapter = MockAdapter()
    service = ClinicianViewService(adapter)

DEFAULT_RUN_DATE = datetime(2024, 1, 25)


@app.get("/api/mode")
def get_mode():
    """Whether using real NEST data, mock, and which LLM provider is active."""
    from crtv.reasoning.llm_providers import get_provider
    provider = get_provider(use_medgemma=USE_MEDGEMMA)
    return {
        "use_real_data": USE_REAL_DATA,
        "use_medgemma": USE_MEDGEMMA,
        "llm_provider": type(provider).__name__.replace("Provider", "").lower(),
    }


@app.get("/api/llm-verify")
def llm_verify():
    """
    Make one direct OpenAI API call (bypasses cache).
    Use to verify API key works and requests show up on platform.openai.com.
    """
    from crtv.reasoning.llm_providers import OpenAICompatibleProvider
    provider = OpenAICompatibleProvider()
    if not provider.api_key:
        return {"ok": False, "error": "CRTV_OPENAI_API_KEY not set", "response": None}
    try:
        raw = provider.generate("Reply with exactly: OK")
        ok = bool(raw and "ok" in raw.lower())
        return {
            "ok": ok,
            "error": None if ok else "Empty or unexpected response",
            "response": raw[:200] if raw else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "response": None}


@app.get("/api/triage-cards")
def list_triage_cards():
    """All triage cards from historical run (real data mode)."""
    if not historical_service:
        return []
    return historical_service.list_triage_cards()


@app.get("/api/patients")
def list_patients(run_date: str | None = None):
    """List patients. In real data mode: grouped by patient, max attention across triages."""
    if USE_REAL_DATA and historical_service:
        return historical_service.list_patients_grouped()
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    return service.list_patients(rd)


@app.get("/api/triage-cards/{patient_id}/{checkpoint_date}")
def get_triage_card_detail(patient_id: int, checkpoint_date: str):
    """Full detail for a triage card."""
    if not historical_service:
        raise HTTPException(status_code=404, detail="Real data mode required")
    detail = historical_service.get_card_detail(patient_id, checkpoint_date)
    if detail is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return detail


@app.get("/api/patients/{patient_id}")
def get_patient_detail(patient_id: int, run_date: str | None = None):
    """Full triage card + observations. In real data mode, returns primary checkpoint (highest attention)."""
    if USE_REAL_DATA and historical_service:
        detail = historical_service.get_patient_detail(patient_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Patient not found")
        return detail
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    detail = service.get_patient_detail(patient_id, rd)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return detail


@app.get("/api/patients/{patient_id}/adherence")
def get_patient_adherence(patient_id: int, run_date: str | None = None):
    """Adherence calendar data (mock mode)."""
    if not service:
        raise HTTPException(status_code=404)
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    detail = service.get_patient_detail(patient_id, rd)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return detail.get("adherence", {})


@app.get("/api/patients/{patient_id}/metrics")
def get_patient_metrics(patient_id: int, run_date: str | None = None):
    """Metric time-series for charts (mock mode)."""
    if not service:
        raise HTTPException(status_code=404)
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    detail = service.get_patient_detail(patient_id, rd)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return detail.get("metrics", {})


# Serve static frontend
FRONTEND_DIR = Path(__file__).parent / "static"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
