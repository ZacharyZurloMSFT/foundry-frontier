"""Gateway cutover contract for the azd template: outputs only change when a gateway is configured."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("validate_reuse_gateway", ROOT / "scripts" / "validate-reuse.py")
validator = importlib.util.module_from_spec(_spec)
sys.modules["validate_reuse_gateway"] = validator
_spec.loader.exec_module(validator)

GATEWAY = "https://apim-northwind.azure-api.net"


@pytest.fixture(scope="module")
def template():
    return json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))


def bicep_binary():
    found = shutil.which("bicep")
    if found:
        return found
    installed = Path.home() / ".azure" / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
    if not installed.is_file():
        pytest.skip("Bicep is not installed")
    return str(installed)


def compile_parameters(values):
    source = ROOT / "infra" / "main.bicepparam"
    keys = re.findall(r"readEnvironmentVariable\('([^']+)'", source.read_text(encoding="utf-8"))
    env = {key: value for key, value in os.environ.items() if key not in keys}
    env.update({"AZURE_ENV_NAME": "synthetic-gateway-test", **values})
    result = subprocess.run([bicep_binary(), "build-params", str(source), "--stdout"], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    compiled = json.loads(result.stdout)
    if "parametersJson" in compiled:
        compiled = json.loads(compiled["parametersJson"])
    return compiled["parameters"]


def test_gateway_is_off_by_default_and_the_stack_is_constrained(template):
    assert template["parameters"]["gatewayUrl"]["defaultValue"] == ""
    assert template["parameters"]["gatewayStack"]["defaultValue"] == "northwind"
    assert template["parameters"]["gatewayStack"]["allowedValues"] == ["northwind", "insurance"]
    assert template["variables"]["gatewayBase"] == (
        "[if(empty(parameters('gatewayUrl')), '', format('{0}/{1}', parameters('gatewayUrl'), parameters('gatewayStack')))]"
    )


@pytest.mark.parametrize("output,suffix,reuse_setting,greenfield_output", [
    ("API_URI", None, "API_URI", "apiUri"),
    ("MCP_TECH_LOOKUP_URL", "tech-lookup", "MCP_TECH_LOOKUP_URL", "mcpTechLookupUrl"),
    ("MCP_WORK_ORDER_URL", "work-order", "MCP_WORK_ORDER_URL", "mcpWorkOrderUrl"),
])
def test_outputs_follow_the_gateway_only_when_it_is_set(template, output, suffix, reuse_setting, greenfield_output):
    value = template["outputs"][output]["value"]
    expected = "variables('gatewayBase')" if suffix is None else f"format('{{0}}/mcp/{suffix}', variables('gatewayBase'))"
    assert value.startswith(f"[if(not(empty(variables('gatewayBase'))), {expected}, if(variables('reuseExisting'), ")
    assert f"outputs.settings.value.{reuse_setting}" in value, "reuse mode keeps the adopted Container App URL"
    assert f"outputs.{greenfield_output}.value" in value, "greenfield keeps the Container Apps module URL"


def test_other_service_urls_are_never_rewritten(template):
    for output in ("WEB_TECH_URI", "WEB_VOICE_URI"):
        assert "gatewayBase" not in template["outputs"][output]["value"]


def test_gateway_choice_is_visible_in_azd_outputs(template):
    assert template["outputs"]["NORTHWIND_GATEWAY_URL"]["value"] == "[parameters('gatewayUrl')]"
    assert template["outputs"]["NORTHWIND_GATEWAY_STACK"]["value"] == "[parameters('gatewayStack')]"


def test_parameters_come_from_the_azd_environment():
    defaults = compile_parameters({})
    assert defaults["gatewayUrl"]["value"] == "" and defaults["gatewayStack"]["value"] == "northwind"
    configured = compile_parameters({"NORTHWIND_GATEWAY_URL": GATEWAY, "NORTHWIND_GATEWAY_STACK": "insurance"})
    assert configured["gatewayUrl"]["value"] == GATEWAY and configured["gatewayStack"]["value"] == "insurance"


@pytest.mark.parametrize("value", ["", GATEWAY, "https://apim-other-01.azure-api.net"])
def test_valid_gateway_origins_are_accepted_in_both_modes(value):
    for reuse in ("false", "true"):
        errors = validator.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": reuse, "NORTHWIND_GATEWAY_URL": value})
        assert not [error for error in errors if "GATEWAY" in error]


@pytest.mark.parametrize("value", [
    f"{GATEWAY}/", f"{GATEWAY}/northwind", f"{GATEWAY}?x=1", "http://apim-northwind.azure-api.net",
    "https://apim-northwind.example.com", "https://evil.example/azure-api.net", f" {GATEWAY}",
    "https://APIM-NORTHWIND.azure-api.net", "https://apim-northwind.azure-api.net:8443",
])
def test_malformed_gateway_origins_are_rejected_before_any_change(value):
    errors = validator.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": "false", "NORTHWIND_GATEWAY_URL": value})
    assert errors == ["NORTHWIND_GATEWAY_URL must be a bare https://<name>.azure-api.net origin with no path, query, or trailing slash."]


@pytest.mark.parametrize("stack,valid", [("northwind", True), ("insurance", True), ("", False), ("retail", False), ("Insurance", False)])
def test_gateway_stack_must_be_a_known_stack(stack, valid):
    errors = validator.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": "false", "NORTHWIND_GATEWAY_STACK": stack})
    assert (errors == []) is valid


def test_spa_builds_use_the_gateway_only_when_configured():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-apps.yml").read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    assert deploy["env"]["NORTHWIND_GATEWAY_URL"] == "${{ vars.NORTHWIND_GATEWAY_URL }}"
    run = next(step["run"] for step in deploy["steps"] if "VITE_API_BASE_URL" in step.get("run", ""))
    assert "${{" not in run
    assert r"^https://[a-z0-9-]+\.azure-api\.net$" in run, "the gateway origin is validated before it is baked into a build"
    assert run.index('[[ "$NORTHWIND_GATEWAY_URL" =~') < run.index('export VITE_API_BASE_URL="$NORTHWIND_GATEWAY_URL/northwind"')
    assert 'export VITE_API_BASE_URL="https://$api_fqdn"' in run, "the direct Container App URL remains the default"
    assert run.index("NORTHWIND_GATEWAY_URL") < run.index("api_fqdn")


def _insurance_spa_build(workflow):
    return next(step["run"] for step in workflow["jobs"]["deploy"]["steps"]
                if step.get("name", "").startswith("Build and push immutable images"))


def test_insurance_spas_are_built_against_the_gateway_only_when_configured():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-apps.yml").read_text(encoding="utf-8"))
    run = _insurance_spa_build(workflow)
    assert "${{" not in run
    gateway = run.index('[[ "$NORTHWIND_GATEWAY_URL" =~')
    assert r"^https://[a-z0-9-]+\.azure-api\.net$" in run, "the gateway origin is validated before it is baked in"
    assert gateway < run.index('insurance_base="$NORTHWIND_GATEWAY_URL/insurance"')
    assert 'insurance_base="https://$insurance_fqdn"' in run, "the direct insurance API URL remains the default"
    assert run.index("NORTHWIND_GATEWAY_URL") < run.index("insurance_fqdn=")
    # Both SPAs take the same base URL, and neither is built against the northwind stack.
    assert run.count('VITE_API_BASE_URL="$insurance_base"') == 2
    assert 'VITE_API_BASE_URL="$NORTHWIND_GATEWAY_URL/northwind"' not in run
    assert "VITE_API_BASE_URL=\"https://$api_fqdn\"" not in run


def test_every_insurance_spa_is_snapshotted_built_and_released_by_the_same_pairs():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-apps.yml").read_text(encoding="utf-8"))
    steps = {step.get("name", ""): step["run"] for step in workflow["jobs"]["deploy"]["steps"] if "run" in step}
    snapshot = next(run for name, run in steps.items() if name.startswith("Snapshot"))
    release = next(run for name, run in steps.items() if name.startswith("Release digest-pinned"))
    pairs = "web-tech:insurance-portal web-voice:insurance-voice"
    assert f"for pair in {pairs}; do" in snapshot and f"for pair in {pairs}; do" in release
    build = _insurance_spa_build(workflow)
    for app in ("insurance-portal", "insurance-voice"):
        assert f'".release-state/build-{app}.json"' in build and f'".release-state/image-{app}"' in build
    # The release loop keeps the current-main guard before every mutation.
    loop = release[release.index(f"for pair in {pairs}"):]
    assert loop.index("|| continue") < loop.index('commits/main') < loop.index("release-app.py")
