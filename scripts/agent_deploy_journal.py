"""Crash-safe deployment receipt helpers: an exclusive lock file and atomic JSON writes."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    created = False
    try:
        with pending.open("x", encoding="utf-8") as stream:
            created = True
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    except BaseException:
        # A stale pending/lock after process death requires manual reconciliation.
        if created and pending.exists():
            pending.unlink()
        raise


@contextmanager
def receipt_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "created_at": now()}))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        yield
    finally:
        lock.unlink()
