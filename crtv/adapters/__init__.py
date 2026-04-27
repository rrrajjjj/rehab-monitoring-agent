"""Data adapters.

Backend selection via env:
    CRTV_DATA_BACKEND = csv | mysql | auto  (default: auto)

'auto' picks MySQL when DB_HOST is set, else CSV when a data dir exists.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from crtv.adapters.database import DatabaseAdapter

logger = logging.getLogger("crtv.adapters")


def get_adapter(data_dir: str | Path | None = None) -> DatabaseAdapter:
    """Return the adapter selected by env. Raises RuntimeError if none available."""
    backend = (os.environ.get("CRTV_DATA_BACKEND") or "auto").lower()

    if backend == "mysql" or (backend == "auto" and os.environ.get("DB_HOST")):
        from crtv.adapters.mysql_adapter import MySQLDataAdapter
        logger.info("data backend: mysql (host=%s db=%s)",
                    os.environ.get("DB_HOST"), os.environ.get("DB_NAME"))
        return MySQLDataAdapter()

    if backend in ("csv", "auto"):
        from crtv.adapters.csv_adapter import CSVDataAdapter
        dd = Path(data_dir) if data_dir else Path(
            os.environ.get("CRTV_DATA_DIR") or "data"
        )
        if not dd.exists():
            raise RuntimeError(
                f"CSV backend selected but data dir not found: {dd}. "
                "Set CRTV_DATA_DIR, or use CRTV_DATA_BACKEND=mysql with DB_* env vars."
            )
        logger.info("data backend: csv (dir=%s)", dd)
        return CSVDataAdapter(dd)

    raise RuntimeError(f"unknown CRTV_DATA_BACKEND: {backend!r}")
