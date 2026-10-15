import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bundle_insurance_assets", ROOT / "scripts" / "bundle-insurance-assets.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.fixture
def staged_files(monkeypatch):
    staged = {}
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module.shutil, "copyfile",
        lambda source, destination: staged.__setitem__(destination, source.read_bytes()),
    )
    return staged


@pytest.mark.parametrize("service", module.SERVICES)
def test_only_canonical_assets_enter_the_selected_service_build_context(service, staged_files):
    result = module.bundle(service)
    app = ROOT / "apps" / service / "app"
    fixture_destination = app / "_northwind" / "insurance-fixtures.json" if service == "api" else app / "insurance-fixtures.json"
    expected = {
        fixture_destination: (ROOT / "data" / "northwind" / "insurance-fixtures.json").read_bytes()
    }
    if service != "api":
        expected[app / "insurance_domain.py"] = (ROOT / "apps" / "api" / "app" / "insurance_domain.py").read_bytes()
    else:
        manifest = ROOT / "data" / "northwind" / "departments" / "claims-warranty" / "claims-documents.json"
        expected[app / "_northwind" / "departments" / "claims-warranty" / manifest.name] = manifest.read_bytes()
        for name in module.CLAIMS_DOCUMENTS:
            expected[app / "_northwind" / "claims-documents" / name] = (ROOT / "data" / "seed" / "generated" / name).read_bytes()
    assert staged_files == expected
    assert all(destination.is_relative_to(ROOT / "apps" / service) for destination in staged_files)
    assert set(result) == {"service", "fixture_sha256", "domain_sha256"}
    assert module.bundle(service) == result


@pytest.mark.parametrize("service", ["web-tech", "cu-ingest", "../api", ""])
def test_unknown_or_retired_services_cannot_be_packaged(service, staged_files):
    with pytest.raises(ValueError, match="only be staged"):
        module.bundle(service)
    assert not staged_files


@pytest.mark.parametrize("source", ["insurance-fixtures.json", "insurance_domain.py"])
def test_missing_canonical_input_fails_before_copying_any_files(source, monkeypatch, staged_files):
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == source:
            raise FileNotFoundError(source)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(FileNotFoundError):
        module.bundle("mcp-tech-lookup")
    assert not staged_files


@pytest.mark.parametrize("invalid", ["{", "[]", "{}", '{"synthetic":false,"business_domain":"insurance"}'])
def test_non_synthetic_or_malformed_fixture_fails_closed(invalid, monkeypatch, staged_files):
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: invalid if path.name == "insurance-fixtures.json"
        else original(path, *args, **kwargs),
    )
    with pytest.raises(ValueError):
        module.bundle("api")
    assert not staged_files


@pytest.mark.parametrize("collection", ["policyholders", "policies", "claims", "policy_wordings"])
def test_incomplete_fixture_fails_before_staging(collection, monkeypatch, staged_files):
    original = Path.read_text
    fixture = json.loads((ROOT / "data" / "northwind" / "insurance-fixtures.json").read_text())
    fixture[collection] = []
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: json.dumps(fixture) if path.name == "insurance-fixtures.json"
        else original(path, *args, **kwargs),
    )
    with pytest.raises(ValueError, match="nonempty " + collection):
        module.bundle("mcp-work-order")
    assert not staged_files


def test_invalid_shared_code_fails_before_staging(monkeypatch, staged_files):
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: "def invalid(" if path.name == "insurance_domain.py"
        else original(path, *args, **kwargs),
    )
    with pytest.raises(SyntaxError):
        module.bundle("mcp-work-order")
    assert not staged_files


def test_missing_cu_source_fails_before_any_api_asset_is_staged(monkeypatch, staged_files):
    original = Path.read_bytes
    def read_bytes(path):
        if path.name == "nw-fnol-demo-2001.pdf":
            raise FileNotFoundError(path.name)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(FileNotFoundError):
        module.bundle("api")
    assert not staged_files


def test_api_and_mcp_prepackage_hooks_stage_assets_before_their_own_docker_build():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text())
    for service in module.SERVICES:
        hooks = config["services"][service]["hooks"]["prepackage"]
        for platform in ("windows", "posix"):
            command = hooks[platform]["run"]
            assert f"python ../../scripts/bundle-insurance-assets.py --service {service}" in command
            if platform == "windows":
                assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in command
            else:
                assert "set -e" in command
