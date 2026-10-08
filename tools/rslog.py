"""
RobloxStudioLive - logging.

Two streams, deliberately separate:

* `logs/sync-YYYY-MM-DD.log` -- human-readable, one line per event, with
  timestamp, direction and file.  This is the "synchronisation log" the user
  reads.
* `logs/audit.jsonl`         -- append-only machine-readable record of every
  decision and every byte written, in either direction.  If something ever
  looks wrong, this is the file that proves what happened.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["get_logger", "audit", "read_audit_tail"]

_AUDIT_LOCK = threading.Lock()
_CONFIGURED: dict[str, logging.Logger] = {}


def get_logger(logs_dir: Path, level: str = "INFO", name: str = "rss") -> logging.Logger:
    """Console + dated file logger."""
    logs = Path(logs_dir)
    logs.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    key = "%s|%s|%s" % (logs, day, name)

    logger = _CONFIGURED.get(key)
    if logger is not None:
        return logger

    logger = logging.getLogger(key)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    handler = logging.FileHandler(logs / ("sync-%s.log" % day), encoding="utf-8")
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    _CONFIGURED[key] = logger
    return logger


def audit(logs_dir: Path, event: dict) -> None:
    """Append one JSON event to logs/audit.jsonl (never rewrites history)."""
    logs = Path(logs_dir)
    logs.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat()}
    record.update(event)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    with _AUDIT_LOCK:
        with open(logs / "audit.jsonl", "a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass


def read_audit_tail(logs_dir: Path, limit: int = 50) -> list[dict]:
    path = Path(logs_dir) / "audit.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
