import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "publish_insurance_knowledge", ROOT / "scripts" / "publish-insurance-knowledge.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
SECRET = "sensitive-test-admin-key"


def expected_counts():
    fixture = json.loads((ROOT / "data" / "northwind" / "insurance-fixtures.json").read_text())
    seeds = json.loads((ROOT / "data" / "seed" / "insurance-corpus.json").read_text())
    return sum(len(wording["sections"]) for wording in fixture["policy_wordings"]), len(seeds)


class SearchHarness:
    def __init__(self):
        self.resources = {}
        self.docs = {}
        self.requests = []
        self.failed_upload = False
        self.upload_status = 200
        self.preflight_status = None
        self.foreign_documents = False

    def handle(self, request):
        self.requests.append(request)
        url = str(request.url)
        if request.method == "GET":
            if self.preflight_status:
                return httpx.Response(self.preflight_status, text=SECRET)
            if url not in self.resources:
                return httpx.Response(404)
            return httpx.Response(200, json=self.resources[url], headers={"etag": '"insurance-etag"'})
        body = json.loads(request.content)
        if request.method == "PUT":
            self.resources[url] = body
            return httpx.Response(201, json=body)
        if request.url.path.endswith("/docs/search"):
            return httpx.Response(200, json={"value": [{"id": "unowned"}] if self.foreign_documents else []})
        if request.url.path.endswith("/docs/index"):
            self.docs.update({document["id"]: document for document in body["value"]})
            results = [{
                "key": document["id"], "status": not self.failed_upload,
                "statusCode": 400 if self.failed_upload else 201, "errorMessage": SECRET,
            } for document in body["value"]]
            return httpx.Response(self.upload_status, json={"value": results})
        raise AssertionError("Unexpected target or operation: " + url)


@pytest.fixture
def cloud(monkeypatch):
    harness = SearchHarness()
    real_client = httpx.Client
    monkeypatch.setattr(module, "search_admin_key", lambda: SECRET)
    monkeypatch.setattr(module, "project_token", lambda: "test-azd-token")
    monkeypatch.setattr(
        module.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(harness.handle), **kwargs),
    )
    return harness


def test_dry_run_never_authenticates_or_contacts_cloud(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry-run cannot authenticate or contact cloud.")

    monkeypatch.setattr(module, "search_admin_key", forbidden)
    monkeypatch.setattr(module, "project_token", forbidden)
    monkeypatch.setattr(module.httpx, "Client", forbidden)
    monkeypatch.setattr(sys, "argv", ["publish-insurance-knowledge.py", "--connect-project"])
    assert module.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "dry-run"
    wordings, seeds = expected_counts()
    assert summary["documents"] == wordings + seeds
    assert summary["policy_wording_sections"] == wordings
    assert summary["seed_documents"] == seeds
    assert summary["connection"] == "northwind-insurance-kb-mcp"
    assert summary["project"] == module.PROJECT_ID
    assert "credentials" not in summary and "content" not in summary


def test_documents_preserve_canonical_citations_dates_state_and_version():
    records = module.documents()
    assert records == module.documents()
    assert len({record["id"] for record in records}) == len(records)
    assert all(record["synthetic"] and record["fictional"] and record["business_domain"] == "insurance" for record in records)
    assert all(module.DISCLAIMER in record["content"] for record in records)
    wordings = [record for record in records if record["doc_type"] == "policy_wording"]
    assert {record["state"] for record in wordings} == {"WA", "CO"}
    for record in wordings:
        assert record["source_uri"] == "urn:northwind:synthetic:policy:" + record["policy_ref"]
        assert record["version"] == "1"
        assert record["effective_from"] == "2026-01-01"
        assert record["effective_to"] == "2026-12-31"
        assert "/sections/" in record["source_path"]
    procedure = next(record for record in records if record["document_id"] == "nw-adjuster-review-2026-v1")
    assert procedure["states"] == ["WA", "CO"]
    assert procedure["effective_to"] is None
    assert "Adjuster procedures and claims-review boundaries" in procedure["content"]
    assert "Workday" not in procedure["content"] and "Expense report" not in procedure["content"]


def test_markdown_metadata_preserves_lowercase_form_version_and_declared_end_date():
    metadata = module._markdown_metadata(
        "**Source reference:** NW-AUTO-WA-2026-v1. **Version:** 1.\n"
        "**Effective:** 2026-01-01 through 2026-12-31. **Jurisdiction:** fictional Washington (WA) auto.\n"
        "Entirely synthetic demonstration."
    )
    assert metadata["policy_ref"] == "NW-AUTO-WA-2026-v1"
    assert metadata["version"] == "1"
    assert metadata["effective_to"] == "2026-12-31"
    assert metadata["states"] == ["WA"]


def test_index_and_knowledge_source_project_citation_metadata_without_embeddings():
    index = module.index_body()
    assert index["name"] == "northwind-insurance-knowledge"
    fields = {field["name"]: field for field in index["fields"]}
    assert fields["id"]["key"] is True
    assert fields["synthetic"]["type"] == "Edm.Boolean"
    assert fields["states"]["type"] == "Collection(Edm.String)"
    assert "vectorSearch" not in index
    for record in module.documents():
        assert set(record) == set(fields)
    source = module.source_body()["searchIndexParameters"]
    assert source["searchIndexName"] == index["name"]
    assert {field["name"] for field in source["sourceDataFields"]} == set(fields)
    kb = module.kb_body()
    assert kb["knowledgeSources"] == [{"name": module.SOURCE_NAME}]
    assert kb["models"] == []
    assert kb["retrievalReasoningEffort"] == {"kind": "minimal"}
    assert kb["outputMode"] == "extractiveData"


def replace_json(monkeypatch, filename, data):
    read_text = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: json.dumps(data) if path.name == filename else read_text(path, *args, **kwargs),
    )


@pytest.mark.parametrize("change", ["not-synthetic", "wrong-domain", "bad-citation", "reversed-dates", "duplicate-section"])
def test_invalid_fixtures_fail_before_any_cloud_call(change, monkeypatch, cloud):
    fixture = json.loads((ROOT / "data" / "northwind" / "insurance-fixtures.json").read_text())
    if change == "not-synthetic":
        fixture["policy_wordings"][0]["synthetic"] = False
    elif change == "wrong-domain":
        fixture["business_domain"] = "retail"
    elif change == "bad-citation":
        fixture["policy_wordings"][0]["sections"][0]["source_uri"] = "https://real-insurer.example/policy"
    elif change == "reversed-dates":
        fixture["policy_wordings"][0]["effective_from"] = "2027-01-01"
    else:
        fixture["policy_wordings"][0]["sections"].append(copy.deepcopy(fixture["policy_wordings"][0]["sections"][0]))
    replace_json(monkeypatch, "insurance-fixtures.json", fixture)
    with pytest.raises(ValueError):
        module.publish(apply=True)
    assert not cloud.requests


@pytest.mark.parametrize("source", ["../../.env", "data/northwind/insurance-fixtures.json", "data/seed/insurance-corpus.json"])
def test_seed_allowlist_cannot_read_configuration_or_unrelated_sources(source, monkeypatch, cloud):
    manifest = json.loads((ROOT / "data" / "seed" / "insurance-corpus.json").read_text())
    manifest[0]["source"] = source
    replace_json(monkeypatch, "insurance-corpus.json", manifest)
    with pytest.raises(ValueError, match="department seed markdown"):
        module.publish(apply=True)
    assert not cloud.requests


def test_admin_key_lookup_is_explicit_and_never_logs_cli_output(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda executable: "az.cmd")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=SECRET + "\n", stderr=SECRET)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.search_admin_key() == SECRET
    command, options = calls[0]
    assert command == [
        "az.cmd", "search", "admin-key", "show", "--subscription", module.SUBSCRIPTION,
        "--resource-group", "rg-northwind-data", "--service-name", "srch-zurlo-northwind",
        "--query", "primaryKey", "--output", "tsv", "--only-show-errors",
    ]
    assert options["capture_output"] is True and options["check"] is False
    assert not capsys.readouterr().out


def test_failed_admin_key_lookup_withholds_secret_output(monkeypatch, capsys):
    monkeypatch.setattr(module.shutil, "which", lambda executable: "az.cmd")
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET),
    )
    with pytest.raises(RuntimeError) as error:
        module.search_admin_key()
    assert SECRET not in str(error.value)
    assert not capsys.readouterr().out


def test_optional_connection_uses_only_azd_user_credential_for_explicit_tenant(monkeypatch):
    from azure.identity import AzureDeveloperCliCredential
    import azure.identity

    calls = []

    class Credential:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_token(self, scope):
            calls.append(scope)
            return SimpleNamespace(token="test-arm-token")

    monkeypatch.setattr(azure.identity, "AzureDeveloperCliCredential", Credential)
    assert module.project_token() == "test-arm-token"
    assert calls == [{"tenant_id": module.TENANT}, "https://management.azure.com/.default"]
    assert AzureDeveloperCliCredential is not Credential


def test_apply_and_reapply_touch_only_new_insurance_targets(cloud, capsys):
    first = module.publish(apply=True, connect_project=True)
    second = module.publish(apply=True, connect_project=True)
    assert first == second
    assert len(cloud.docs) == sum(expected_counts())
    puts = [request for request in cloud.requests if request.method == "PUT"]
    assert len(puts) == 7  # First connection creation; repeated owned connection is unchanged.
    assert all("northwind-insurance-" in str(request.url) for request in cloud.requests)
    assert all(request.method in ("GET", "PUT", "POST") for request in cloud.requests)
    for request in puts:
        body = json.loads(request.content)
        if request.url.host == "management.azure.com":
            assert module.PROJECT_ID in str(request.url)
            assert body["name"] == module.CONNECTION_NAME
            assert body["properties"]["credentials"]["keys"] == {"api-key": SECRET}
            assert request.headers["authorization"] == "Bearer test-azd-token"
            assert "api-key" not in request.headers
        else:
            assert request.headers["api-key"] == SECRET
            assert body["name"] in (module.INDEX_NAME, module.SOURCE_NAME, module.KB_NAME)
        assert request.headers.get("if-none-match") == "*" or request.headers.get("if-match") == '"insurance-etag"'
    assert SECRET not in json.dumps(first)
    assert not capsys.readouterr().out


def test_search_only_publish_never_uses_project_credential(cloud, monkeypatch):
    monkeypatch.setattr(module, "project_token", lambda: pytest.fail("Connection flag is required for azd credentials."))
    module.publish(apply=True)
    assert all(request.url.host == f"{module.SEARCH_SERVICE}.search.windows.net" for request in cloud.requests)


@pytest.mark.parametrize("status", [401, 403, 409, 412, 500])
def test_preflight_failure_never_writes_or_exposes_response_body(status, cloud):
    cloud.preflight_status = status
    with pytest.raises(RuntimeError) as error:
        module.publish(apply=True)
    assert SECRET not in str(error.value)
    assert all(request.method == "GET" for request in cloud.requests)


def test_foreign_index_schema_prevents_any_writes(cloud):
    cloud.resources[f"{module.SEARCH_ENDPOINT}/indexes/{module.INDEX_NAME}?api-version={module.SEARCH_API}"] = {
        "name": module.INDEX_NAME, "fields": [{"name": "retail_order", "type": "Edm.String", "key": True}],
    }
    with pytest.raises(RuntimeError, match="incompatible"):
        module.publish(apply=True)
    assert not any(request.method == "PUT" for request in cloud.requests)


def test_foreign_documents_prevent_overwriting_a_matching_schema(cloud):
    cloud.resources[f"{module.SEARCH_ENDPOINT}/indexes/{module.INDEX_NAME}?api-version={module.SEARCH_API}"] = module.index_body()
    cloud.foreign_documents = True
    with pytest.raises(RuntimeError, match="unowned or nonsynthetic"):
        module.publish(apply=True)
    assert not any(request.method == "PUT" or request.url.path.endswith("/docs/index") for request in cloud.requests)


def test_retail_source_in_insurance_named_kb_prevents_any_writes(cloud):
    kb = module.kb_body()
    kb["knowledgeSources"] = [{"name": "northwind-knowledge-ks"}]
    cloud.resources[f"{module.SEARCH_ENDPOINT}/knowledgebases/{module.KB_NAME}?api-version={module.SEARCH_API}"] = kb
    with pytest.raises(RuntimeError, match="incompatible"):
        module.publish(apply=True)
    assert not any(request.method == "PUT" for request in cloud.requests)


@pytest.mark.parametrize("status", [200, 207])
def test_per_document_failures_stop_kb_and_connection_publication(status, cloud):
    cloud.failed_upload = True
    cloud.upload_status = status
    with pytest.raises(RuntimeError) as error:
        module.publish(apply=True, connect_project=True)
    assert SECRET not in str(error.value)
    assert all("/indexes/" in str(request.url) for request in cloud.requests if request.method == "PUT")


def test_server_added_kb_reference_defaults_do_not_break_idempotency(cloud):
    body = module.kb_body()
    body["knowledgeSources"][0]["alwaysQuerySource"] = False
    cloud.resources[f"{module.SEARCH_ENDPOINT}/knowledgebases/{module.KB_NAME}?api-version={module.SEARCH_API}"] = body
    assert module.publish(apply=True)["mode"] == "applied"


@pytest.mark.parametrize("kind", ["source", "connection"])
def test_incompatible_insurance_named_reference_prevents_all_writes(kind, cloud):
    if kind == "source":
        body = module.source_body()
        body["searchIndexParameters"]["searchIndexName"] = "northwind-knowledge"
        url = f"{module.SEARCH_ENDPOINT}/knowledgesources/{module.SOURCE_NAME}?api-version={module.SEARCH_API}"
    else:
        body = {"properties": {
            "authType": "CustomKeys", "category": "RemoteTool",
            "target": f"{module.SEARCH_ENDPOINT}/knowledgebases/northwind-kb/mcp?api-version={module.SEARCH_API}",
            "metadata": {"NorthwindInsurancePublisher": module.OWNER},
        }}
        url = f"https://management.azure.com{module.PROJECT_ID}/connections/{module.CONNECTION_NAME}?api-version={module.ARM_API}"
    cloud.resources[url] = body
    with pytest.raises(RuntimeError, match="incompatible"):
        module.publish(apply=True, connect_project=True)
    assert not any(request.method == "PUT" or request.url.path.endswith("/docs/index") for request in cloud.requests)


def test_cli_failure_reports_status_without_key_response_or_success(cloud, monkeypatch, capsys):
    cloud.preflight_status = 403
    monkeypatch.setattr(sys, "argv", ["publish-insurance-knowledge.py", "--apply"])
    assert module.main() == 1
    output = capsys.readouterr()
    assert "HTTP 403" in output.err
    assert SECRET not in output.err
    assert not output.out
