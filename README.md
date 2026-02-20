# CRTV — Continuous Rehab Triage + Voice

Clinician-in-the-loop triage system for stroke telerehabilitation built on Eodyne's RGS ecosystem. Part of the MedGemma Impact Challenge.

## Setup

```bash
pip install -r requirements.txt
cd "medgemma agent" && pip install -e .
```

## Usage

**Run pipeline (mock data):**
```bash
python main.py
```

**Run clinician web UI:**
```bash
pip install fastapi uvicorn
python run_web.py
```
Then open http://127.0.0.1:8000

**Programmatic:**
```python
from datetime import datetime
from crtv.adapters.mock_adapter import MockAdapter
from crtv.repositories.patient_history import PatientHistoryRepository
from crtv.pipeline.runner import TriagePipeline

adapter = MockAdapter()
repo = PatientHistoryRepository(adapter)
bundle = repo.load(patient_id=1, start=datetime(2024,1,1), end=datetime(2024,2,1))

pipeline = TriagePipeline(adapter)
results = pipeline.run(datetime(2024, 1, 25))
```

## Structure

- `crtv/domain` - Pydantic models
- `crtv/adapters` - DatabaseAdapter, MockAdapter (raw-table fixtures)
- `crtv/builders` - Session, difficulty, performance, kinematics
- `crtv/features` - Adherence, session summaries, learning rate, PPF
- `crtv/drift` - DriftDetector, PatientStateBuilder
- `crtv/checkin` - CheckInPolicy, CheckInInterpreter (MedGemma)
- `crtv/recommendations` - RecommendationEngine
- `crtv/cards` - TriageCardRenderer, ClinicianSummaryGenerator
- `crtv/pipeline` - TriagePipeline
