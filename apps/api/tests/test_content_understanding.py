import base64
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app import content_understanding as cu

ENDPOINT = "https://insurance-test.services.ai.azure.com"
LOCATION = ENDPOINT + "/contentunderstanding/analyzerResults/live-op-19?api-version=2025-11-01"
PDF = b"%PDF-1.7\nsynthetic input bytes\n%%EOF"
CONFIG = cu.CUConfig(
    enabled=True, endpoint=ENDPOINT, completion_deployment="existing-gpt41",
    api_key="server-test-secret", poll_interval_seconds=0.01,
)
FIELDS = {
    "claim_id": {"type": "string", "valueString": "CLM-LIVE-913", "confidence": 0.61, "source": "D(2,0,0,1,0,1,1,0,1)", "spans": [{"offset": 2, "length": 12}]},
    "policy_id": {"type": "string", "valueString": "POL-LIVE-27"},
    "loss_date": {"type": "date", "valueDate": "2026-09-24"},
}
RESULT = {
    "analyzerId": cu.ANALYZER_ID, "apiVersion": cu.API_VERSION,
    "contents": [{"kind": "document", "markdown": "CU readback: CLM-LIVE-913", "fields": FIELDS, "pages": [{"pageNumber": 2}]}],
}


def analyzer():
    return {
        "analyzerId": cu.ANALYZER_ID, "status": "ready", "tags": {"owner": cu.OWNER, "schema": "insurance-claims-v1"},
        "models": {"completion": "gpt-4.1"},
        "fieldSchema": {"name": "NorthwindInsuranceClaimsV1", "fields": {key: {"type": kind} for key, kind in cu.REQUIRED_FIELD_TYPES.items()}},
    }


def test_analyzer_identifier_and_default_avoid_service_rejected_hyphens():
    assert cu.ANALYZER_ID == "northwindInsuranceClaimsV1"
    assert cu.ANALYZER_ID.isascii() and cu.ANALYZER_ID.isalnum()
    assert cu.CUConfig().analyzer_id == cu.ANALYZER_ID


class Cloud:
    def __init__(self, statuses=("Pending", "Running", "Succeeded")):
        self.requests = []
        self.statuses = iter(statuses)
        self.analyzer = analyzer()
        self.location = LOCATION
        self.result = copy.deepcopy(RESULT)
        self.post_status = 202
        self.error = {"code": "ModelDeploymentNotFound", "message": "server-test-secret"}

    def handle(self, request):
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST":
            if self.post_status != 202:
                return httpx.Response(self.post_status, json={"error": self.error})
            return httpx.Response(202, json={"id": "live-op-19", "status": "NotStarted"}, headers={"Operation-Location": self.location})
        if "/analyzers/" in request.url.path:
            return httpx.Response(200, json=self.analyzer)
        status = next(self.statuses, "Running")
        return httpx.Response(200, json={"status": status, "result": self.result, "error": self.error})


@pytest.mark.asyncio
async def test_pending_succeeded_returns_only_actual_typed_model_fields():
    cloud = Cloud()
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        actual = await cu.CUService(CONFIG, client).analyze(PDF, "source.pdf", "application/pdf", synthetic=True)
    assert actual["extraction_mode"] == "live_cu"
    assert actual["extraction"] == FIELDS
    assert actual["result"] == RESULT
    assert actual["extraction"]["claim_id"]["confidence"] == 0.61
    assert "confidence" not in actual["extraction"]["policy_id"]
    assert "policyholder_id" not in actual["extraction"]
    assert "CLM-2001" not in json.dumps(actual)
    assert actual["review_required"] is True
    post = next(r for r in cloud.requests if r.method == "POST")
    payload = json.loads(post.content)
    assert payload["inputs"] == [{"name": "source.pdf", "mimeType": "application/pdf", "data": base64.b64encode(PDF).decode()}]
    assert payload["modelDeployments"] == {"gpt-4.1": "existing-gpt41"}
    assert post.headers["Content-Type"] == "application/json"
    assert post.headers["Ocp-Apim-Subscription-Key"] == CONFIG.api_key
    assert ":analyze?" in str(post.url) and "url" not in payload["inputs"][0]
    assert all(r.url.host == "insurance-test.services.ai.azure.com" for r in cloud.requests)


@pytest.mark.asyncio
async def test_identity_uses_cognitive_services_scope_and_no_key():
    scopes = []
    class Credential:
        async def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="identity-access-token")
    cloud = Cloud(("Succeeded",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        await cu.CUService(replace(CONFIG, api_key=""), client, Credential()).analyze(PDF, "source.pdf", "application/pdf")
    assert scopes and set(scopes) == {cu.SCOPE}
    assert all(r.headers.get("Authorization") == "Bearer identity-access-token" for r in cloud.requests)
    assert all("Ocp-Apim-Subscription-Key" not in r.headers for r in cloud.requests)


@pytest.mark.asyncio
async def test_inherited_embedding_role_is_used_as_returned_without_relabelling_small_model():
    cloud = Cloud(("Succeeded",))
    cloud.analyzer["models"]["embedding"] = "prebuilt-analyzer-embedding"
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        await cu.CUService(replace(CONFIG, embedding_deployment="text-embedding-3-small"), client).analyze(PDF, "source.pdf", "application/pdf")
    post = next(r for r in cloud.requests if r.method == "POST")
    assert json.loads(post.content)["modelDeployments"] == {
        "gpt-4.1": "existing-gpt41", "prebuilt-analyzer-embedding": "text-embedding-3-small",
    }


@pytest.mark.asyncio
async def test_failed_operation_is_not_success_or_fixture_fallback():
    cloud = Cloud(("Failed",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        with pytest.raises(HTTPException) as error:
            await cu.CUService(CONFIG, client).analyze(PDF, "source.pdf", "application/pdf")
    assert error.value.status_code == 502
    assert error.value.detail["code"] == "ModelDeploymentNotFound"
    assert "server-test-secret" not in json.dumps(error.value.detail)
    assert error.value.detail["extraction_mode"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_upstream_model_or_analyzer_errors_remain_errors(status):
    cloud = Cloud()
    cloud.post_status = status
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        with pytest.raises(HTTPException) as error:
            await cu.CUService(CONFIG, client).analyze(PDF, "source.pdf", "application/pdf")
    assert error.value.status_code == status
    assert error.value.detail["code"] == "ModelDeploymentNotFound"


@pytest.mark.asyncio
async def test_timeout_requests_supported_result_deletion_without_claiming_compute_cancelled():
    cloud = Cloud(("Pending",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        with pytest.raises(HTTPException) as error:
            await cu.CUService(replace(CONFIG, timeout_seconds=0.04), client).analyze(PDF, "source.pdf", "application/pdf")
    assert error.value.status_code == 504
    assert cloud.requests[-1].method == "DELETE"
    assert str(cloud.requests[-1].url) == LOCATION
    assert "does not cancel compute" in error.value.detail["message"]


@pytest.mark.parametrize("url", [
    "http://insurance-test.services.ai.azure.com/contentunderstanding/analyzerResults/op?api-version=2025-11-01",
    "https://evil.example/contentunderstanding/analyzerResults/op?api-version=2025-11-01",
    ENDPOINT + ":444/contentunderstanding/analyzerResults/op?api-version=2025-11-01",
    ENDPOINT + "/contentunderstanding/analyzerResults/../defaults?api-version=2025-11-01",
    ENDPOINT + "/contentunderstanding/analyzerResults/op?api-version=2025-11-01&url=http://localhost",
    ENDPOINT + "/contentunderstanding/analyzerResults/op?api-version=2025-11-01#fragment",
    ENDPOINT.replace("https://", "https://someone@") + "/contentunderstanding/analyzerResults/op?api-version=2025-11-01",
])
def test_operation_url_rejects_ssrf_and_credentials(url):
    with pytest.raises(HTTPException):
        cu.operation_url(url, ENDPOINT)


@pytest.mark.asyncio
async def test_bad_operation_host_is_never_contacted():
    cloud = Cloud()
    cloud.location = "https://evil.example/result"
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        with pytest.raises(HTTPException) as error:
            await cu.CUService(CONFIG, client).analyze(PDF, "source.pdf", "application/pdf")
    assert error.value.detail["code"] == "unsafe_operation_location"
    assert len(cloud.requests) == 2


@pytest.mark.parametrize("change,code", [
    ({"enabled": False}, "cu_disabled"),
    ({"completion_deployment": ""}, "cu_model_not_configured"),
    ({"completion_model": "unsupported-model"}, "cu_model_not_configured"),
    ({"endpoint": "https://localhost"}, "invalid_cu_endpoint"),
    ({"endpoint": ENDPOINT + "/api/projects/project"}, "invalid_cu_endpoint"),
    ({"analyzer_id": "retail-analyzer"}, "invalid_insurance_analyzer"),
])
def test_configuration_fails_closed(change, code):
    with pytest.raises(HTTPException) as error:
        replace(CONFIG, **change).check()
    assert error.value.status_code == 503 and error.value.detail["code"] == code
    assert cu.CUConfig().enabled is False
    assert "server-test-secret" not in repr(CONFIG)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"status": "creating"}, {"tags": {"owner": "foreign"}},
    {"fieldSchema": {"fields": {"retail": {"type": "string"}}}},
    {"models": {"completion": "gpt-4o"}},
    {"fieldSchema": None}, {"tags": None}, {"models": None},
])
async def test_actual_cloud_analyzer_configuration_is_required(change):
    cloud = Cloud()
    cloud.analyzer.update(change)
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        with pytest.raises(HTTPException) as error:
            await cu.CUService(CONFIG, client).analyze(PDF, "source.pdf", "application/pdf")
    assert error.value.status_code == 503
    assert not any(r.method == "POST" for r in cloud.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, {**RESULT, "contents": []}, {**RESULT, "analyzerId": "foreign"}])
async def test_no_success_when_service_has_no_valid_result(result):
    cloud = Cloud(("Succeeded",))
    cloud.result = result
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as client:
        with pytest.raises(HTTPException):
            await cu.CUService(CONFIG, client).analyze(PDF, "source.pdf", "application/pdf")


@pytest.mark.parametrize("data,name,media,status", [
    (b"", "source.pdf", "application/pdf", 400),
    (b"not a pdf", "source.pdf", "application/pdf", 415),
    (PDF, "source.jpg", "image/jpeg", 415),
    (PDF, "source.pdf", "image/png", 415),
    (PDF, "../source.pdf", "application/pdf", 400),
    (PDF, "https://example.test/source.pdf", "application/pdf", 400),
    (PDF, "source.md", "text/markdown", 415),
    (b"\xff\xd8\xff", "source.jpg", "image/jpeg", 415),
])
def test_documents_reject_mismatch_empty_paths_and_urls(data, name, media, status):
    with pytest.raises(HTTPException) as error:
        cu.validate_document(data, name, media)
    assert error.value.status_code == status


def test_existing_allowlisted_samples_are_real_pdf_bytes_not_extraction_fixtures():
    for item in cu.document_catalog():
        row, data = cu.sample_bytes(item["source_file"])
        assert data.startswith(b"%PDF-") and len(data) > 1000
        assert row["synthetic"] is True
        assert "extraction" not in row and "expected_extraction" not in row
    with pytest.raises(HTTPException) as error:
        cu.sample_bytes("unknown.pdf")
    assert error.value.status_code == 404


@pytest.fixture
def container_sources(monkeypatch):
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "data" / "seed" / "generated").is_dir())
    manifest = root / "data" / "northwind" / "departments" / "claims-warranty" / "claims-documents.json"
    rows = json.loads(manifest.read_text(encoding="utf-8"))["items"]
    container_file = "/app/app/content_understanding.py"
    files = {"/app/app/_northwind/departments/claims-warranty/claims-documents.json": manifest}
    files.update({
        "/app/app/_northwind/claims-documents/" + row["source_file"]: root / "data" / "seed" / "generated" / row["source_file"]
        for row in rows
    })

    class ContainerPath(PurePosixPath):
        def resolve(self):
            return self

        def is_file(self):
            return str(self) in files and files[str(self)].is_file()

        def read_text(self, **kwargs):
            return files[str(self)].read_text(**kwargs)

        def stat(self):
            return files[str(self)].stat()

        def open(self, *args, **kwargs):
            return files[str(self)].open(*args, **kwargs)

    monkeypatch.setattr(cu, "__file__", container_file)
    monkeypatch.setattr(cu, "Path", ContainerPath)
    assert len(ContainerPath(container_file).parents) == 3
    return files, rows


def test_container_paths_read_only_real_bundled_source_bytes(container_sources):
    files, rows = container_sources
    assert len(list(cu._document_locations())) == 4
    assert cu.document_catalog() == rows
    row, data = cu.sample_bytes("nw-fnol-demo-2001.pdf")
    assert row["source_file"] == "nw-fnol-demo-2001.pdf"
    assert data == files["/app/app/_northwind/claims-documents/nw-fnol-demo-2001.pdf"].read_bytes()
    assert data.startswith(b"%PDF-") and len(data) > 1000


def test_container_missing_catalog_and_nonexistent_repository_fail_honestly(container_sources):
    files, _ = container_sources
    files.clear()
    with pytest.raises(HTTPException) as error:
        cu.document_catalog()
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "sample_catalog_unavailable"


def test_container_missing_pdf_and_nonexistent_repository_never_fabricate_bytes(container_sources):
    files, _ = container_sources
    del files["/app/app/_northwind/claims-documents/nw-fnol-demo-2001.pdf"]
    with pytest.raises(HTTPException) as error:
        cu.sample_bytes("nw-fnol-demo-2001.pdf")
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "sample_bytes_unavailable"


@pytest.mark.asyncio
async def test_container_extract_route_analyzes_actual_bundle_without_ancestor_index_error(container_sources):
    files, _ = container_sources
    cloud = Cloud(("Succeeded",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as upstream:
        app = FastAPI()
        app.include_router(cu.router)
        app.dependency_overrides[cu.get_cu_service] = lambda: cu.CUService(CONFIG, upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/claims/extract", json={"filename": "nw-fnol-demo-2001.pdf"})
    assert response.status_code == 200
    assert response.json()["extraction_mode"] == "live_cu"
    post = next(request for request in cloud.requests if request.method == "POST")
    assert base64.b64decode(json.loads(post.content)["inputs"][0]["data"]) == files["/app/app/_northwind/claims-documents/nw-fnol-demo-2001.pdf"].read_bytes()


@pytest.mark.asyncio
async def test_default_off_samples_are_metadata_only_and_explicitly_unavailable(monkeypatch):
    @asynccontextmanager
    async def disabled():
        raise cu.fail(503, "cu_disabled", "Live insurance CU is disabled.")
        yield
    monkeypatch.setattr(cu, "cu_service", disabled)
    app = FastAPI()
    app.include_router(cu.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/claims/samples")
    assert response.status_code == 200
    assert response.json()["availability"]["available"] is False
    assert all("extraction" not in row for row in response.json()["items"])


@pytest.mark.asyncio
async def test_raw_binary_upload_contract_and_stream_limit():
    cloud = Cloud(("Succeeded",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as upstream:
        service = cu.CUService(replace(CONFIG, max_upload_bytes=64), upstream)
        app = FastAPI()
        app.include_router(cu.router)
        app.dependency_overrides[cu.get_cu_service] = lambda: service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/claims/upload?filename=source.pdf", content=PDF, headers={"Content-Type": "application/pdf"})
            assert response.status_code == 200 and response.json()["extraction"] == FIELDS
            bad = await client.post("/claims/upload?filename=source.pdf", json={"url": "https://evil.example/file.pdf"})
            assert bad.status_code == 415
            async def chunks():
                yield b"%PDF-" + b"x" * 32
                yield b"x" * 32
            oversized = await client.post("/claims/upload?filename=source.pdf", content=chunks(), headers={"Content-Type": "application/pdf"})
            assert oversized.status_code == 413
            deleted = await client.delete("/claims/results/live-op-19")
            assert deleted.json()["compute_cancelled"] is False


@pytest.mark.asyncio
async def test_sample_route_analyzes_actual_packaged_bytes():
    cloud = Cloud(("Succeeded",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud.handle)) as upstream:
        app = FastAPI()
        app.include_router(cu.router)
        app.dependency_overrides[cu.get_cu_service] = lambda: cu.CUService(CONFIG, upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/claims/extract", json={"filename": "nw-fnol-demo-2001.pdf"})
            assert response.status_code == 200
            assert response.json()["extraction"] == FIELDS
            assert response.json()["source_mode"] == "synthetic_document"
            source = await client.get("/claims/samples/nw-fnol-demo-2001.pdf/content")
            assert source.content.startswith(b"%PDF-")
            bad = await client.post("/claims/extract", json={"filename": "nw-fnol-demo-2001.pdf", "expected_extraction": {"claim_id": "CLM-2001"}})
            assert bad.status_code == 422
    post = next(r for r in cloud.requests if r.method == "POST")
    assert base64.b64decode(json.loads(post.content)["inputs"][0]["data"]) == source.content
