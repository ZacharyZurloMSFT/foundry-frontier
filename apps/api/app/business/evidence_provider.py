"""Split CU transport for durable workers; never use the legacy all-in-one analyze."""
from __future__ import annotations

import base64
import json
import math
import re
from typing import Protocol

import httpx
from azure.core.exceptions import AzureError
from fastapi import HTTPException

from ..content_understanding import API_VERSION, MAX_RESPONSE_BYTES, REQUIRED_FIELD_TYPES, CUService, operation_url
from .evidence_models import MAX_PROJECTED_FIELDS_BYTES, MAX_PROVENANCE_BYTES, ExtractedField


class ProviderFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EvidenceProvider(Protocol):
    async def preflight(self) -> dict[str, str]: ...
    async def submit(self, data: bytes, filename: str, media_type: str) -> str: ...
    async def poll(self, location: str, provenance: dict[str, str]) -> dict | None: ...


def validate_provenance(provenance: dict[str, str]) -> dict[str, str]:
    if (
        not isinstance(provenance, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in provenance.items())
        or not all(isinstance(provenance.get(key), str) for key in ("endpoint", "analyzer_id", "model"))
        or len(provenance["endpoint"]) > 512 or len(provenance["analyzer_id"]) > 128
        or len(provenance["model"]) > 128
        or len(json.dumps(provenance, ensure_ascii=True).encode()) > MAX_PROVENANCE_BYTES
    ):
        raise ProviderFailure("cu_provenance_too_large")
    return provenance


def validate_result(result: object, analyzer_id: str) -> dict:
    if not isinstance(result, dict) or result.get("analyzerId") != analyzer_id:
        raise ProviderFailure("cu_invalid_result")
    contents = result.get("contents")
    if not isinstance(contents, list) or not contents or not all(
        isinstance(content, dict) and isinstance(content.get("fields", {}), dict)
        and all(isinstance(value, dict) and isinstance(value.get("type"), str)
                for value in content.get("fields", {}).values())
        for content in contents
    ):
        raise ProviderFailure("cu_invalid_result")
    try:
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ProviderFailure("cu_result_too_large")
    except (ValueError, TypeError, RecursionError):
        raise ProviderFailure("cu_invalid_result") from None
    return result


class CUEvidenceProvider:
    def __init__(self, service: CUService):
        self.service = service
        self.deployments: dict[str, str] = {}

    async def preflight(self) -> dict[str, str]:
        try:
            analyzer, self.deployments = await self.service.analyzer()
        except (HTTPException, httpx.HTTPError, AzureError, TimeoutError):
            raise ProviderFailure("cu_analyzer_unavailable") from None
        return {
            "endpoint": self.service.endpoint, "analyzer_id": self.service.config.analyzer_id,
            "model": analyzer["models"]["completion"], "api_version": API_VERSION,
            "model_deployments": json.dumps(self.deployments, sort_keys=True),
        }

    async def submit(self, data: bytes, filename: str, media_type: str) -> str:
        try:
            _, headers = await self.service.request(
                "POST",
                f"{self.service.endpoint}/contentunderstanding/analyzers/{self.service.config.analyzer_id}:analyze?api-version={API_VERSION}",
                json={
                    "inputs": [{"name": filename, "mimeType": media_type, "data": base64.b64encode(data).decode("ascii")}],
                    "modelDeployments": self.deployments,
                },
            )
            return operation_url(headers.get("Operation-Location", ""), self.service.endpoint)
        except (HTTPException, httpx.HTTPError, AzureError, TimeoutError):
            # Even a malformed/missing response may follow an accepted paid POST.
            raise ProviderFailure("cu_submission_uncertain") from None

    async def poll(self, location: str, provenance: dict[str, str]) -> dict | None:
        if (
            provenance.get("endpoint") != self.service.endpoint
            or provenance.get("analyzer_id") != self.service.config.analyzer_id
        ):
            raise ProviderFailure("cu_configuration_changed")
        try:
            body, _ = await self.service.request("GET", operation_url(location, self.service.endpoint))
        except (HTTPException, httpx.HTTPError, AzureError, TimeoutError):
            raise ProviderFailure("cu_poll_unavailable") from None
        status = str(body.get("status", "")).lower()
        if status == "succeeded":
            return validate_result(body.get("result"), self.service.config.analyzer_id)
        if status in ("running", "notstarted", "pending"):
            return None
        if status in ("failed", "cancelled", "canceled"):
            raise ProviderFailure("cu_analysis_failed")
        raise ProviderFailure("cu_invalid_operation")


def project_fields(result: dict) -> dict[str, ExtractedField]:
    """Small review projection only; complete typed fields remain in the result blob."""
    projected = {}
    for name, kind in REQUIRED_FIELD_TYPES.items():
        raw: dict = next((
            content.get("fields", {}).get(name) for content in result["contents"]
            if content.get("fields", {}).get(name) is not None
        ), {})
        value = raw.get({"string": "valueString", "date": "valueDate", "number": "valueNumber"}[kind], raw.get("value"))
        if (
            isinstance(value, bool) or not isinstance(value, (str, int, float, type(None)))
            or isinstance(value, str) and len(value) > 200
            or isinstance(value, (int, float)) and (abs(value) > 1e15 or not math.isfinite(value))
        ):
            value = None
        confidence = raw.get("confidence")
        if (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1 or not math.isfinite(confidence)
        ):
            confidence = None
        spans: list[dict | list | str | int | float | None] = []
        for source_key in ("source", "spans", "sourceSpans", "boundingRegions"):
            source = raw.get(source_key)
            if source is not None:
                try:
                    serialized = json.dumps(source, allow_nan=False)
                    if len(serialized.encode()) <= 2048 and not re.search(r"https?://", serialized, re.IGNORECASE):
                        # Preserve the original key and structured geometry, not a provider URL.
                        spans.append({source_key: source})
                except (ValueError, TypeError, RecursionError):
                    continue
        projected[name] = ExtractedField(
            type=kind, value=value, confidence=confidence, source_spans=spans,
            missing=value is None,
        )
    if len(json.dumps({name: field.model_dump(mode="json") for name, field in projected.items()},
                      ensure_ascii=True).encode()) > MAX_PROJECTED_FIELDS_BYTES:
        raise ProviderFailure("cu_projection_too_large")
    return projected
