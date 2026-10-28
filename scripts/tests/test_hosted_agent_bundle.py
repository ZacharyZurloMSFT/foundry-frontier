import importlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "hosted_agents_deploy", Path(__file__).parents[1] / "deploy-hosted-agents.py"
)
sys.path.insert(0, str(Path(__file__).parents[1]))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
# The runtime's own tables; config.py needs only PyYAML, so it imports without the agent framework.
sys.path.insert(0, str(ROOT))
runtime = importlib.import_module("agents.hosted.config")

PROJECT = "https://existing.invalid/api/projects/nw-insurance-claims"
REQUIREMENTS = ROOT / "agents" / "hosted" / "requirements.txt"


def deploy_arguments(tmp_path, config, **overrides):
    return SimpleNamespace(
        output=tmp_path / "deploy.json", apply=False, config=config, role="orchestrator",
        project_endpoint=PROJECT, requirements=REQUIREMENTS, grant_runtime_roles=False, **overrides)


def write_config(tmp_path, document):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    return config


def test_bundle_is_reproducible_and_excludes_local_configuration(tmp_path):
    root = tmp_path / "repo"
    hosted = root / "agents" / "hosted"
    hosted.mkdir(parents=True)
    for name in (*module.PACKAGE_SOURCES, "agents/orchestrator/main.py"):
        source = root.joinpath(*name.split("/"))
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# fixture\n", encoding="utf-8")
    shared = root / "apps" / "api" / "app" / "insurance_domain.py"
    shared.parent.mkdir(parents=True)
    shared.write_text("# canonical shared insurance implementation\n", encoding="utf-8")
    (shared.parent / "workflow_binding.py").write_text("# canonical workflow binding\n", encoding="utf-8")
    (root / ".env").write_text("DO_NOT_PACKAGE=fixture\n", encoding="utf-8")
    (hosted / ".env").write_text("DO_NOT_PACKAGE=fixture\n", encoding="utf-8")
    (root / "agents" / "unlisted.yaml").write_text("name: not-packaged\n", encoding="utf-8")
    hidden = root / "agents" / ".venv" / "Lib"
    hidden.mkdir(parents=True)
    (hidden / "package.py").write_text("pass\n", encoding="utf-8")
    requirements = root / "agents" / "requirements.txt"
    requirements.write_text("azure-ai-projects==2.3.0\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    assert module.bundle(root, requirements, first) == module.bundle(root, requirements, second)
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {"main.py", "requirements.txt", "agents/insurance/domain.py",
                                         "agents/insurance/workflow_binding.py", *module.PACKAGE_SOURCES}
        assert archive.read("agents/insurance/domain.py") == shared.read_bytes()


def test_the_package_sources_are_the_runtime_and_the_insurance_prompts_only():
    assert all(name.startswith(("agents/hosted/", "agents/insurance/")) or name in (
        "agents/__init__.py", "data/northwind/insurance-fixtures.json") for name in module.PACKAGE_SOURCES)
    assert {name for name in module.PACKAGE_SOURCES if name.endswith(".yaml")} == {
        "agents/" + path for path in runtime.PROMPTS.values()}
    for name in module.PACKAGE_SOURCES:
        assert (ROOT / name).is_file(), name


def test_the_deploy_tables_match_the_runtime_tables():
    assert set(module.ROLES) == set(runtime.PROMPTS)
    assert module.ROLE_ALIASES == runtime.ROLE_ALIASES
    assert module.SETTING_ALIASES == runtime.SETTING_ALIASES
    assert set(module.MCP_GATEWAY_ROLES) == set(runtime.AI_GATEWAY_MCP_ROLES)
    for role, name in module.ROLES.items():
        assert yaml.safe_load((ROOT / "agents" / runtime.PROMPTS[role]).read_text(encoding="utf-8"))["name"] == name
    specialists = {agent for agent, _ in runtime.SPECIALISTS.values()}
    assert specialists == {name for role, name in module.ROLES.items() if role != "orchestrator"}


def test_dry_run_preserves_submitted_journal(tmp_path):
    output = tmp_path / "candidate.json"
    contents = json.dumps({"submission_started": True, "version": "1"})
    output.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="must not overwrite"):
        module.deploy(SimpleNamespace(output=output, apply=False))
    assert output.read_text(encoding="utf-8") == contents


def test_insurance_dry_run_uses_canonical_bundle_and_never_sets_readiness_or_roles(tmp_path, monkeypatch):
    config = write_config(tmp_path, {"common": {"AGENT_MODEL_DEPLOYMENT": "baseline"}})
    monkeypatch.setattr(module, "AzureCliCredential", lambda **_: pytest.fail("Dry run cannot authenticate"))
    arguments = deploy_arguments(tmp_path, config)
    module.deploy(arguments)
    report = json.loads(arguments.output.read_text())
    assert report["agent"] == "northwind-insurance-orchestrator" and report["business_domain"] == "insurance"
    assert report["agent_runtime_ready"] is False and report["roles_modified"] is False
    with zipfile.ZipFile(arguments.output.with_suffix(".zip")) as archive:
        assert b"northwind-insurance-orchestrator" in archive.read("agents/insurance/orchestrator.yaml")
        assert b"def lookup_claim_record" in archive.read("agents/insurance/domain.py")


@pytest.mark.parametrize("connection", ["nw-insurance-claims-kb-mcp", "northwind-insurance-kb-mcp"])
@pytest.mark.parametrize("role", list(module.ROLES))
def test_every_role_deploys_under_its_insurance_agent_name(tmp_path, role, connection):
    config = write_config(tmp_path, {"common": {"AGENT_MODEL_DEPLOYMENT": "baseline",
                                                "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
                                                "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
                                                "AGENT_KB_CONNECTION": connection}})
    arguments = deploy_arguments(tmp_path, config)
    arguments.role = role
    module.deploy(arguments)
    assert json.loads(arguments.output.read_text())["agent"] == module.ROLES[role]
    assert module.ROLES[role].startswith("northwind-insurance-")


def test_deployment_rejects_fabricated_fleet_readiness(tmp_path):
    config = write_config(tmp_path, {"common": {"AGENT_MODEL_DEPLOYMENT": "baseline",
                                                "NORTHWIND_INSURANCE_AGENTS_READY": "true"}})
    with pytest.raises(ValueError, match="cannot declare API fleet readiness"):
        module.deploy(deploy_arguments(tmp_path, config))


@pytest.mark.parametrize("source,class_name", [("cli", "AzureCliCredential"), ("azd", "AzureDeveloperCliCredential")])
def test_deployment_identity_source_is_explicit_with_no_fallback(monkeypatch, source, class_name):
    calls = []
    sentinel = object()
    def selected(**kwargs):
        calls.append(kwargs)
        return sentinel
    other = "AzureCliCredential" if class_name == "AzureDeveloperCliCredential" else "AzureDeveloperCliCredential"
    monkeypatch.setattr(module, class_name, selected)
    monkeypatch.setattr(module, other, lambda **_: pytest.fail("No identity fallback is permitted"))
    assert module.deployment_credential(source, "tenant-fixture") is sentinel
    assert calls == [{"tenant_id": "tenant-fixture", "process_timeout": 120}]
    with pytest.raises(ValueError, match="credential source"):
        module.deployment_credential("auto", "tenant-fixture")


def test_azd_identity_dry_run_records_source_without_authentication(tmp_path, monkeypatch):
    config = write_config(tmp_path, {"common": {"AGENT_MODEL_DEPLOYMENT": "baseline"}})
    monkeypatch.setattr(module, "deployment_credential", lambda *_: pytest.fail("Dry run cannot authenticate"))
    arguments = deploy_arguments(tmp_path, config, credential_source="azd")
    module.deploy(arguments)
    report = json.loads(arguments.output.read_text())
    assert report["credential_source"] == "azd" and report["roles_modified"] is False


def test_insurance_wording_deployment_rejects_unrelated_knowledge(tmp_path):
    config = write_config(tmp_path, {"common": {
        "AGENT_MODEL_DEPLOYMENT": "baseline", "AZURE_SEARCH_KNOWLEDGE_BASE": "other-kb",
        "AZURE_SEARCH_INDEX": "other-knowledge", "AGENT_KB_CONNECTION": "other-kb-mcp",
    }})
    arguments = deploy_arguments(tmp_path, config, credential_source="azd")
    arguments.role = "policy-wording"
    with pytest.raises(ValueError, match="explicit insurance KB, index and connection"):
        module.deploy(arguments)


@pytest.mark.parametrize("model", [
    {"TRIAGE_MODEL_DEPLOYMENT": "northwind-insurance-triage-ft-v1"},
    {"TRIAGE_MODEL_DEPLOYMENT": "other-triage-v1", "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "true"},
    {"AGENT_MODEL_DEPLOYMENT": "baseline-ft-v1", "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "true"},
])
def test_a_triage_override_or_fine_tuned_model_must_be_a_verified_insurance_model(tmp_path, model):
    config = write_config(tmp_path, {"common": {"AGENT_MODEL_DEPLOYMENT": "baseline", **model}})
    arguments = deploy_arguments(tmp_path, config)
    arguments.role = "triage"
    with pytest.raises(ValueError, match="not an evaluated insurance model"):
        module.deploy(arguments)


# Deprecated names: the pre-cleanup role ids, config keys and settings keep working; canonical names win.

@pytest.mark.parametrize("legacy,canonical", list(module.ROLE_ALIASES.items()))
def test_deprecated_role_ids_deploy_exactly_like_the_canonical_role(tmp_path, legacy, canonical):
    common = {"AGENT_MODEL_DEPLOYMENT": "baseline", "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
              "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
              "AGENT_KB_CONNECTION": "northwind-insurance-kb-mcp"}
    reports = []
    for role, section in ((legacy, legacy), (canonical, canonical), (legacy, canonical), (canonical, legacy)):
        config = write_config(tmp_path, {"common": common, section: {"TRIAGE_MODEL_DEPLOYMENT": ""}})
        arguments = deploy_arguments(tmp_path, config)
        arguments.role = role
        module.deploy(arguments)
        reports.append(json.loads(arguments.output.read_text()))
        arguments.output.unlink()
    assert all(report == reports[0] for report in reports) and reports[0]["agent"] == module.ROLES[canonical]


def test_role_sections_accept_either_spelling_but_never_both():
    assert module.role_section({"orderlookup": {"A": "1"}}, "policy-claim-lookup") == {"A": "1"}
    assert module.role_section({"policy-claim-lookup": {"A": "2"}}, "policy-claim-lookup") == {"A": "2"}
    assert module.role_section({"policylookup": {"A": "1"}}, "policy-claim-lookup") == {}
    assert module.role_section({"common": {"A": "3"}}, "triage") == {}
    with pytest.raises(ValueError, match="more than one section"):
        module.role_section({"orderlookup": {}, "policy-claim-lookup": {}}, "policy-claim-lookup")


def test_canonical_role_accepts_both_spellings_and_rejects_anything_else():
    for legacy, canonical in module.ROLE_ALIASES.items():
        assert module.canonical_role(legacy) == canonical == module.canonical_role(canonical)
    for unknown in ("", "unknown-role", "Orchestrator", "claim_action_draft"):
        with pytest.raises(ValueError, match="Unknown role"):
            module.canonical_role(unknown)


@pytest.mark.parametrize("canonical,legacy", [(new, old) for new, olds in module.SETTING_ALIASES.items() for old in olds])
def test_the_environment_sent_to_the_agent_carries_canonical_names_only(canonical, legacy):
    assert module.canonical_environment({legacy: "old"}) == {canonical: "old"}
    assert module.canonical_environment({canonical: "new", legacy: "old"}) == {canonical: "new"}
    assert module.canonical_environment({canonical: " ", legacy: "old"}) == {canonical: "old"}
    assert module.canonical_environment({canonical: "new", "OTHER": "kept"}) == {canonical: "new", "OTHER": "kept"}
    assert module.setting({legacy: " old "}, canonical) == "old" and module.setting({}, canonical) == ""


def test_a_pre_cleanup_config_deploys_with_canonical_names_and_without_the_removed_flag(tmp_path, monkeypatch):
    document = {
        "common": {
            "ORDERS_MODEL_DEPLOYMENT": "baseline", "NORTHWIND_BUSINESS_DOMAIN": "insurance",
            "ORDERS_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/default/models/openai/v1/",
            "ORDERS_MODEL_GATEWAY_MODEL": "northwind/baseline", "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "baseline",
            "AI_GATEWAY_REQUIRED": "true", "AI_GATEWAY_MCP_REQUIRED": "true",
            "ORDERS_MCP_GATEWAY": json.dumps({"orderlookup": {"url": "https://gateway.example/default/toolservers/"
                                                                      "northwind-insurance-lookup/mcp"}}),
            "MCP_TECH_LOOKUP_URL": "https://lookup.example/mcp", "MCP_WORK_ORDER_URL": "https://review.example/mcp",
        },
        "orderlookup": {"ORDER_AGENT_ROLE": "ignored-in-favour-of-the-role-argument"},
    }
    monkeypatch.setattr(module, "deployment_credential", lambda *_: pytest.fail("Dry run cannot authenticate"))
    arguments = deploy_arguments(tmp_path, write_config(tmp_path, document))
    arguments.role = "orderlookup"
    module.deploy(arguments)
    report = json.loads(arguments.output.read_text())
    assert report["agent"] == "northwind-insurance-policy-claim-lookup"
    assert report["ai_gateway"] == {"models_required": True, "mcp_required": True}


def test_the_environment_in_the_hosted_definition_is_canonical(tmp_path, monkeypatch):
    document = {
        "common": {"ORDERS_MODEL_DEPLOYMENT": "baseline", "NORTHWIND_BUSINESS_DOMAIN": "insurance",
                   "MCP_TECH_LOOKUP_URL": "https://lookup.example/mcp", "ORDER_AGENT_ROLE": "manager"},
        "resolutiondraft": {"MCP_WORK_ORDER_URL": "https://review.example/mcp"},
    }
    config = write_config(tmp_path, document)
    seen = {}

    class Stop(Exception):
        pass

    def definition(**options):
        seen.update(options)
        raise Stop

    monkeypatch.setattr(module, "HostedAgentDefinition", definition)
    arguments = deploy_arguments(tmp_path, config, tenant="tenant", subscription="sub")
    arguments.role = "resolutiondraft"
    arguments.apply = True
    arguments.project_arm_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a/projects/nw-insurance-claims"
    with pytest.raises(Stop):
        module.deploy(arguments)
    assert seen["environment_variables"] == {
        "AGENT_MODEL_DEPLOYMENT": "baseline", "MCP_POLICY_LOOKUP_URL": "https://lookup.example/mcp",
        "AGENT_ROLE": "claim-action-draft", "MCP_CLAIM_REVIEW_URL": "https://review.example/mcp",
    }


@pytest.mark.parametrize("value", ["legacy", "other"])
def test_the_removed_domain_flag_cannot_select_another_domain(tmp_path, value):
    config = write_config(tmp_path, {"common": {"AGENT_MODEL_DEPLOYMENT": "baseline",
                                                "NORTHWIND_BUSINESS_DOMAIN": value}})
    with pytest.raises(ValueError, match="was removed"):
        module.deploy(deploy_arguments(tmp_path, config))


def test_the_removed_domain_and_companion_options_are_rejected(tmp_path, monkeypatch, capsys):
    required = ["--project-endpoint", PROJECT, "--project-arm-id", "x", "--tenant", "t", "--subscription", "s",
                "--requirements", str(REQUIREMENTS), "--config", str(tmp_path / "c.json"),
                "--output", str(tmp_path / "o.json")]
    for option in (["--business-domain", "insurance"], ["--agent-name", "northwind-insurance-triage"]):
        monkeypatch.setattr(sys, "argv", ["deploy-hosted-agents.py", "--role", "triage", *required, *option])
        with pytest.raises(SystemExit):
            module.main()
        assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("role", [*module.ROLES, *module.ROLE_ALIASES])
def test_the_cli_accepts_canonical_and_deprecated_role_ids(tmp_path, monkeypatch, role):
    captured = []
    monkeypatch.setattr(module, "deploy", captured.append)
    monkeypatch.setattr(sys, "argv", [
        "deploy-hosted-agents.py", "--role", role, "--project-endpoint", PROJECT, "--project-arm-id", "x",
        "--tenant", "t", "--subscription", "s", "--requirements", str(REQUIREMENTS),
        "--config", str(tmp_path / "c.json"), "--output", str(tmp_path / "o.json")])
    module.main()
    assert captured[0].role == role and module.canonical_role(captured[0].role) in module.ROLES
    assert not (tmp_path / "o.json.lock").exists()


def test_the_cli_rejects_an_unknown_role(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "deploy-hosted-agents.py", "--role", "unknown-role", "--project-endpoint", PROJECT, "--project-arm-id", "x",
        "--tenant", "t", "--subscription", "s", "--requirements", str(REQUIREMENTS),
        "--config", str(tmp_path / "c.json"), "--output", str(tmp_path / "o.json")])
    with pytest.raises(SystemExit):
        module.main()
