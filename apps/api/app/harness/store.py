from __future__ import annotations

import hashlib
import time
import uuid

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from ..repositories import _container
from ..settings import get_settings
from .models import Event, HarnessError, RunRequest, RunState, now, redact

TTL_SECONDS = 7 * 24 * 60 * 60


def workflow_id(run_id: str) -> str:
    return f"wf-{run_id}"


class RunStore:
    async def create(self, request: RunRequest, model: str, prompt_sha: str,
                     timeout: int) -> tuple[dict, bool]:
        run_id = f"harness-{uuid.uuid5(uuid.NAMESPACE_URL, f'northwind-harness:{request.request_id}')}"
        wid = workflow_id(run_id)
        fingerprint = hashlib.sha256(request.message.encode()).hexdigest()
        item = {
            "id": wid, "workflowId": wid, "sessionId": run_id, "record_kind": "harness_run",
            "harness": "copilot-sdk", "department": "order-concierge",
            "foundry_project": get_settings().order_project_name, "status": "running", "model": model,
            "prompt_sha": prompt_sha, "request_hash": fingerprint,
            "input": redact(request.message), "output": "", "error": None,
            "createdAt": now(), "updatedAt": now(), "deadline": time.time() + timeout,
            "ttl": TTL_SECONDS,
            "business_domain": get_settings().business_domain,
        }
        container = await _container("workflow_state")
        try:
            return await container.create_item(item), True
        except CosmosResourceExistsError:
            existing = await container.read_item(wid, partition_key=wid)
            if (existing["request_hash"] != fingerprint
                    or existing.get("business_domain", "retail") != get_settings().business_domain):
                raise HarnessError("idempotency_conflict", "Request ID was already used for different input.")
            return existing, False

    async def append(self, run_id: str, event: Event) -> None:
        container = await _container("conversations")
        await container.create_item({
            "id": f"harness-event-{event.seq:06d}", "sessionId": run_id,
            "record_kind": "harness_event", "seq": event.seq,
            "event": event.model_dump(), "ts": event.ts, "ttl": TTL_SECONDS,
        })

    async def finish(self, run_id: str, status: RunState, output: str,
                     error: str | None = None) -> dict:
        container = await _container("workflow_state")
        wid = workflow_id(run_id)
        item = await container.read_item(wid, partition_key=wid)
        if item["status"] != "running":
            return item
        item.update(status=status, output=redact(output), error=error, updatedAt=now())
        try:
            return await container.replace_item(
                wid, item, etag=item["_etag"], match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            return await container.read_item(wid, partition_key=wid)

    async def approval(self, wid: str) -> dict | None:
        container = await _container("hitl_queue")
        try:
            item = await container.read_item(f"hitl-{wid}", partition_key=wid)
        except CosmosResourceNotFoundError:
            return None
        return {k: item[k] for k in ("id", "workflowId", "status", "payload", "kind",
                                    "business_domain", "synthetic", "execution_status", "review_scope") if k in item}

    async def require_active(self, wid: str, domain: str) -> None:
        container = await _container("workflow_state")
        item = await container.read_item(wid, partition_key=wid)
        if (item.get("record_kind") != "harness_run" or item.get("status") != "running"
                or item.get("business_domain", "retail") != domain
                or item.get("deadline", 0) <= time.time()):
            raise HarnessError("workflow_inactive", "The application-owned harness workflow is not active.")

    async def snapshot(self, run_id: str) -> dict:
        container = await _container("workflow_state")
        wid = workflow_id(run_id)
        item = await container.read_item(wid, partition_key=wid)
        if item.get("record_kind") != "harness_run":
            raise CosmosResourceNotFoundError(message="Harness run not found")
        if item.get("business_domain", "retail") != get_settings().business_domain:
            raise CosmosResourceNotFoundError(message="Harness run belongs to a different business domain")
        events_container = await _container("conversations")
        events = [
            doc["event"] async for doc in events_container.query_items(
                query="SELECT c.event FROM c WHERE c.sessionId = @sid AND c.record_kind = 'harness_event' ORDER BY c.seq",
                parameters=[{"name": "@sid", "value": run_id}], partition_key=run_id,
            )
        ]
        if item["status"] == "running" and time.time() > item["deadline"]:
            text = next((e["text"] for e in reversed(events) if e["kind"] == "assistant_message"), "")
            item = await self.finish(run_id, "interrupted", text,
                                     "The worker stopped or the run deadline expired. Check approval status before retrying.")
        return {
            "run_id": run_id, "workflow_id": wid, "status": item["status"],
            "input": item["input"], "output": item["output"], "error": item["error"],
            "model": item["model"], "prompt_sha": item["prompt_sha"],
            "created_at": item["createdAt"], "events": events,
            "business_domain": item.get("business_domain", "retail"),
            "approval": await self.approval(wid),
        }
