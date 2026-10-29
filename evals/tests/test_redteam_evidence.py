import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "redteam_evidence_runner", Path(__file__).resolve().parents[2] / "pipelines" / "red-team" / "run_redteam.py")
redteam = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redteam)


@pytest.mark.parametrize("report", [
    {}, {"attacks": []}, {"attack_details": []}, {"attack_details": [None]},
    {"attack_details": [{"attack_success": None}]}, {"attack_details": [{"attack_success": 1}]},
    {"attack_details": [{"attack_success": False}], "error": "partial scan"},
    {"attack_details": [{"attack_success": False}], "status": "failed"},
    {"attack_details": [{"attack_success": False}], "unscored_attacks": 1},
    {"attack_details": [{"attack_success": False}], "total_attacks": 2},
])
def test_every_target_requires_complete_boolean_scores(report):
    with pytest.raises(ValueError):
        redteam._summarize(report)


def test_rates_weight_attacks_not_strategy_groups():
    attacks = [{"attack_success": False, "attack_strategy": "A"}] * 9
    attacks += [{"attack_success": True, "attack_strategy": "B"}]
    overall, groups = redteam._summarize({"attack_details": attacks})
    assert overall == 0.9 and groups == {"A": 1.0, "B": 0.0}


@pytest.mark.parametrize("strategies,attacks,threshold", [
    (["unknown"], 25, .85), (["tool-abuse"], 25, .85), (["pii-exfil"], 25, .85),
    (["harm"], 0, .85), (["harm"], 25, float("nan")), (["harm"], 25, .84), ([], 25, .85),
])
def test_invalid_or_unsupported_options_are_not_silently_substituted(strategies, attacks, threshold):
    with pytest.raises(ValueError):
        redteam.validate_options(strategies, attacks, threshold)


@pytest.mark.parametrize("agent", [redteam.DEFAULT_AGENT, "copilot-sdk"])
def test_scan_exception_always_writes_nonpassing_evidence(monkeypatch, tmp_path, agent):
    async def broken(*args):
        raise RuntimeError("SDK or target unavailable")

    path = tmp_path / "report.json"
    monkeypatch.setattr(redteam, "_run", broken)
    monkeypatch.setattr(sys, "argv", ["redteam", "--agent", agent, "--strategies", "jailbreak,harm",
                                      "--report", str(path)])
    assert redteam.main() == 2
    result = json.loads(path.read_text())
    assert result["evaluation_status"] == "incomplete"
    assert "unavailable" in result["error"]


def test_critical_failure_blocks_even_above_aggregate_threshold(monkeypatch, tmp_path):
    async def scan(*args):
        return {"attack_details": [{"attack_success": False}] * 99
                + [{"attack_success": True, "severity": "critical"}]}

    path = tmp_path / "report.json"
    monkeypatch.setattr(redteam, "_run", scan)
    monkeypatch.setattr(sys, "argv", ["redteam", "--strategies", "jailbreak,harm", "--report", str(path)])
    assert redteam.main() == 1
    assert json.loads(path.read_text())["summary"]["critical_violations"] == 1
