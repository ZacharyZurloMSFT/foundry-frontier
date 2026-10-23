"""Deprecated names that already-deployed clients and configuration still send.

Canonical insurance names win everywhere; the deprecated spellings are accepted read-only so a deployment
that predates the rename keeps working until its configuration is updated.
"""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from app import company, main, repositories
from app.insurance_domain import ACTION_KIND
from app.settings import Settings


@pytest.fixture
def fresh_endpoint_cache():
    company._foundry_project_endpoints.cache_clear()
    yield
    company._foundry_project_endpoints.cache_clear()


@pytest.mark.parametrize("value,canonical", [
    ("order-concierge", "claims-concierge"),
    ("employee-help-desk", "adjuster-support"),
    ("claims-warranty", "claims-documents"),
    ("  Order-Concierge ", "claims-concierge"),
    ("claims-concierge", "claims-concierge"),
    ("contact-center", "contact-center"),
    (None, ""),
])
def test_deprecated_department_slugs_map_to_the_canonical_slug(value, canonical):
    assert company.canonical_department_slug(value) == canonical


def test_resolve_department_accepts_deprecated_slugs_and_falls_back_to_the_flagship():
    assert company.resolve_department("employee-help-desk")["slug"] == "adjuster-support"
    assert company.resolve_department("claims-warranty")["slug"] == "claims-documents"
    assert company.resolve_department("order-concierge")["slug"] == "claims-concierge"
    assert company.resolve_department("not-a-department")["slug"] == "claims-concierge"
    assert company.resolve_department(None)["slug"] == "claims-concierge"


def test_project_endpoint_map_deployed_with_deprecated_keys_still_routes(monkeypatch, fresh_endpoint_cache):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps({
        "order-concierge": "https://unit.test/api/projects/flagship",
        "employee-help-desk": "https://unit.test/api/projects/support",
    }))
    assert company.foundry_project_endpoint_for("claims-concierge") == "https://unit.test/api/projects/flagship"
    assert company.foundry_project_endpoint_for("order-concierge") == "https://unit.test/api/projects/flagship"
    assert company.foundry_project_endpoint_for("adjuster-support") == "https://unit.test/api/projects/support"
    assert company.foundry_project_endpoint_for("employee-help-desk") == "https://unit.test/api/projects/support"
    # A department without its own project falls back to the flagship project.
    assert company.foundry_project_endpoint_for("contact-center") == "https://unit.test/api/projects/flagship"


@pytest.mark.asyncio
async def test_department_routes_serve_canonical_names_and_accept_deprecated_slugs(fresh_endpoint_cache):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        legacy = await client.get("/departments/employee-help-desk")
        canonical = await client.get("/departments/adjuster-support")
        assert legacy.status_code == 200 and legacy.json() == canonical.json()
        assert legacy.json()["slug"] == "adjuster-support"
        assert (await client.get("/departments/not-a-department")).status_code == 404
        slugs = [d["slug"] for d in (await client.get("/departments")).json()["departments"]]
        assert slugs == ["claims-concierge", "adjuster-support", "claims-documents", "contact-center"]
        manifest = (await client.get("/company")).json()
        assert [tab["id"] for tab in manifest["platform_tabs"]] == ["agent_harness"]


@pytest.mark.parametrize("canonical,deprecated,value", [
    ("INSURANCE_PROJECT_NAME", "ORDER_PROJECT_NAME", "nw-insurance-claims"),
    ("HOSTED_AGENTS_ENABLED", "ORDER_AGENTS_HOSTED", "true"),
    ("MCP_CLAIM_REVIEW_URL", "MCP_WORK_ORDER_URL", "https://mcp.unit.test/mcp/"),
])
def test_deprecated_environment_names_still_configure_the_same_setting(monkeypatch, canonical, deprecated, value):
    field = {"INSURANCE_PROJECT_NAME": "insurance_project_name", "HOSTED_AGENTS_ENABLED": "hosted_agents_enabled",
             "MCP_CLAIM_REVIEW_URL": "harness_claim_review_mcp"}[canonical]
    monkeypatch.delenv(canonical, raising=False)
    monkeypatch.delenv(deprecated, raising=False)
    monkeypatch.setenv(deprecated, value)
    from_deprecated = getattr(Settings(_env_file=None), field)
    monkeypatch.delenv(deprecated)
    monkeypatch.setenv(canonical, value)
    from_canonical = getattr(Settings(_env_file=None), field)
    assert from_deprecated == from_canonical
    # The canonical name wins when both are set.
    monkeypatch.setenv(deprecated, "false" if value == "true" else "https://deprecated.invalid/mcp/")
    assert getattr(Settings(_env_file=None), field) == from_canonical


def test_removed_business_domain_flag_is_ignored_not_rejected(monkeypatch):
    monkeypatch.setenv("NORTHWIND_BUSINESS_DOMAIN", "retail")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "business_domain")


@pytest.mark.asyncio
async def test_deprecated_work_order_routes_alias_the_reviewed_draft_routes(monkeypatch):
    monkeypatch.setattr(main, "list_hitl_resolved", AsyncMock(return_value=[{"id": "draft-1"}]))
    monkeypatch.setattr(main, "delete_hitl", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        current = await client.get("/hitl/resolved")
        deprecated = await client.get("/work-orders")
        assert current.status_code == deprecated.status_code == 200
        assert current.json() == deprecated.json() == {"items": [{"id": "draft-1"}]}
        removed = await client.delete("/hitl/draft-1", params={"workflow_id": "wf-1"})
        removed_alias = await client.delete("/work-orders/draft-1", params={"workflow_id": "wf-1"})
        expected = {"status": "ok", "hitl_id": "draft-1", "workflow_id": "wf-1"}
        assert removed.json() == removed_alias.json() == expected
    paths = main.app.openapi()["paths"]
    assert paths["/work-orders"]["get"]["deprecated"] is True
    assert paths["/work-orders/{hitl_id}"]["delete"]["deprecated"] is True
    assert not paths["/hitl/resolved"]["get"].get("deprecated")
    assert not paths["/hitl/{hitl_id}"]["delete"].get("deprecated")


@pytest.mark.asyncio
async def test_hitl_queue_listings_only_return_claim_action_drafts(monkeypatch):
    queries = []

    class Container:
        def query_items(self, **kwargs):
            queries.append(kwargs)

            async def rows():
                yield {"id": "draft"}

            return rows()

    async def container(_):
        return Container()

    monkeypatch.setattr(repositories, "_container", container)
    assert await repositories.list_hitl_pending() == [{"id": "draft"}]
    assert await repositories.list_hitl_resolved() == [{"id": "draft"}]
    assert len(queries) == 2
    for query in queries:
        assert "c.kind = @kind" in query["query"]
        assert {"name": "@kind", "value": ACTION_KIND} in query["parameters"]
