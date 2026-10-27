"""The committed OpenAPI export must describe exactly the HTTP routes the API serves."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", "")

_spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export-openapi.py")
export_openapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_openapi)

DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
HTTP_OPERATIONS = 61
BUSINESS_OPERATIONS = 26
HINT = "The APIM-facing API surface changed: run `python scripts/export-openapi.py` and update docs/api-management.md."


@pytest.fixture(scope="module")
def app():
    return export_openapi.load_app()


@pytest.fixture(scope="module")
def document(app):
    return app.openapi()


def surface(document):
    """What the gateway sees: operations, parameters, request body and response codes, but not how a library renders schemas."""
    result = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method in export_openapi.METHODS:
                result[(method.upper(), path)] = (
                    operation["operationId"],
                    tuple(sorted((p["in"], p["name"], bool(p.get("required"))) for p in operation.get("parameters", []))),
                    "requestBody" in operation,
                    tuple(sorted(operation.get("responses", {}))),
                )
    return result


def test_committed_spec_matches_the_served_api_surface(document):
    # Schema rendering can vary with unpinned FastAPI/pydantic releases; `export-openapi.py --check` is the strict byte comparison.
    committed = json.loads(export_openapi.read(export_openapi.SPEC))
    assert surface(committed) == surface(document), HINT


def test_committed_operation_table_is_current(document):
    assert export_openapi.read(export_openapi.TABLE) == export_openapi.render_table(document), HINT


def test_committed_spec_is_compact_valid_and_within_the_bicep_limit():
    text = export_openapi.read(export_openapi.SPEC)
    assert len(text) <= export_openapi.BICEP_TEXT_LIMIT
    assert text.endswith("}\n") and "\n" not in text[:-1]
    assert json.loads(text)["openapi"].startswith("3.")


def served_routes() -> set[tuple[str, str]]:
    """HTTP (method, path) pairs from the public `routes` of every router the app includes (FastAPI >= 0.140 wraps included routers privately)."""
    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    from apps.api.app import main

    routers = [main.app.router] + [value for value in vars(main).values() if isinstance(value, APIRouter)]
    return {(method, route.path) for router in routers for route in router.routes if isinstance(route, APIRoute)
            for method in route.methods if method not in ("HEAD", "OPTIONS")}


def test_every_served_http_route_is_in_the_export_and_nothing_else(document):
    served = served_routes()
    exported = {(row["method"], row["path"]) for row in export_openapi.operations(document)}
    assert served == exported
    assert len(exported) == HTTP_OPERATIONS, HINT


def test_business_api_surface_is_the_versioned_prefix(document):
    business = [row for row in export_openapi.operations(document) if row["path"].startswith("/api/v1/")]
    assert len(business) == BUSINESS_OPERATIONS, HINT


def test_assistant_routes_are_additive_versioned_operations(document):
    assistants = {(row["method"], row["path"]) for row in export_openapi.operations(document)
                  if row["path"].startswith("/api/v1/assistants/")}
    assert assistants == {
        ("GET", "/api/v1/assistants/status"),
        ("POST", "/api/v1/assistants/{surface}/chat"),
    }


def test_documentation_routes_are_not_published(document):
    assert not DOC_PATHS & set(document["paths"])


def test_operation_ids_fit_apim_limits_and_are_unique(document):
    ids = [row["operation_id"] for row in export_openapi.operations(document)]
    assert len(ids) == len(set(ids))
    assert max(map(len, ids)) <= export_openapi.APIM_OPERATION_NAME_LIMIT


def test_spec_fits_bicep_text_limit_and_is_compact(document):
    text = export_openapi.render_spec(document)
    assert len(text) <= export_openapi.BICEP_TEXT_LIMIT
    assert text.endswith("}\n") and "\n" not in text[:-1]


def test_schemas_are_not_recursive_which_apim_cannot_import(document):
    schemas = document.get("components", {}).get("schemas", {})

    def references(node, found):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                found.add(ref.rsplit("/", 1)[-1])
            for value in node.values():
                references(value, found)
        elif isinstance(node, list):
            for value in node:
                references(value, found)

    graph = {}
    for name, schema in schemas.items():
        found: set[str] = set()
        references(schema, found)
        graph[name] = found

    def visit(name, stack):
        assert name not in stack, f"recursive schema: {' -> '.join(stack + [name])}"
        for child in graph.get(name, ()):
            visit(child, stack + [name])

    for name in graph:
        visit(name, [])


def test_export_is_deterministic_regardless_of_feature_flags(monkeypatch):
    baseline = export_openapi.render_spec(export_openapi.load_app().openapi())
    for name in ("COPILOT_HARNESS_ENABLED", "NORTHWIND_BUSINESS_API_ENABLED", "VOICE_LIVE_ENABLED", "ADJUSTER_SUPPORT_ENABLED",
                 "NORTHWIND_ASSISTANTS_ENABLED"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("NORTHWIND_ASSISTANT_SURFACES", "concierge,help_center")
    app = export_openapi.load_app()
    app.openapi_schema = None
    assert export_openapi.render_spec(app.openapi()) == baseline
