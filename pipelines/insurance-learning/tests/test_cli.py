import json
import subprocess
import sys
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
ROOT = PIPELINE.parents[1]
sys.path.insert(0, str(PIPELINE))
import cli  # noqa: E402

RETIRED_COMMANDS = (
    "init", "import", "status", "review", "export", "approve-dataset", "budget-template",
    "train", "evaluate", "publish", "native-evaluate", "job-status",
)


@pytest.mark.parametrize("command", RETIRED_COMMANDS)
def test_retired_lifecycle_commands_are_rejected(command):
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args([command])
    assert raised.value.code == 2


@pytest.mark.parametrize("command", ["prepare", "prepare-insurance"])
def test_prepare_delegates_to_the_insurance_pipeline(monkeypatch, tmp_path, capsys, command):
    seen = []
    monkeypatch.setattr(cli, "prepare", lambda output: seen.append(output) or {"state": "prepared_unreviewed"})
    assert cli.main([command, "--output", str(tmp_path / "pack")]) == 0
    assert seen == [tmp_path / "pack"]
    assert json.loads(capsys.readouterr().out) == {"state": "prepared_unreviewed"}


def test_failures_are_reported_as_blocked_without_a_traceback(monkeypatch, tmp_path, capsys):
    def refuse(output):
        raise FileExistsError("Preserve prepared insurance versions; choose a new output directory.")

    monkeypatch.setattr(cli, "prepare", refuse)
    assert cli.main(["prepare", "--output", str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "blocked" and "new output directory" in report["error"]


def test_script_entry_point_offers_prepare_only():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "insurance-learning.py"), "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0
    assert "{prepare,prepare-insurance}" in result.stdout.splitlines()[0]
