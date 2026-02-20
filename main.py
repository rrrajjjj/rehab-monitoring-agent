"""CLI entry point for CRTV pipeline."""

import sys
from datetime import datetime

from crtv.adapters.mock_adapter import MockAdapter
from crtv.pipeline.runner import TriagePipeline


def main():
    """Run triage pipeline."""
    adapter = MockAdapter()
    pipeline = TriagePipeline(adapter)
    run_date = datetime(2024, 1, 25)  # Default for fixture window
    if len(sys.argv) > 1:
        run_date = datetime.fromisoformat(sys.argv[1])
    results = pipeline.run(run_date)
    print(f"Processed {len(results)} patient(s)")
    for r in results:
        print(f"  Patient {r['patient_id']}: {r['disposition']} - {r['card'].headline}")


if __name__ == "__main__":
    main()
