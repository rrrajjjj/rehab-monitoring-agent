"""
Enumerate (patient_id, focal_week) windows across NEST, build the full LLM
metrics dict for each, and write one JSONL row per window.

Output row schema (per line):
  {
    "patient_id": int,
    "focal_week_start": "YYYY-MM-DD",   # Monday of the focal week
    "focal_week_end":   "YYYY-MM-DD",   # exclusive
    "checkpoint_date":  "YYYY-MM-DD",   # = focal_week_end (checkpoint convention)
    "focal_session_count": int,          # sessions strictly inside focal week
    "window_days": 28,                   # trailing window the metrics cover
    "metrics": {...},                    # exact dict fed to MedGemmaTriageEngine
    "prompt_text": "...",                # literal output of _metrics_to_prompt
    "drift_events": [{"type":..., "severity":...}, ...],
  }

Usage:
  python -m demo_mining.mine_features \
    --data-dir data \
    --out demo_mining/windows.jsonl \
    --min-focal-sessions 2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

from crtv.pipeline.metrics_builder import MetricsBuilder
from crtv.reasoning.medgemma_triage import _metrics_to_prompt

logger = logging.getLogger("demo_mining.mine_features")


def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _patient_date_range(builder: MetricsBuilder, patient_id: int) -> tuple[date, date] | None:
    sessions = builder.adapter.get_sessions(
        patient_id, datetime(2000, 1, 1), datetime(2100, 1, 1)
    )
    if not sessions:
        return None
    dates = [s.start_time.date() for s in sessions]
    return min(dates), max(dates)


def _enumerate_patients(builder: MetricsBuilder) -> list[int]:
    """Enrolled NEST cohort = patients present in NEST_clinical_scores.csv.
    The prescription table also contains testers/admins which we must exclude."""
    ids: set[int] = set()
    for r in builder.adapter._clinical_scores:
        try:
            pid = int(float(r.get("PATIENT_ID") or 0))
            if pid:
                ids.add(pid)
        except (ValueError, TypeError):
            continue
    return sorted(ids)


def mine(
    data_dir: str,
    out_path: Path,
    min_focal_sessions: int = 2,
    window_days: int = 28,
    patient_ids: list[int] | None = None,
) -> int:
    """Write one JSONL row per eligible (patient, focal_week). Returns row count."""
    builder = MetricsBuilder.from_data_dir(data_dir)
    patients = patient_ids if patient_ids is not None else _enumerate_patients(builder)
    logger.info("Mining %d patients", len(patients))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped_sparse = 0
    skipped_no_data = 0

    with out_path.open("w", encoding="utf-8") as f:
        for pid in patients:
            rng = _patient_date_range(builder, pid)
            if rng is None:
                skipped_no_data += 1
                continue
            first_session, last_session = rng

            # Focal week is the 7-day bucket ending at checkpoint.
            # Start at the first Monday AFTER first_session so the focal week is
            # fully inside the patient's active range.
            first_monday = _monday_on_or_before(first_session)
            focal_start = first_monday + timedelta(days=7)  # first complete week
            last_monday = _monday_on_or_before(last_session)
            # include the week containing last_session
            final_focal_start = last_monday

            fs = focal_start
            while fs <= final_focal_start:
                focal_end = fs + timedelta(days=7)  # exclusive
                checkpoint = datetime.combine(focal_end, datetime.min.time())
                snapshot = builder.build(pid, checkpoint, window_days=window_days)
                if snapshot is None:
                    fs += timedelta(days=7)
                    continue

                focal_sessions = [
                    s for s in snapshot.bundle.sessions if fs <= s.start_time.date() < focal_end
                ]
                if len(focal_sessions) < min_focal_sessions:
                    skipped_sparse += 1
                    fs += timedelta(days=7)
                    continue

                prompt_text = _metrics_to_prompt(snapshot.metrics)

                row = {
                    "patient_id": pid,
                    "focal_week_start": fs.isoformat(),
                    "focal_week_end": focal_end.isoformat(),
                    "checkpoint_date": focal_end.isoformat(),
                    "focal_session_count": len(focal_sessions),
                    "window_days": window_days,
                    "metrics": snapshot.metrics,
                    "prompt_text": prompt_text,
                    "drift_events": snapshot.metrics.get("drift_events", []),
                }
                f.write(json.dumps(row, default=str))
                f.write("\n")
                written += 1

                fs += timedelta(days=7)

    logger.info(
        "Wrote %d rows | skipped sparse=%d | skipped no-data patients=%d",
        written,
        skipped_sparse,
        skipped_no_data,
    )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mine (patient, focal_week) feature windows for demo selection.")
    parser.add_argument("--data-dir", default="data", help="NEST data directory")
    parser.add_argument("--out", default="demo_mining/windows.jsonl", help="Output JSONL path")
    parser.add_argument("--min-focal-sessions", type=int, default=2, help="Drop windows with fewer sessions in focal week")
    parser.add_argument("--window-days", type=int, default=28, help="Trailing window for metrics (days)")
    parser.add_argument("--patient", type=int, action="append", default=None, help="Limit to specific patient id(s); repeatable")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    written = mine(
        data_dir=args.data_dir,
        out_path=Path(args.out),
        min_focal_sessions=args.min_focal_sessions,
        window_days=args.window_days,
        patient_ids=args.patient,
    )
    print(f"wrote {written} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
