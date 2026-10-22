"""AI Gateway consistency check of validate-reuse.py (offline, via the module's existing entry points)."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("validate_reuse_gateway", ROOT / "scripts" / "validate-reuse.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

ENDPOINT = "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/"
MANAGER = "aisvc-zurlo-northwind/gpt-5-4"
SPECIALIST = "aisvc-zurlo-northwind/gpt-4o"
LOOKUP = "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp"
KNOWLEDGE = "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-knowledge/mcp"
ROUTES = {
    "orderlookup": {"url": LOOKUP, "tool_prefix": "lookup_", "connection": "nw-insurance-claims-gateway-mcp"},
    "policylookup": {"url": KNOWLEDGE, "tool_prefix": "knowledge_", "connection": "nw-insurance-claims-gateway-kb"},
}


def gateway_environment(**overrides):
    environment = {
        "AI_GATEWAY_REQUIRED": "true",
        "ORDERS_MODEL_GATEWAY_ENDPOINT": ENDPOINT,
        "ORDERS_GATEWAY_MANAGER_MODEL": MANAGER,
        "ORDERS_GATEWAY_SPECIALIST_MODEL": SPECIALIST,
    }
    environment.update(overrides)
    return environment


def mcp_environment(routes=ROUTES, **overrides):
    return gateway_environment(AI_GATEWAY_MCP_REQUIRED="true", ORDERS_MCP_GATEWAY=json.dumps(routes), **overrides)


@pytest.mark.parametrize("environment", [
    {},
    {"AI_GATEWAY_REQUIRED": ""},
    {"AI_GATEWAY_REQUIRED": "false"},
    {"AI_GATEWAY_REQUIRED": "0", "AI_GATEWAY_MCP_REQUIRED": "no"},
    # Garbage gateway settings are ignored while the flags are off.
    {"ORDERS_MODEL_GATEWAY_ENDPOINT": "http://bad", "ORDERS_GATEWAY_MANAGER_MODEL": "gpt-5.4",
     "ORDERS_MCP_GATEWAY": "not json"},
])
def test_gateway_check_is_a_no_op_unless_a_flag_is_enabled(environment):
    original = dict(environment)
    assert module.validate_gateway(environment) == []
    assert module.validate_environment(environment) == []
    assert environment == original


def test_existing_checks_are_unchanged_and_come_first():
    invalid_mode = {"NORTHWIND_REUSE_EXISTING_RESOURCES": "yes"}
    errors = module.validate_environment(invalid_mode)
    assert errors == module.validate_reuse_environment(invalid_mode)
    assert errors and "exactly true or false" in errors[0]
    combined = module.validate_environment({**invalid_mode, "AI_GATEWAY_REQUIRED": "true"})
    assert combined[0] == errors[0] and len(combined) > len(errors)


@pytest.mark.parametrize("flag", ["true", "1", "yes", "TRUE", " yes "])
def test_a_consistent_gateway_environment_passes(flag):
    assert module.validate_gateway(gateway_environment(AI_GATEWAY_REQUIRED=flag)) == []
    assert module.validate_environment(gateway_environment(AI_GATEWAY_REQUIRED=flag)) == []


def test_models_are_checked_independently_of_the_reuse_mode():
    assert module.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": "false", **gateway_environment()}) == []
    errors = module.validate_environment({"AI_GATEWAY_REQUIRED": "true"})
    assert len(errors) == 3
    assert any("ORDERS_MODEL_GATEWAY_ENDPOINT" in error for error in errors)
    assert any("ORDERS_GATEWAY_MANAGER_MODEL" in error for error in errors)
    assert any("ORDERS_GATEWAY_SPECIALIST_MODEL" in error for error in errors)


@pytest.mark.parametrize("endpoint", [
    "",
    "http://ai-gateway-northwind.azure-api.net/default/models/openai/v1/",
    "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1",
    "https://ai-gateway-northwind.azure-api.net/default/models/",
    "https://user:secret@ai-gateway-northwind.azure-api.net/default/models/openai/v1/",
    "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/?api-version=preview",
    "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/#fragment",
    " https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/",
    "https://[",
    "not a url",
])
def test_endpoint_must_be_a_plain_https_openai_v1_url(endpoint):
    errors = module.validate_gateway(gateway_environment(ORDERS_MODEL_GATEWAY_ENDPOINT=endpoint))
    assert len(errors) == 1 and "ORDERS_MODEL_GATEWAY_ENDPOINT" in errors[0] and "/openai/v1/" in errors[0]


@pytest.mark.parametrize("key", ["ORDERS_GATEWAY_MANAGER_MODEL", "ORDERS_GATEWAY_SPECIALIST_MODEL"])
@pytest.mark.parametrize("alias", ["", "gpt-5.4", "gpt-5-4", "aisvc-zurlo-northwind/gpt-5.4",
                                   "aisvc-zurlo-northwind/GPT-5-4", "a/b/c", "/gpt-4o", "provider/", "pro vider/gpt"])
def test_manager_and_specialist_must_be_gateway_aliases(key, alias):
    errors = module.validate_gateway(gateway_environment(**{key: alias}))
    assert len(errors) == 1 and key in errors[0] and "<provider>/<registration>" in errors[0]


@pytest.mark.parametrize("alias", ["aisvc-zurlo-northwind/gpt-4o", "aisvc-zurlo-northwind/kimi-k2-6", "p.q_r/gpt-5-6-luna"])
def test_registered_aliases_are_accepted(alias):
    assert module.validate_gateway(gateway_environment(
        ORDERS_GATEWAY_MANAGER_MODEL=alias, ORDERS_GATEWAY_SPECIALIST_MODEL=alias,
        ORDERS_MODEL_GATEWAY_MODEL=alias)) == []


def test_per_agent_alias_is_optional_but_must_be_well_formed_when_present():
    assert module.validate_gateway(gateway_environment(ORDERS_MODEL_GATEWAY_MODEL="")) == []
    errors = module.validate_gateway(gateway_environment(ORDERS_MODEL_GATEWAY_MODEL="gpt-4o"))
    assert len(errors) == 1 and "ORDERS_MODEL_GATEWAY_MODEL" in errors[0]


@pytest.mark.parametrize("flag", ["ture", "enabled", "2", "on"])
@pytest.mark.parametrize("name", ["AI_GATEWAY_REQUIRED", "AI_GATEWAY_MCP_REQUIRED"])
def test_a_flag_typo_is_an_error_not_a_silent_disable(name, flag):
    errors = module.validate_gateway({name: flag})
    assert errors == [f"{name} must be true, 1 or yes to enable (false, 0, no, off or empty to disable)."]


@pytest.mark.parametrize("flag", ["", "false", "0", "no", "off", " OFF "])
@pytest.mark.parametrize("name", ["AI_GATEWAY_REQUIRED", "AI_GATEWAY_MCP_REQUIRED"])
def test_every_disabled_spelling_the_hosted_runtime_accepts_is_accepted(name, flag):
    assert module.validate_gateway({name: flag}) == []


def test_valid_mcp_routes_pass():
    assert module.validate_gateway(mcp_environment()) == []
    assert module.validate_gateway({"AI_GATEWAY_MCP_REQUIRED": "true", "ORDERS_MCP_GATEWAY": json.dumps(ROUTES)}) == []


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '"text"', "{}"])
def test_mcp_routes_must_be_a_non_empty_json_object(raw):
    errors = module.validate_gateway({"AI_GATEWAY_MCP_REQUIRED": "true", "ORDERS_MCP_GATEWAY": raw})
    assert len(errors) == 1 and "JSON object keyed by agent role" in errors[0]


def test_a_missing_routes_variable_fails_when_mcp_is_required():
    errors = module.validate_gateway({"AI_GATEWAY_MCP_REQUIRED": "true"})
    assert len(errors) == 1 and "ORDERS_MCP_GATEWAY" in errors[0]


@pytest.mark.parametrize("url", [
    "http://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp",
    "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp/",
    "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup",
    "https://ai-gateway-northwind.azure-api.net/default/toolservers//mcp",
    "https://ai-gateway-northwind.azure-api.net/toolservers/northwind-insurance-lookup/mcp",
    "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/",
    "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp?code=1",
    "https://user:secret@ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp",
    "https://ca-insurance-lookup.example.net/mcp/",
    "",
    None,
    7,
])
def test_mcp_urls_must_be_gateway_tool_server_endpoints(url):
    routes = {**ROUTES, "orderlookup": {**ROUTES["orderlookup"], "url": url}}
    errors = module.validate_gateway(mcp_environment(routes))
    assert len(errors) == 1 and "ORDERS_MCP_GATEWAY[orderlookup].url" in errors[0]


def test_a_route_that_is_not_an_object_is_rejected():
    errors = module.validate_gateway(mcp_environment({"orderlookup": LOOKUP}))
    assert len(errors) == 1 and "ORDERS_MCP_GATEWAY[orderlookup].url" in errors[0]


def test_mcp_routes_are_checked_even_when_models_are_not_required():
    environment = {"AI_GATEWAY_MCP_REQUIRED": "true", "ORDERS_MCP_GATEWAY": json.dumps({"orderlookup": {"url": "x"}})}
    assert len(module.validate_gateway(environment)) == 1


@pytest.mark.parametrize("service", ["orderlookup", "policylookup"])
def test_the_deployed_lookup_service_needs_its_own_route(service):
    other = "policylookup" if service == "orderlookup" else "orderlookup"
    assert module.validate_gateway(mcp_environment(), service) == []
    errors = module.validate_gateway(mcp_environment({other: ROUTES[other]}), service)
    assert len(errors) == 1 and f"no {service} entry" in errors[0]
    assert module.validate_environment(mcp_environment({other: ROUTES[other]}), service) == errors


@pytest.mark.parametrize("service", ["orchestrator", "triage", "resolutiondraft", None])
def test_other_services_do_not_need_a_route_of_their_own(service):
    assert module.validate_gateway(mcp_environment({"orderlookup": ROUTES["orderlookup"]}), service) == []


def test_main_reports_gateway_errors_and_fails(monkeypatch, capsys):
    environment = gateway_environment(ORDERS_GATEWAY_MANAGER_MODEL="gpt-5.4")
    original = dict(environment)
    monkeypatch.setattr(module.os, "environ", environment)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert "ORDERS_GATEWAY_MANAGER_MODEL" in captured.err and not captured.out
    assert environment == original


def test_main_passes_silently_for_a_consistent_gateway_environment(monkeypatch, capsys):
    monkeypatch.setattr(module.os, "environ", mcp_environment())
    assert module.main() == 0
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_main_checks_the_route_of_the_agent_service_being_deployed(monkeypatch, capsys):
    monkeypatch.setattr(module.os, "environ", mcp_environment({"policylookup": ROUTES["policylookup"]}))
    assert module.main("orderlookup") == 1
    assert "no orderlookup entry" in capsys.readouterr().err
    monkeypatch.setattr(module.os, "environ", mcp_environment())
    assert module.main("orderlookup") == 0
