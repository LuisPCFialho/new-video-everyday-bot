"""Logs em logs/YYYY-MM-DD.log (um ficheiro por dia, guarda N dias)."""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
# Estas bibliotecas registam URLs completos a INFO (com tokens): ficam em WARNING.
NOISY = ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3")


def purge_old_logs(log_dir: Path, today: date, keep_days: int) -> list[Path]:
    removed = []
    cutoff = today - timedelta(days=keep_days)
    for path in log_dir.glob("????-??-??.log"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def setup_logging(log_dir: Path, keep_days: int, today: date | None = None) -> None:
    today = today or date.today()
    log_dir.mkdir(parents=True, exist_ok=True)
    purge_old_logs(log_dir, today, keep_days)
    handlers = [
        logging.FileHandler(log_dir / f"{today.isoformat()}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=FORMAT, handlers=handlers, force=True)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
