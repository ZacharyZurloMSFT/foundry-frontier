"""Deployment configuration contracts, runnable without installing the host."""
import io
import zipfile

import pytest
import yaml

from agents.hosted.config import AGENTS_DIR, Settings

ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://existing.services.ai.azure.com/api/projects/northwind-orders",
    "MODEL_DEPLOYMENT_NAME": "existing-baseline",
    "ORDER_AGENT_ROLE": "policylookup",
    "ORDERS_KB_CONNECTION": "northwind-orders-kb-mcp",
    "AZURE_SEARCH_ENDPOINT": "https://existing.search.windows.net",
    "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-kb",
}


def test_migrated_knowledge_uses_project_connection_without_search_credentials():
    config = Settings.from_env(ENV)
    spec = config.knowledge_spec()
    assert spec["project_connection_id"] == "northwind-orders-kb-mcp"
    assert spec["allowed_tools"] == ["knowledge_base_retrieve"]
    assert spec["url"] == ("https://existing.search.windows.net/knowledgebases/"
                           "northwind-kb/mcp?api-version=2026-05-01-preview")
    assert "headers" not in spec


@pytest.mark.parametrize("role", ["manager", "triage", "orderlookup", "policylookup", "resolutiondraft"])
def test_source_deployment_model_env_supported_by_every_role(role):
    config = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": role})
    assert config.model == "existing-baseline"
    assert config.model_route == "baseline"
    definition = AGENTS_DIR / ("orchestrator" if role == "manager" else role) / "agent.yaml"
    variables = yaml.safe_load(definition.read_text())["environment_variables"]
    assert any(v["name"] == "MODEL_DEPLOYMENT_NAME" for v in variables)
    assert all(not v["name"].startswith("FOUNDRY_") for v in variables)


def test_promoted_triage_override_wins_without_changing_other_agents():
    env = {**ENV, "TRIAGE_MODEL_DEPLOYMENT": "northwind-orders-triage-ft-v1"}
    triage = Settings.from_env({**env, "ORDER_AGENT_ROLE": "triage"})
    assert triage.model == "northwind-orders-triage-ft-v1"
    assert triage.model_route == "triage_override"
    assert Settings.from_env(env).model == "existing-baseline"


def test_original_nonreserved_aliases_remain_supported():
    env = {**ENV, "ORDERS_MODEL_DEPLOYMENT": "legacy-baseline",
           "ORDERS_KB_CONNECTION_NAME": "legacy-connection"}
    assert Settings.from_env(env).model == "existing-baseline"
    assert Settings.from_env(env).knowledge_spec()["project_connection_id"] == "northwind-orders-kb-mcp"
    env.pop("MODEL_DEPLOYMENT_NAME")
    env.pop("ORDERS_KB_CONNECTION")
    config = Settings.from_env(env)
    assert config.model == "legacy-baseline"
    assert config.knowledge_spec()["project_connection_id"] == "legacy-connection"


def test_unrelated_environment_secrets_are_not_copied_into_configuration():
    config = Settings.from_env({**ENV, "NOT_A_RUNTIME_SETTING": "test-only-sentinel"})
    assert "NOT_A_RUNTIME_SETTING" not in config.env


def test_source_zip_contract_contains_entrypoint_dependencies_and_canonical_prompts():
    files = {
        "main.py": AGENTS_DIR / "orchestrator" / "main.py",
        "requirements.txt": AGENTS_DIR / "hosted" / "requirements.txt",
        "agents/__init__.py": AGENTS_DIR / "__init__.py",
    }
    for name in ("__init__", "config", "main", "runtime", "scope", "tools"):
        files[f"agents/hosted/{name}.py"] = AGENTS_DIR / "hosted" / f"{name}.py"
    for name in ("triage", "tech_lookup", "manual_retrieval", "work_order_draft"):
        files[f"agents/{name}.yaml"] = AGENTS_DIR / f"{name}.yaml"
    files["agents/orchestrator/instructions.yaml"] = AGENTS_DIR / "orchestrator" / "instructions.yaml"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for destination, source in files.items():
            archive.writestr(destination, source.read_bytes())
    with zipfile.ZipFile(buffer) as archive:
        assert len(archive.namelist()) == 14
        assert b"from agents.hosted.main import main" in archive.read("main.py")
        assert b"agent-framework-foundry-hosting==1.0.0b260918" in archive.read("requirements.txt")
        assert not any(name.startswith(("tests/", ".venv/", ".env")) for name in archive.namelist())
        for name, source in files.items():
            assert archive.read(name) == source.read_bytes()
            if name.endswith(".py"):
                compile(archive.read(name), name, "exec")
