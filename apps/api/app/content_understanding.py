"""Live insurance CU. No fixture extractions, storage writes, or adjudication.

REST contract: GA 2025-11-01 :analyze accepts base64 input and request-scoped
modelDeployments, avoiding account-default changes and public document URLs.
See https://learn.microsoft.com/rest/api/contentunderstanding/content-analyzers/analyze
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import DefaultAzureCredential
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .settings import get_settings

API_VERSION = "2025-11-01"
ANALYZER_ID = "northwindInsuranceClaimsV1"
OWNER = "northwind-insurance-cu-v1"
SCOPE = "https://cognitiveservices.azure.com/.default"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
REQUIRED_FIELD_TYPES = {
    "claim_id": "string", "policy_id": "string", "policyholder_id": "string",
    "loss_date": "date", "estimate_total_usd": "number", "invoice_total_usd": "number",
}
REQUIRED_FIELDS = set(REQUIRED_FIELD_TYPES)
REVIEW_NOTICE = (
    "Untrusted model extraction, not verified ground truth. Review the original "
    "document, field confidence and source evidence. No coverage, fault, cause, "
    "fraud or payment determination; no records were updated."
)
router = APIRouter(prefix="/claims", tags=["insurance-content-understanding"])
_analysis_slots = asyncio.Semaphore(2)


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, "extraction_mode": "unavailable"})


def safe_filename(filename: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,159}", filename) or ".." in filename:
        raise fail(400, "invalid_filename", "Use a plain filename, not a path or remote URL.")
    return filename


def validate_document(data: bytes, filename: str, media_type: str, limit: int = MAX_UPLOAD_BYTES) -> None:
    safe_filename(filename)
    if not data:
        raise fail(400, "empty_document", "The document is empty.")
    if len(data) > limit:
        raise fail(413, "document_too_large", f"Document limit is {limit} bytes.")
    expected = MEDIA_TYPES.get(Path(filename).suffix.lower())
    if expected is None or media_type != expected:
        raise fail(415, "unsupported_document", "Filename and Content-Type must agree: PDF, JPEG or PNG only.")
    signatures_match = (
        expected == "application/pdf" and data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:]
        or expected == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in data[-32:]
        or expected == "image/jpeg" and data.startswith(b"\xff\xd8\xff") and data.rstrip().endswith(b"\xff\xd9")
    )
    if not signatures_match:
        raise fail(415, "document_type_mismatch", "Document bytes do not match the declared file type.")


def validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        valid = (
            parsed.scheme == "https" and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and parsed.path in ("", "/")
            and bool(re.fullmatch(r"[a-z0-9-]+\.(?:cognitiveservices|services\.ai)\.azure\.com", parsed.hostname or ""))
        )
    except ValueError:
        valid = False
    if not valid:
        raise fail(503, "invalid_cu_endpoint", "Configure the HTTPS Azure resource root endpoint, not a project or document URL.")
    return endpoint.rstrip("/")


def operation_url(location: str, endpoint: str) -> str:
    """Never forward a credential to an operation URL on a different origin."""
    try:
        url, origin = urlsplit(location), urlsplit(endpoint)
        valid = (
            url.scheme == origin.scheme and url.hostname == origin.hostname
            and url.port == origin.port and not url.username and not url.password and not url.fragment
            and re.fullmatch(r"/contentunderstanding/analyzerResults/[A-Za-z0-9_-]{1,128}", url.path)
            and parse_qs(url.query) == {"api-version": [API_VERSION]}
        )
    except ValueError:
        valid = False
    if not valid:
        raise fail(502, "unsafe_operation_location", "CU returned an unexpected operation location; polling was refused.")
    return location


@dataclass(frozen=True)
class CUConfig:
    enabled: bool = False
    endpoint: str = ""
    analyzer_id: str = ANALYZER_ID
    completion_model: str = "gpt-4.1"
    completion_deployment: str = ""
    embedding_deployment: str = ""
    client_id: str = ""
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 90
    poll_interval_seconds: float = 1
    max_upload_bytes: int = MAX_UPLOAD_BYTES

    @classmethod
    def from_settings(cls) -> CUConfig:
        s = get_settings()
        return cls(
            enabled=getattr(s, "cu_enabled", False),
            endpoint=getattr(s, "cu_endpoint", "") or s.foundry_endpoint,
            analyzer_id=getattr(s, "cu_analyzer_id", ANALYZER_ID),
            completion_model=getattr(s, "cu_completion_model", "gpt-4.1"),
            completion_deployment=getattr(s, "cu_completion_deployment", ""),
            embedding_deployment=getattr(s, "cu_embedding_deployment", ""),
            client_id=s.azure_client_id, api_key=s.foundry_api_key,
            timeout_seconds=getattr(s, "cu_timeout_seconds", 90),
            max_upload_bytes=getattr(s, "cu_max_upload_bytes", MAX_UPLOAD_BYTES),
        )

    def check(self) -> str:
        if not self.enabled:
            raise fail(503, "cu_disabled", "Live insurance Content Understanding is disabled; no authored extraction fallback is used.")
        endpoint = validate_endpoint(self.endpoint)
        if self.analyzer_id != ANALYZER_ID:
            raise fail(503, "invalid_insurance_analyzer", f"Use the versioned insurance analyzer {ANALYZER_ID}.")
        if not self.completion_deployment or self.completion_model not in ("gpt-4.1", "gpt-4o"):
            raise fail(503, "cu_model_not_configured", "Configure an existing supported CU completion model and deployment.")
        if not all(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", d) for d in (self.completion_deployment, self.embedding_deployment) if d):
            raise fail(503, "invalid_model_deployment", "Invalid CU model deployment name.")
        if not 0 < self.timeout_seconds <= 180 or not 0 < self.max_upload_bytes <= MAX_UPLOAD_BYTES:
            raise fail(503, "invalid_cu_limits", "CU request bounds exceed the supported application limits.")
        return endpoint


class CUService:
    def __init__(self, config: CUConfig, client: httpx.AsyncClient, credential=None):
        self.config, self.client, self.credential = config, client, credential
        self.endpoint = config.check()

    async def headers(self) -> dict[str, str]:
        if self.config.api_key:
            return {"Ocp-Apim-Subscription-Key": self.config.api_key}
        try:
            token = await self.credential.get_token(SCOPE)
        except ClientAuthenticationError:
            raise fail(503, "cu_auth_unavailable", "The API identity could not authenticate to Content Understanding.") from None
        return {"Authorization": f"Bearer {token.token}"}

    async def request(self, method: str, url: str, **kwargs) -> tuple[dict, httpx.Headers]:
        async with self.client.stream(method, url, headers=await self.headers(), **kwargs) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise fail(502, "cu_response_too_large", "CU response exceeded the application response limit.")
                data.extend(chunk)
            if not response.is_success:
                # Do not expose arbitrary upstream text, document content, URLs or credentials.
                code = "upstream_error"
                try:
                    candidate = json.loads(data).get("error", {}).get("code", "")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate):
                        code = candidate
                except (ValueError, AttributeError):
                    pass
                status = response.status_code if 400 <= response.status_code < 500 else 502
                raise fail(status, code, f"CU rejected the request (HTTP {response.status_code}); check analyzer, model deployment and identity configuration.")
            if response.status_code == 204:
                return {}, response.headers
            try:
                body = json.loads(data)
            except ValueError:
                raise fail(502, "invalid_cu_response", "CU returned invalid JSON.") from None
            if not isinstance(body, dict):
                raise fail(502, "invalid_cu_response", "CU returned an unexpected response shape.")
            return body, response.headers

    async def analyzer(self) -> tuple[dict, dict[str, str]]:
        analyzer, _ = await self.request(
            "GET", f"{self.endpoint}/contentunderstanding/analyzers/{self.config.analyzer_id}?api-version={API_VERSION}"
        )
        schema, tags, models = (analyzer.get(key) for key in ("fieldSchema", "tags", "models"))
        if not all(isinstance(value, dict) for value in (schema, tags, models)):
            raise fail(503, "insurance_analyzer_not_ready", "The live analyzer did not return a valid insurance schema/model configuration.")
        fields = schema.get("fields", {})
        if (
            str(analyzer.get("status", "")).lower() != "ready"
            or analyzer.get("analyzerId") != self.config.analyzer_id
            or tags.get("owner") != OWNER
            or tags.get("schema") != "insurance-claims-v1"
            or schema.get("name") != "NorthwindInsuranceClaimsV1"
            or not isinstance(fields, dict) or not REQUIRED_FIELDS.issubset(fields)
            or any(not isinstance(fields[key], dict) or fields[key].get("type") != kind for key, kind in REQUIRED_FIELD_TYPES.items())
            or models.get("completion") != self.config.completion_model
        ):
            raise fail(503, "insurance_analyzer_not_ready", "The live analyzer is not ready or does not match the owned insurance schema/model.")
        deployments = {models["completion"]: self.config.completion_deployment}
        if models.get("embedding"):
            if not self.config.embedding_deployment:
                raise fail(503, "cu_embedding_not_configured", "The live analyzer requires an explicit existing embedding deployment.")
            deployments[models["embedding"]] = self.config.embedding_deployment
        return analyzer, deployments

    async def availability(self) -> dict:
        async with asyncio.timeout(min(15, self.config.timeout_seconds)):
            analyzer, _ = await self.analyzer()
        return {
            "available": True, "status": "analyzer_ready", "analyzer_id": self.config.analyzer_id,
            "completion_model": analyzer["models"]["completion"], "analysis_verified": False,
            "message": "Live analyzer configuration checked. Extraction is verified only by a successful live request.",
            "max_upload_bytes": self.config.max_upload_bytes,
        }

    async def delete_result(self, url: str) -> bool:
        """GA supports result deletion, not cancellation of compute already started."""
        try:
            async with asyncio.timeout(5):
                await self.request("DELETE", operation_url(url, self.endpoint))
            return True
        except (HTTPException, httpx.HTTPError, TimeoutError, ClientAuthenticationError):
            return False

    async def analyze(self, data: bytes, filename: str, media_type: str, *, synthetic: bool = False) -> dict:
        validate_document(data, filename, media_type, self.config.max_upload_bytes)
        location = None
        try:
            # Includes semaphore wait, auth, preflight, submission and all polling.
            async with asyncio.timeout(self.config.timeout_seconds):
                async with _analysis_slots:
                    _, deployments = await self.analyzer()
                    _, headers = await self.request(
                        "POST",
                        f"{self.endpoint}/contentunderstanding/analyzers/{self.config.analyzer_id}:analyze?api-version={API_VERSION}",
                        json={
                            "inputs": [{"name": filename, "mimeType": media_type, "data": base64.b64encode(data).decode("ascii")}],
                            "modelDeployments": deployments,
                        },
                    )
                    location = operation_url(headers.get("Operation-Location", ""), self.endpoint)
                    result_id = urlsplit(location).path.rsplit("/", 1)[-1]
                    for _ in range(180):
                        operation, _ = await self.request("GET", location)
                        status = str(operation.get("status", "")).lower()
                        if status == "succeeded":
                            result = operation.get("result")
                            if not isinstance(result, dict) or not isinstance(result.get("contents"), list) or not result["contents"]:
                                raise fail(502, "cu_missing_result", "CU succeeded without document contents; no fields were invented.")
                            if result.get("analyzerId") != self.config.analyzer_id:
                                raise fail(502, "cu_analyzer_mismatch", "CU returned a result from an unexpected analyzer.")
                            contents = result["contents"]
                            if not all(isinstance(c, dict) and isinstance(c.get("fields", {}), dict) for c in contents):
                                raise fail(502, "cu_invalid_fields", "CU returned malformed field contents.")
                            if any(
                                not isinstance(value, dict) or not isinstance(value.get("type"), str)
                                for content in contents for value in content.get("fields", {}).values()
                            ):
                                raise fail(502, "cu_invalid_fields", "CU returned malformed typed fields.")
                            return {
                                "id": result_id, "status": "succeeded", "extraction_mode": "live_cu",
                                "source_mode": "synthetic_document" if synthetic else "user_upload",
                                "synthetic": synthetic, "source_file": filename,
                                "source_sha256": hashlib.sha256(data).hexdigest(), "source_bytes": len(data),
                                "source_text": "\n\n".join(c.get("markdown", "") for c in contents if isinstance(c.get("markdown", ""), str)),
                                # Keep typed values, confidence, source geometry, pages and spans intact.
                                "extraction": contents[0].get("fields", {}), "result": result,
                                "operation_id": result_id, "review_required": True, "notice": REVIEW_NOTICE,
                                "result_retention": "Service result retained until explicit deletion or service expiry; input is not saved by this API.",
                            }
                        if status in ("failed", "canceled", "cancelled"):
                            error = operation.get("error", {})
                            code = error.get("code", "") if isinstance(error, dict) else ""
                            code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "cu_analysis_failed"
                            raise fail(502, code, "Live CU analysis failed; no authored or expected extraction was substituted.")
                        if status not in ("notstarted", "running", "pending"):
                            raise fail(502, "invalid_cu_operation", "CU returned an unexpected operation status.")
                        await asyncio.sleep(max(0.01, min(self.config.poll_interval_seconds, 5)))
                    raise TimeoutError
        except (TimeoutError, httpx.TimeoutException):
            deleted = await self.delete_result(location) if location else False
            raise fail(504, "cu_timeout", f"CU analysis timed out. Result deletion {'requested' if deleted else 'not confirmed'}; deletion does not cancel compute.") from None
        except asyncio.CancelledError:
            if location:
                await self.delete_result(location)
            raise
        except httpx.HTTPError:
            raise fail(502, "cu_transport_error", "The API could not reach Content Understanding; no fallback extraction was used.") from None


@asynccontextmanager
async def cu_service():
    config = CUConfig.from_settings()
    config.check()
    credential = None if config.api_key else DefaultAzureCredential(managed_identity_client_id=config.client_id or None)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15), follow_redirects=False, trust_env=False) as client:
            yield CUService(config, client, credential)
    finally:
        if credential:
            await credential.close()


async def get_cu_service():
    async with cu_service() as service:
        yield service


def _document_locations():
    here = Path(__file__).resolve()
    bundle = here.parent / "_northwind"
    yield (
        bundle / "departments" / "claims-warranty" / "claims-documents.json",
        bundle / "claims-documents",
    )
    for parent in here.parents:
        yield (
            parent / "data" / "northwind" / "departments" / "claims-warranty" / "claims-documents.json",
            parent / "data" / "seed" / "generated",
        )


def document_catalog() -> list[dict]:
    try:
        manifest = next((manifest for manifest, _ in _document_locations() if manifest.is_file()), None)
        if manifest is None:
            raise FileNotFoundError
        rows = json.loads(manifest.read_text(encoding="utf-8"))["items"]
        if not isinstance(rows, list) or len(rows) > 30:
            raise ValueError
        for row in rows:
            safe_filename(row["source_file"])
            if row.get("synthetic") is not True or row.get("media_type") != "application/pdf" or Path(row["source_file"]).suffix != ".pdf":
                raise ValueError
        if len({row["source_file"] for row in rows}) != len(rows):
            raise ValueError
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        raise fail(503, "sample_catalog_unavailable", "The allowlisted synthetic source document catalog is unavailable.") from None


def sample_bytes(filename: str) -> tuple[dict, bytes]:
    safe_filename(filename)
    row = next((r for r in document_catalog() if r["source_file"] == filename), None)
    if row is None:
        raise fail(404, "unknown_sample", "No allowlisted source document has this filename.")
    for _, root in _document_locations():
        path = (root / filename).resolve()
        if not path.is_relative_to(root.resolve()):
            raise fail(400, "invalid_source_path", "Source document must stay inside the allowlisted document directory.")
        if path.is_file():
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                raise fail(413, "document_too_large", "Packaged source document exceeds the upload limit.")
            with path.open("rb") as source:
                data = source.read(MAX_UPLOAD_BYTES + 1)
            validate_document(data, filename, row["media_type"])
            return row, data
    raise fail(503, "sample_bytes_unavailable", "The synthetic PDF bytes are not packaged; no fixture extraction was used.")


class SampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=160)


@router.get("/samples")
async def claims_samples() -> dict:
    rows = document_catalog()
    try:
        async with cu_service() as service:
            availability = await service.availability()
    except HTTPException as exc:
        availability = {"available": False, "status": "unavailable", "message": exc.detail["message"], "code": exc.detail["code"]}
    except (httpx.HTTPError, TimeoutError, ClientAuthenticationError):
        availability = {"available": False, "status": "unavailable", "message": "Live CU configuration could not be checked."}
    return {"items": rows, "availability": availability, "notice": REVIEW_NOTICE}


@router.get("/status")
async def claims_status(service: CUService = Depends(get_cu_service)) -> dict:
    try:
        return await service.availability()
    except (httpx.HTTPError, TimeoutError):
        raise fail(503, "cu_unavailable", "Live CU configuration could not be checked.") from None


@router.get("/samples/{filename}/content")
async def claims_source(filename: str) -> Response:
    _, data = sample_bytes(filename)
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/extract")
async def claims_extract(req: SampleRequest, service: CUService = Depends(get_cu_service)) -> dict:
    row, data = sample_bytes(req.filename)
    return await service.analyze(data, req.filename, row["media_type"], synthetic=True)


@router.post("/upload")
async def claims_upload(
    request: Request, filename: str = Query(min_length=1, max_length=160),
    service: CUService = Depends(get_cu_service),
) -> dict:
    """Raw bounded PDF/JPEG/PNG body; intentionally not multipart or a URL."""
    safe_filename(filename)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in MEDIA_TYPES.values():
        raise fail(415, "unsupported_document", "Send raw PDF/JPEG/PNG bytes with the matching Content-Type, not multipart or JSON.")
    limit = service.config.max_upload_bytes
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isdigit():
            raise fail(400, "invalid_content_length", "Invalid Content-Length.")
        if int(length) > limit:
            raise fail(413, "document_too_large", f"Document limit is {limit} bytes.")
    data = bytearray()
    try:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise fail(413, "document_too_large", f"Document limit is {limit} bytes.")
                data.extend(chunk)
    except TimeoutError:
        raise fail(408, "upload_timeout", "Document upload timed out before analysis; no source was saved.") from None
    return await service.analyze(bytes(data), filename, media_type)


@router.delete("/results/{operation_id}")
async def claims_delete_result(operation_id: str, service: CUService = Depends(get_cu_service)) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id):
        raise fail(400, "invalid_operation_id", "Invalid operation ID.")
    url = f"{service.endpoint}/contentunderstanding/analyzerResults/{operation_id}?api-version={API_VERSION}"
    if not await service.delete_result(url):
        raise fail(502, "cu_delete_unconfirmed", "CU result deletion was not confirmed.")
    return {"status": "deletion_requested", "operation_id": operation_id, "compute_cancelled": False}
