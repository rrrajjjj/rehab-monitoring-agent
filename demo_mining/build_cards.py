"""
For each row in demo_mining/shortlist.json, build a triage card AND a patient
check-in at the focal week. Both LLM calls see the same 4-week trailing window
of metrics via MetricsBuilder. Results are persisted to demo_mining/card_store/
as one JSON per patient-week (containing both the triage card and the check-in).

LLM responses are cached (.llm_cache/), so re-runs are free unless the cache
is cleared or the prompt changed.

Usage:
  python -m demo_mining.build_cards \
    --shortlist demo_mining/shortlist.json \
    --data-dir data \
    --store demo_mining/card_store

Skip already-built cards with --skip-existing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from crtv.pipeline.historical_runner import HistoricalTriageRunner  # noqa: E402
from crtv.reasoning.patient_checkin import PatientCheckInEngine  # noqa: E402
from demo_mining.card_store import CardStore  # noqa: E402

logger = logging.getLogger("demo_mining.build_cards")


def _to_date(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def build(
    shortlist_path: Path,
    data_dir: str,
    store_path: Path,
    skip_existing: bool,
    only_pids: set[int] | None = None,
) -> int:
    shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    if only_pids is not None:
        shortlist = [row for row in shortlist if int(row["patient_id"]) in only_pids]
    logger.info("Loaded %d shortlist entries", len(shortlist))

    runner = HistoricalTriageRunner(data_dir)
    checkin_engine = PatientCheckInEngine()
    store = CardStore(store_path)

    built = 0
    skipped = 0
    for row in shortlist:
        pid = int(row["patient_id"])
        checkpoint = _to_date(row.get("checkpoint_date") or row["focal_week_end"])
        key = (pid, checkpoint.isoformat())
        if skip_existing and key in store:
            skipped += 1
            logger.debug("skip existing %s", key)
            continue
        logger.info("building pid=%s checkpoint=%s", pid, checkpoint)
        entry = runner.run_single_checkpoint(pid, checkpoint)
        if entry is None:
            logger.warning("no data for pid=%s checkpoint=%s", pid, checkpoint)
            continue

        # Patient-facing check-in, same metrics snapshot
        try:
            checkin = checkin_engine.generate(entry["metrics"])
            entry["checkin"] = asdict(checkin)
        except Exception as e:
            logger.warning("checkin failed pid=%s: %s", pid, e)
            entry["checkin"] = None

        out = store.save(entry)
        logger.info("  wrote %s (%s)", out.name, entry["disposition"])
        built += 1

    logger.info("Done. built=%d skipped=%d total_in_store=%d", built, skipped, len(store))
    return built


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build triage cards + check-ins for the demo shortlist")
    p.add_argument("--shortlist", default="demo_mining/shortlist.json")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--store", default="demo_mining/card_store")
    p.add_argument("--skip-existing", action="store_true", help="Don't rebuild cards already on disk")
    p.add_argument("--only-pids", default="", help="Comma-separated patient ids to restrict to")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    only_pids = {int(x) for x in args.only_pids.split(",") if x.strip()} or None
    build(Path(args.shortlist), args.data_dir, Path(args.store), args.skip_existing, only_pids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
