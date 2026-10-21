import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "orders_hosted_deploy", Path(__file__).parents[1] / "deploy-orders-hosted.py"
)
sys.path.insert(0, str(Path(__file__).parents[1]))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


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
    shared.with_name("insurance_config.py").write_text("# canonical shared configuration\n", encoding="utf-8")
    (root / ".env").write_text("DO_NOT_PACKAGE=fixture\n", encoding="utf-8")
    (hosted / ".env").write_text("DO_NOT_PACKAGE=fixture\n", encoding="utf-8")
    (root / "agents" / "triage.yaml").write_text("name: northwind-triage\n", encoding="utf-8")
    hidden = root / "agents" / ".venv" / "Lib"
    hidden.mkdir(parents=True)
    (hidden / "package.py").write_text("pass\n", encoding="utf-8")
    requirements = root / "agents" / "requirements.txt"
    requirements.write_text("azure-ai-projects==2.3.0\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    assert module.bundle(root, requirements, first) == module.bundle(root, requirements, second)
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {
            "main.py", "requirements.txt", "agents/insurance/domain.py",
            "agents/insurance/config.py", "agents/hosted/build.json", *module.PACKAGE_SOURCES,
        }
        assert archive.read("agents/insurance/domain.py") == shared.read_bytes()


def test_dry_run_preserves_submitted_journal(tmp_path):
    output = tmp_path / "candidate.json"
    contents = json.dumps({"submission_started": True, "version": "1"})
    output.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="must not overwrite"):
        module.deploy(SimpleNamespace(output=output, apply=False))
    assert output.read_text(encoding="utf-8") == contents


def test_uncertain_submission_cannot_replace_its_original_source_archive(tmp_path):
    output = tmp_path / "insurance.json"
    output.write_text(json.dumps({"submission_started": True, "source_sha256": "0" * 64}), encoding="utf-8")
    archive = output.with_suffix(".zip")
    archive.write_bytes(b"preserve the originally submitted bytes")
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="source differs"):
        module.deploy(SimpleNamespace(
            output=output, apply=True, requirements=root / "agents" / "hosted" / "requirements.txt",
        ))
    assert archive.read_bytes() == b"preserve the originally submitted bytes"
    assert not output.with_suffix(".pending.zip").exists()


def test_insurance_dry_run_uses_canonical_bundle_and_never_sets_readiness_or_roles(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"common": {"ORDERS_MODEL_DEPLOYMENT": "baseline"}}), encoding="utf-8")
    output = tmp_path / "insurance-deploy.json"
    monkeypatch.setattr(module, "AzureCliCredential", lambda **_: pytest.fail("Dry run cannot authenticate"))
    module.deploy(SimpleNamespace(
        output=output, apply=False, config=config, role="manager", agent_name=None,
        business_domain="insurance", project_endpoint="https://existing.invalid/api/projects/nw-insurance-claims",
        requirements=root / "agents" / "hosted" / "requirements.txt", grant_runtime_roles=False,
    ))
    report = json.loads(output.read_text())
    assert report["agent"] == "northwind-insurance-orchestrator"
    assert report["agent_runtime_ready"] is False and report["roles_modified"] is False
    with zipfile.ZipFile(output.with_suffix(".zip")) as archive:
        assert b"northwind-insurance-orchestrator" in archive.read("agents/insurance/orchestrator.yaml")
        assert b"def lookup_claim_record" in archive.read("agents/insurance/domain.py")


def test_deployment_rejects_fabricated_fleet_readiness(tmp_path):
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"common": {"ORDERS_MODEL_DEPLOYMENT": "baseline",
                                            "NORTHWIND_INSURANCE_AGENTS_READY": "true"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot declare API fleet readiness"):
        module.deploy(SimpleNamespace(
            output=tmp_path / "not-ready.json", apply=False, config=config, role="manager", agent_name=None,
            business_domain="insurance", requirements=root / "agents" / "hosted" / "requirements.txt",
        ))


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
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"common": {"ORDERS_MODEL_DEPLOYMENT": "baseline"}}), encoding="utf-8")
    output = tmp_path / "azd-dry-run.json"
    monkeypatch.setattr(module, "deployment_credential", lambda *_: pytest.fail("Dry run cannot authenticate"))
    module.deploy(SimpleNamespace(
        output=output, apply=False, config=config, role="manager", agent_name=None,
        business_domain="insurance", credential_source="azd",
        project_endpoint="https://existing.invalid/api/projects/nw-insurance-claims",
        requirements=root / "agents" / "hosted" / "requirements.txt", grant_runtime_roles=False,
    ))
    report = json.loads(output.read_text())
    assert report["credential_source"] == "azd" and report["roles_modified"] is False


def test_insurance_wording_deployment_rejects_retail_knowledge(tmp_path):
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "retail-knowledge.json"
    config.write_text(json.dumps({"common": {
        "ORDERS_MODEL_DEPLOYMENT": "baseline", "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-kb",
        "AZURE_SEARCH_INDEX": "northwind-knowledge", "ORDERS_KB_CONNECTION": "northwind-orders-kb-mcp",
    }}), encoding="utf-8")
    with pytest.raises(ValueError, match="no retail fallback"):
        module.deploy(SimpleNamespace(
            output=tmp_path / "not-deployed.json", apply=False, config=config,
            role="policylookup", agent_name=None, business_domain="insurance", credential_source="azd",
            project_endpoint="https://existing.invalid/api/projects/nw-insurance-claims",
            requirements=root / "agents" / "hosted" / "requirements.txt",
        ))
