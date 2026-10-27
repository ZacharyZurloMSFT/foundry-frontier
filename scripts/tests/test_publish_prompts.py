"""Prompt publishing names yaml prompts after their agents and lists the insurance prompts (offline)."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "publish-prompts.py"
SPEC = importlib.util.spec_from_file_location("publish_prompts", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_yaml_prompts_are_named_after_the_agent_they_declare(tmp_path):
    prompt = tmp_path / "policy_claim_lookup.yaml"
    prompt.write_text("name: northwind-insurance-policy-claim-lookup\ninstructions: x\n", encoding="utf-8")
    assert module._prompt_name(prompt, prompt.read_text(encoding="utf-8")) == "northwind-insurance-policy-claim-lookup"


@pytest.mark.parametrize("content", ["", "instructions: x\n", "name: ''\n", "name: 5\n", "- not a mapping\n", "name: [unclosed"])
def test_a_yaml_prompt_without_a_usable_name_falls_back_to_its_file_stem(tmp_path, content):
    assert module._prompt_name(tmp_path / "fallback.yaml", content) == "fallback"


def test_prompty_files_use_their_stem_and_the_harness_keeps_its_published_name(tmp_path):
    rubric = tmp_path / "rubrics" / "claim_action_rubric.prompty"
    harness = tmp_path / "copilot-harness" / "instructions.prompty"
    assert module._prompt_name(rubric, "name: ignored\n") == "claim_action_rubric"
    assert module._prompt_name(harness, "") == "northwind-copilot-harness"


def test_dry_run_lists_every_insurance_prompt_and_nothing_from_the_agents_root(monkeypatch, capsys):
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    assert module.main() == 0
    out = capsys.readouterr().out
    assert "[dry-run] would publish" in out
    listed = [line[len("  - "):].strip() for line in out.splitlines() if line.startswith("  - ")]
    insurance = sorted(str(path.relative_to(module.REPO_ROOT)) for path in (module.REPO_ROOT / "agents" / "insurance").rglob("*.yaml"))
    assert insurance and set(insurance) <= set(listed)
    assistants = Path("agents", "insurance", "assistants")
    assert {str(assistants / "concierge.yaml"), str(assistants / "help-center.yaml")} <= set(listed)
    sources = {Path("agents", "insurance"), assistants, Path("agents", "copilot-harness"), Path("evals", "evaluators", "rubrics")}
    assert {Path(item).parent for item in listed} <= sources
