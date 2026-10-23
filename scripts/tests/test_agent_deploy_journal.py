import importlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
journal = importlib.import_module("agent_deploy_journal")


def test_now_is_a_timezone_aware_iso_timestamp():
    assert datetime.fromisoformat(journal.now()).utcoffset().total_seconds() == 0


def test_atomic_receipt_and_exclusive_lock_preserve_prior_artifacts(tmp_path):
    path = tmp_path / "receipt.json"
    journal.atomic_json(path, {"state": "dry_run"})
    journal.atomic_json(path, {"state": "submitted", "run_id": "run-1"})
    assert json.loads(path.read_text())["run_id"] == "run-1"
    with journal.receipt_lock(path):
        with pytest.raises(FileExistsError):
            with journal.receipt_lock(path):
                pytest.fail("Concurrent apply acquired the receipt lock")
    assert not path.with_name(path.name + ".lock").exists()
    pending = path.with_name(path.name + ".pending")
    pending.write_text("unreconciled prior crash")
    with pytest.raises(FileExistsError):
        journal.atomic_json(path, {"state": "should-not-overwrite"})
    assert pending.read_text() == "unreconciled prior crash"
    assert json.loads(path.read_text())["run_id"] == "run-1"


def test_failed_write_leaves_no_pending_file_and_keeps_the_previous_receipt(tmp_path):
    path = tmp_path / "receipt.json"
    journal.atomic_json(path, {"state": "dry_run"})
    with pytest.raises(ValueError):
        journal.atomic_json(path, {"state": float("nan")})
    assert not path.with_name(path.name + ".pending").exists()
    assert json.loads(path.read_text()) == {"state": "dry_run"}


def test_lock_records_the_owner_and_is_released_after_a_failure(tmp_path):
    path = tmp_path / "nested" / "receipt.json"
    lock = path.with_name(path.name + ".lock")
    with pytest.raises(RuntimeError, match="boom"):
        with journal.receipt_lock(path):
            assert set(json.loads(lock.read_text())) == {"pid", "created_at"}
            raise RuntimeError("boom")
    assert not lock.exists()
