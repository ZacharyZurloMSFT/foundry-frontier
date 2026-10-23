"""Department knowledge scripts act only on the departments they own (offline; no Azure calls)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connect = load("connect_knowledge_base_departments", "connect-knowledge-base.py")
create_kb = load("create_knowledge_base_departments", "create-knowledge-base.py")
create_indexes = load("create_department_indexes_departments", "create-department-indexes.py")

SEARCH = "https://search.example.net"
DEPARTMENTS = (
    ("claims-concierge", "northwind-insurance-knowledge", "northwind-orders"),
    ("adjuster-support", "nw-insurance-adjuster-support", "nw-employee-help-desk"),
    ("claims-documents", "nw-claims", "nw-claims-warranty"),
    ("contact-center", "nw-contact-center", "nw-contact-center"),
)
ENDPOINTS = {slug: f"https://aisvc.cognitiveservices.azure.com/api/projects/{project}"
             for slug, _, project in DEPARTMENTS}


def company():
    return SimpleNamespace(name="Northwind Insurance", departments=tuple(
        SimpleNamespace(slug=slug, search_index=index, foundry_project=project, seed_dir=Path("missing"))
        for slug, index, project in DEPARTMENTS))


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code, self.text, self._body = status_code, "", body or {}

    def json(self):
        return self._body


class RecordingClient:
    """Stands in for httpx.Client and records every request."""

    requests = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put(self, url, headers=None, json=None):
        type(self).requests.append(("PUT", url, json))
        return FakeResponse()

    def post(self, url, headers=None, json=None):
        type(self).requests.append(("POST", url, json))
        return FakeResponse()

    def get(self, url, headers=None):
        type(self).requests.append(("GET", url, None))
        return FakeResponse(body={"name": url})

    def delete(self, url, headers=None):
        type(self).requests.append(("DELETE", url, None))
        return FakeResponse(404)


@pytest.fixture
def recorded(monkeypatch):
    RecordingClient.requests = []
    for name in ("FOUNDRY_PROJECT_ENDPOINTS", "FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_ENDPOINT",
                 "AZURE_SEARCH_KNOWLEDGE_BASE", "AZURE_SEARCH_KNOWLEDGE_SOURCE", "FOUNDRY_KB_CONNECTION_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", SEARCH)
    monkeypatch.setenv("AZURE_SEARCH_KEY", "search-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://aisvc.openai.azure.com")
    for module in (connect, create_kb, create_indexes):
        monkeypatch.setattr(module, "load_company", company)
        monkeypatch.setattr(module.httpx, "Client", RecordingClient)
    return RecordingClient.requests


def test_knowledge_connections_are_made_only_for_departments_with_their_own_knowledge_base(monkeypatch, recorded, capsys):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps(ENDPOINTS))
    monkeypatch.setattr(connect, "DefaultAzureCredential", lambda **kwargs: SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token="arm-token")))
    connect.main()
    names = [url.split("/connections/")[1].split("?")[0] for _, url, _ in recorded]
    assert names == ["nw-claims-kb-mcp", "nw-contact-center-kb-mcp"]
    assert recorded[0][2]["properties"]["target"] == f"{SEARCH}/knowledgebases/nw-claims-kb/mcp?api-version=2026-05-01-preview"
    assert recorded[0][2]["properties"]["credentials"] == {"keys": {"api-key": "search-key"}}
    out = capsys.readouterr().out
    assert "scripts/publish-insurance-knowledge.py" in out and "scripts/publish-adjuster-support.py" in out
    assert "2 project connection(s) ready" in out


@pytest.mark.parametrize("keys", [
    {"claims-documents": "https://new", "claims-warranty": "https://old"},
    {"claims-warranty": "https://old", "claims-documents": "https://new"},
])
def test_a_canonical_slug_wins_over_its_deprecated_alias_in_the_endpoint_map(monkeypatch, keys):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps(keys))
    assert connect._resolve_endpoints() == {"claims-documents": "https://new"}


def test_deprecated_slugs_in_the_endpoint_map_resolve_to_canonical_departments(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps({
        "order-concierge": "https://a", "employee-help-desk": "https://b", "claims-warranty": "https://c",
        "contact-center": "https://d"}))
    assert connect._resolve_endpoints() == {
        "claims-concierge": "https://a", "adjuster-support": "https://b",
        "claims-documents": "https://c", "contact-center": "https://d"}


def test_knowledge_bases_are_created_only_for_departments_whose_index_this_repo_builds(recorded, capsys):
    create_kb.main()
    assert [(method, url.split("?")[0]) for method, url, _ in recorded] == [
        ("PUT", f"{SEARCH}/knowledgesources/nw-claims-ks"), ("PUT", f"{SEARCH}/knowledgebases/nw-claims-kb"),
        ("PUT", f"{SEARCH}/knowledgesources/nw-contact-center-ks"), ("PUT", f"{SEARCH}/knowledgebases/nw-contact-center-kb"),
    ]
    assert recorded[0][2]["searchIndexParameters"]["searchIndexName"] == "nw-claims"
    out = capsys.readouterr().out
    assert "2 Foundry IQ knowledge base(s) ready" in out
    assert "scripts/publish-insurance-knowledge.py" in out and "scripts/publish-adjuster-support.py" in out


def test_the_published_insurance_indexes_are_never_dropped_or_recreated(monkeypatch, recorded):
    monkeypatch.setenv("STORAGE_ACCOUNT", "storage")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg")
    monkeypatch.setattr(create_indexes, "get_settings", lambda: SimpleNamespace(
        search_endpoint=SEARCH, foundry_endpoint="https://aisvc.cognitiveservices.azure.com"))
    monkeypatch.setattr(create_indexes, "_blob_service", lambda: object())
    monkeypatch.setattr(create_indexes, "_ensure_container", lambda service, name: None)
    monkeypatch.setattr(create_indexes, "_upload_department_markdown", lambda service, dept: 1)
    create_indexes.main()
    touched = " ".join(url for _, url, _ in recorded)
    for owned in ("nw-claims", "nw-contact-center"):
        assert f"/indexes/{owned}?" in touched
    for published in ("northwind-insurance-knowledge", "nw-insurance-adjuster-support"):
        assert published not in touched
    assert create_indexes._SKIP_SLUGS == {"claims-concierge", "adjuster-support"}
