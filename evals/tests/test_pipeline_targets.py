import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from evals.foundry_rubrics import AGENT_RUBRICS

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


optimizer = load("run_prompt_optimizer", "pipelines/optimizer/run_prompt_optimizer.py")
redteam = load("run_redteam", "pipelines/red-team/run_redteam.py")


def test_optimizer_agents_are_the_insurance_prompts_and_the_rubric_keys():
    assert set(optimizer.PROMPT_FILES) == set(AGENT_RUBRICS)
    for agent in optimizer.PROMPT_FILES:
        path, data = optimizer._load_agent_prompt(agent)
        assert path.parent == ROOT / "agents" / "insurance"
        assert data["name"].startswith("northwind-insurance-") and str(data["instructions"]).strip()
        assert path == ROOT / "agents" / AGENT_RUBRICS[agent]["agent_yaml"]


def test_the_optimizer_workflow_only_matrixes_known_agents_and_the_insurance_dataset():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "optimizer.yml").read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["optimize"]["strategy"]["matrix"]["agent"]
    assert matrix and set(matrix) <= set(optimizer.PROMPT_FILES)
    text = (ROOT / ".github" / "workflows" / "optimizer.yml").read_text(encoding="utf-8")
    assert "data/golden/dataset.jsonl" in text and "steps.opt.outputs.prompt_path" in text


def baseline(tmp_path, aggregates):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema_version": 2, "aggregates": aggregates}), encoding="utf-8")
    return path


@pytest.mark.parametrize("department", ["claims-concierge", "order-concierge"])
def test_optimizer_reads_the_canonical_and_the_legacy_department_baseline(tmp_path, department):
    path = baseline(tmp_path, {department: {"orchestrator": {"triage_rubric_score": 4.25}}})
    assert optimizer._baseline_score(path, "triage_rubric_score") == 4.25
    assert optimizer._baseline_score(path, "safety_rubric_score") == 0.0


def test_optimizer_baseline_is_zero_without_a_measured_value(tmp_path):
    assert optimizer._baseline_score(tmp_path / "missing.json", "triage_rubric_score") == 0.0
    assert optimizer._baseline_score(ROOT / "evals" / "baseline.json", "triage_rubric_score") == 0.0
    assert optimizer._baseline_score(baseline(tmp_path, {"claims-concierge": {"orchestrator": {"triage_rubric_score": True}}}),
                                     "triage_rubric_score") == 0.0
    flat = baseline(tmp_path, {"triage_rubric_score": 3.5})
    assert optimizer._baseline_score(flat, "triage_rubric_score") == 3.5


def test_optimizer_publishes_the_prompt_path_for_the_pull_request(monkeypatch, tmp_path):
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(optimizer, "run", lambda *_args: {"agent": "claim_action_draft", "improved": True, "lift": 0.1})
    monkeypatch.setattr(sys, "argv", ["optimizer", "--agent", "claim_action_draft", "--dataset", "d", "--baseline", "b",
                                      "--report", str(tmp_path / "report.json")])
    assert optimizer.main() == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines == ["improved=true", "lift=0.1000", "prompt_path=agents/insurance/claim_action_draft.yaml"]


def test_redteam_targets_the_insurance_orchestrator_by_default(monkeypatch, tmp_path):
    assert redteam.DEFAULT_AGENT == "northwind-insurance-orchestrator"
    seen = []

    async def fake_run(agent, strategies, attacks):
        seen.append(agent)
        return {"attack_details": [{"attack_success": False, "attack_strategy": "Jailbreak"}]}

    monkeypatch.setattr(redteam, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["redteam", "--strategies", "jailbreak,harm",
                                      "--report", str(tmp_path / "report.json")])
    assert redteam.main() == 0
    assert seen == ["northwind-insurance-orchestrator"]


def test_the_redteam_workflow_defaults_to_the_insurance_orchestrator():
    text = (ROOT / ".github" / "workflows" / "red-team.yml").read_text(encoding="utf-8")
    assert "northwind-insurance-orchestrator" in text
    assert "northwind-orchestrator" not in text.replace("northwind-insurance-orchestrator", "")
