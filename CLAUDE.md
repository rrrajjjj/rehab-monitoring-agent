# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CRTV (Continuous Rehab Triage + Voice) is a clinician-in-the-loop triage system for stroke telerehabilitation. It processes patient session data through a layered pipeline to produce clinician-facing triage cards with evidence-based recommendations.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -e .                # Dev install
pip install -e ".[api]"         # With OpenAI support
pip install -e ".[dev]"         # With pytest

# Run CLI pipeline (mock data)
python main.py [YYYY-MM-DD]    # Default: 2024-01-25

# Run web server (auto-detects mock vs real data mode)
python run_web.py              # FastAPI on 0.0.0.0:8000

# Tests
pytest tests/ -v
pytest tests/test_adherence.py -v
pytest tests/ --cov=crtv
```

## Architecture

The system follows a layered pipeline architecture:

**Adapters → Repository → Builders → Features → Drift Detection → LLM Reasoning → Recommendations → Triage Cards**

### Data Layer
- **Adapters** (`crtv/adapters/`): `DatabaseAdapter` (abstract), `MockAdapter` (fixtures), `CSVDataAdapter` (real NEST CSV data from `data/`)
- **Repository** (`crtv/repositories/patient_history.py`): Loads a `PatientHistoryBundle` per patient per time window

### Feature Engineering
- **Builders** (`crtv/builders/`): Construct sessions, difficulty ticks, performance points, kinematics from raw rows
- **Features** (`crtv/features/`): Compute adherence, session signal summaries, learning rate trends, Patient-Protocol Fit (PPF)

### Analysis & Reasoning
- **Drift Detection** (`crtv/drift/detector.py`): `DriftDetector` produces 7 event types (ADHERENCE_DRIFT, PLATEAU, REGRESSION, OVERCHALLENGE, UNDERCHALLENGE, FATIGUE_CYCLE, DATA_ISSUE). `PatientStateBuilder` synthesizes engagement/challenge/trajectory states.
- **Check-in** (`crtv/checkin/`): `CheckInInterpreter` parses patient self-report text into structured barriers/entities. `CheckInPolicy` decides when to invoke.
- **LLM Reasoning** (`crtv/reasoning/`): `MedGemmaTriageEngine` orchestrates LLM calls and parses structured JSON output (observations with attention levels 1-3, actions, disposition).

### Output
- **Recommendations** (`crtv/recommendations/engine.py`): Rule-based `RecommendationEngine` producing disposition + actions
- **Cards** (`crtv/cards/`): `TriageCardRenderer` (template-based) and `ClinicianSummaryGenerator` (LLM-based)

### Orchestration
- **Pipeline** (`crtv/pipeline/runner.py`): `TriagePipeline` — batch processing for a given date
- **Historical** (`crtv/pipeline/historical_runner.py`): `HistoricalTriageRunner` — weekly checkpoint-based regression analysis with deduplication

### Web
- **API** (`crtv/web/api.py`): FastAPI with endpoints: `/api/mode`, `/api/llm-verify`, `/api/patients`, `/api/triage-cards`, `/api/detail/{id}`
- **Services**: `ClinicianViewService` (mock mode), `HistoricalTriageService` (real data mode)

## LLM Provider System

Configured via `CRTV_LLM_PROVIDER` env var. Three backends:
- `rule` — No LLM, rule-based fallback (default)
- `medgemma` — Local HuggingFace transformers (`google/medgemma-4b-it`)
- `openai` — OpenAI-compatible API (supports Azure, vLLM, Together, Groq)

All providers are wrapped by `CachingProvider` which caches deterministically by `(patient_id, model, week)`. Cache stored in `.llm_cache/`.

## Dual Data Modes

- **Mock mode**: Uses fixture JSON files in `crtv/fixtures/` (10 files). Active when `data/` directory is missing.
- **Real mode**: Loads NEST CSV files from `data/`. Auto-detected by `run_web.py`.

## Domain Model

All domain entities are Pydantic v2 models in `crtv/domain/models.py` (~47 classes). Key types: `PatientHistoryBundle`, `Session`, `DriftEvent`, `PatientState`, `TriageCard`, `RecommendationBundle`, `CheckInResult`.

## System Prompt

`prompts/triage_system.txt` defines the clinical reasoning guidelines for the LLM: finding types, attention levels (1-3), plot reference IDs, and expected JSON output schema.

## Configuration

All config is via environment variables (see `.env.example`). Key settings:
- `CRTV_LLM_PROVIDER`: rule | medgemma | openai
- `CRTV_TRIAL_MODE`: 1 = limited to 1 patient, 3 weeks
- `CRTV_LLM_CACHE` / `CRTV_LLM_USE_CACHE`: Control deterministic caching
- `CRTV_OPENAI_*`: API key, base URL, model, reasoning effort, timeouts

Python ≥ 3.10 required.
