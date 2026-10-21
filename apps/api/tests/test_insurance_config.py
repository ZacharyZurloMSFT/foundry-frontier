import json

import httpx
import pytest
from app import company, insurance, main
from app.insurance_config import (
    ALIASES, INSURANCE_CONNECTION, configuration_hash, insurance_routing,
    normalize_environment, validate_insurance_connection,
)
from app.settings import Settings
from fastapi import HTTPException
from pydantic import ValidationError


def test_primary_configuration_and_legacy_aliases_are_equivalent_without_retaining_aliases():
    primary = {
        **insurance_routing(), "AGENT_ROLE": "policy_claim_lookup",
        "MODEL_DEPLOYMENT_NAME": "gpt-4o", "INSURANCE_KB_CONNECTION_NAME": INSURANCE_CONNECTION,
    }
    legacy = {
        next(iter(ALIASES.get(key, (key,)))): ("orderlookup" if key == "AGENT_ROLE" else value)
        for key, value in primary.items()
    }
    assert normalize_environment(legacy, warn=False) == primary
    assert configuration_hash(legacy) == configuration_hash(primary)
    assert normalize_environment({**primary, "ORDER_AGENT_ROLE": "orderlookup"}, warn=False) == primary


@pytest.mark.parametrize("primary,alias,new,old", [
    ("INSURANCE_PROJECT_NAME", "ORDER_PROJECT_NAME", "nw-insurance-claims", "northwind-orders"),
    ("INSURANCE_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT", "https://unit/one", "https://unit/two"),
    ("AGENTS_HOSTED", "ORDER_AGENTS_HOSTED", "true", "false"),
])
def test_api_rejects_conflicting_init_aliases(primary, alias, new, old):
    with pytest.raises(ValueError, match="Conflicting settings"):
        Settings(_env_file=None, **{primary: new, alias: old})


def test_api_rejects_conflicting_process_aliases_even_when_settings_source_prefers_primary(monkeypatch):
    monkeypatch.setenv("INSURANCE_PROJECT_NAME", "nw-insurance-claims")
    monkeypatch.setenv("ORDER_PROJECT_NAME", "northwind-orders")
    with pytest.raises((ValueError, ValidationError), match="Conflicting settings"):
        Settings(_env_file=None)


def test_connection_validates_target_and_category_not_just_insurance_looking_name():
    endpoint = "https://unit.search.windows.net"
    valid = {"properties": {
        "category": "RemoteTool",
        "target": endpoint + "/knowledgebases/northwind-insurance-kb/mcp?api-version=2026-05-01-preview",
    }}
    validate_insurance_connection(valid, endpoint)
    for changed in (
        {**valid["properties"], "category": "Other"},
        {**valid["properties"], "target": endpoint + "/knowledgebases/northwind-kb/mcp"},
    ):
        with pytest.raises(ValueError, match="canonical insurance RemoteTool"):
            validate_insurance_connection(changed, endpoint)


@pytest.mark.parametrize("change", ["retired", "missing", "wrong-account", "credentials", "unknown"])
def test_ready_guard_rejects_stale_or_incomplete_department_routes(monkeypatch, change):
    env = insurance_routing()
    routes = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    if change == "retired":
        routes["order-concierge"] = routes["order-concierge"].replace("nw-insurance-claims", "northwind-orders")
    elif change == "missing":
        del routes["employee-help-desk"]
    elif change == "wrong-account":
        routes["claims-warranty"] = routes["claims-warranty"].replace("aisvc-zurlo-northwind", "unrelated")
    elif change == "credentials":
        routes["contact-center"] = routes["contact-center"].replace("https://", "https://user:secret@")
    else:
        routes["unknown"] = routes["order-concierge"]
    settings = Settings(_env_file=None, **{
        **env, "FOUNDRY_PROJECT_ENDPOINTS": json.dumps(routes),
        "NORTHWIND_INSURANCE_AGENTS_READY": True,
    })
    monkeypatch.setattr(insurance, "get_settings", lambda: settings)
    assert insurance.insurance_runtime_ready() is False
    with pytest.raises(HTTPException) as error:
        insurance.require_insurance_runtime()
    assert error.value.status_code == 503


def test_ready_guard_and_company_routing_use_the_same_explicit_insurance_mapping(monkeypatch):
    settings = Settings(_env_file=None, **insurance_routing(), NORTHWIND_INSURANCE_AGENTS_READY=True)
    monkeypatch.setattr(insurance, "get_settings", lambda: settings)
    monkeypatch.setattr(company, "get_settings", lambda: settings)
    company._foundry_project_endpoints.cache_clear()
    try:
        assert insurance.insurance_runtime_ready()
        assert company.foundry_project_endpoint_for("claims-warranty").endswith("/nw-insurance-documents")
        with pytest.raises(HTTPException, match="Unknown insurance department"):
            company.foundry_project_endpoint_for("unknown")
        with pytest.raises(HTTPException, match="Unknown insurance department"):
            company.resolve_department("unknown")
        settings.foundry_project_endpoints = "{"
        company._foundry_project_endpoints.cache_clear()
        with pytest.raises(HTTPException, match="explicitly name all four"):
            company.foundry_project_endpoint_for("order-concierge")
    finally:
        company._foundry_project_endpoints.cache_clear()


@pytest.mark.asyncio
async def test_invalid_insurance_department_is_an_http_error_not_a_stamping_fallback(monkeypatch):
    settings = Settings(_env_file=None, **insurance_routing())
    monkeypatch.setattr(company, "get_settings", lambda: settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://unit.test") as client:
        response = await client.get("/company", headers={"X-Department": "unknown"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown insurance department"
