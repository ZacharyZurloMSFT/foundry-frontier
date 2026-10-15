"""Content Understanding ingestion worker.

Watches the `cu-inbox` blob container. For each new blob:
  1. Classify by extension → doc_type analyzer
  2. Call Azure AI Content Understanding with the appropriate analyzer schema
  3. Persist structured output to Cosmos `cu_extractions`
  4. If applicable, upload derived text back into AI Search

Runs as a small HTTP server exposing /run so it can be invoked on a timer or
by an Event Grid subscription (out of scope for the initial demo).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT.parent / "apps" / "api"))

from app.azure_clients import get_blob_client, get_cosmos_client, get_credential  # noqa: E402
from app.observability import configure_observability, get_tracer  # noqa: E402
from app.settings import get_settings  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cu-ingest")

configure_observability()
tracer = get_tracer("cu.ingest")

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

ANALYZERS = {
    "service_report":       "northwind-service-report",
    "warranty_pdf":         "northwind-warranty",
    "emailed_work_order":   "northwind-work-order",
    "nameplate_photo":      "northwind-nameplate",
    # Claims & Warranty Ops department — see
    # data/northwind/departments/claims-warranty/cu-analyzers/claim-intake.json
    "claim_intake":         "northwind-claim-intake",
}


# Blob-prefix -> (department, override doc_type when the classifier
# should not run). The Claims dept publishes intake forms to
# cu-inbox/claims-warranty/* — everything in that prefix is a claim
# intake regardless of extension. Field Services keeps the flat
# cu-inbox/* namespace so pre-multi-project blobs still route correctly.
DEPT_PREFIX_ROUTES: dict[str, tuple[str, str | None]] = {
    "claims-warranty/":     ("claims-warranty", "claim_intake"),
    "employee-help-desk/":  ("employee-help-desk", None),
    "contact-center/":      ("contact-center", None),
    "field-services/":      ("field-services", None),
}


def route_department(blob_name: str) -> tuple[str, str | None]:
    """Given a blob path, return (department, forced_doc_type).
    Falls back to field-services + no override for legacy flat blobs."""
    for prefix, (dept, doc_type) in DEPT_PREFIX_ROUTES.items():
        if blob_name.startswith(prefix):
            return dept, doc_type
    return "field-services", None


def classify(blob_name: str) -> str:
    # Strip department prefix before extension/name checks so a blob at
    # cu-inbox/claims-warranty/nameplate.jpg still classifies as photo.
    stripped = blob_name
    for prefix in DEPT_PREFIX_ROUTES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    lower = stripped.lower()
    if lower.endswith((".jpg", ".jpeg", ".png")):
        return "nameplate_photo"
    if "warranty" in lower:
        return "warranty_pdf"
    if "work_order" in lower or "wo-" in lower:
        return "emailed_work_order"
    if "intake" in lower or "claim" in lower:
        return "claim_intake"
    return "service_report"


async def _analyze(analyzer_id: str, blob_url: str) -> dict[str, Any]:
    """Call Azure AI Content Understanding analyze API."""
    s = get_settings()
    cred = get_credential()
    token = cred.get_token("https://cognitiveservices.azure.com/.default")
    endpoint = s.foundry_endpoint.rstrip("/")
    url = f"{endpoint}/contentunderstanding/analyzers/{analyzer_id}:analyze?api-version=2024-12-01-preview"
    with tracer.start_as_current_span("cu.analyze") as span:
        span.set_attribute("analyzer_id", analyzer_id)
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.post(
                url,
                headers={"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"},
                json={"url": blob_url},
            )
            r.raise_for_status()
            # CU returns 202 with an operation-location; poll it.
            op_url = r.headers.get("operation-location")
            if not op_url:
                return r.json()
            for _ in range(30):
                await asyncio.sleep(2)
                poll = await http.get(op_url, headers={"Authorization": f"Bearer {token.token}"})
                poll.raise_for_status()
                data = poll.json()
                if data.get("status") in {"Succeeded", "Failed"}:
                    return data
            return {"status": "Timeout"}


async def ingest_once() -> dict[str, int]:
    s = get_settings()
    counts = {"processed": 0, "skipped": 0, "errors": 0}
    blob = get_blob_client()
    container = blob.get_container_client("cu-inbox")
    cosmos = get_cosmos_client().get_database_client(s.cosmos_database).get_container_client("cu_extractions")

    async for props in container.list_blobs():
        try:
            department, forced_type = route_department(props.name)
            doc_type = forced_type or classify(props.name)
            analyzer_id = ANALYZERS[doc_type]
            blob_url = f"https://{s.storage_account}.blob.core.windows.net/cu-inbox/{props.name}"
            log.info("Analyzing %s (dept=%s, doc_type=%s)", props.name, department, doc_type)
            with tracer.start_as_current_span("cu.ingest.item") as span:
                span.set_attribute("department", department)
                span.set_attribute("foundry_project", f"nw-{department}")
                span.set_attribute("doc_type", doc_type)
                result = await _analyze(analyzer_id, blob_url)
            item = {
                "id": props.name.replace("/", "_"),
                "docType": doc_type,
                "department": department,
                "sourceBlob": props.name,
                "result": result,
            }
            await cosmos.upsert_item(item)
            counts["processed"] += 1
        except Exception as exc:  # noqa: BLE001,F841
            log.exception("Failed to process %s", props.name)
            counts["errors"] += 1

    return counts


app = FastAPI(title="Northwind CU Ingest", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/run")
async def run() -> dict:
    return await ingest_once()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
