"""Cosmos DB repository helpers — conversations, workflow state, HITL queue."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from azure.core import MatchConditions
from azure.cosmos import PartitionKey
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError

from .azure_clients import get_cosmos_client
from .settings import get_settings
from .insurance_domain import ACTION_KIND, ClaimActionDraft


async def _container(name: str) -> ContainerProxy:
    s = get_settings()
    db = get_cosmos_client().get_database_client(s.cosmos_database)
    return db.get_container_client(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Conversations ──────────────────────────────────────────────────────────
async def append_message(session_id: str, role: str, content: str, meta: dict | None = None) -> dict:
    c = await _container("conversations")
    item = {
        "id": str(uuid.uuid4()),
        "sessionId": session_id,
        "role": role,
        "content": content,
        "meta": meta or {},
        "ts": _now(),
    }
    await c.create_item(item)
    return item


async def load_conversation(session_id: str, limit: int = 50) -> list[dict]:
    c = await _container("conversations")
    query = "SELECT * FROM c WHERE c.sessionId = @sid ORDER BY c.ts ASC OFFSET 0 LIMIT @lim"
    params = [{"name": "@sid", "value": session_id}, {"name": "@lim", "value": limit}]
    items: list[dict] = []
    async for item in c.query_items(query=query, parameters=params, partition_key=session_id):
        items.append(item)
    return items


# ── Workflow state ─────────────────────────────────────────────────────────
async def save_workflow(workflow_id: str, state: dict) -> dict:
    c = await _container("workflow_state")
    state["id"] = workflow_id
    state["workflowId"] = workflow_id
    state["updatedAt"] = _now()
    return await c.upsert_item(state)


async def load_workflow(workflow_id: str) -> dict | None:
    c = await _container("workflow_state")
    try:
        return await c.read_item(item=workflow_id, partition_key=workflow_id)
    except Exception:
        return None


# ── HITL queue ─────────────────────────────────────────────────────────────
async def enqueue_hitl(workflow_id: str, kind: str, payload: dict) -> dict:
    c = await _container("hitl_queue")
    item = {
        "id": str(uuid.uuid4()),
        "workflowId": workflow_id,
        "kind": kind,
        "status": "pending",
        "payload": payload,
        "createdAt": _now(),
    }
    if kind == ACTION_KIND:
        ClaimActionDraft.model_validate(payload)
        item.update(business_domain="insurance", synthetic=True, execution_status="not_executed",
                    sessionId=None)
    await c.create_item(item)
    return item


async def list_hitl_pending(limit: int = 50, workflow_id: str | None = None) -> list[dict]:
    c = await _container("hitl_queue")
    query = "SELECT TOP @lim * FROM c WHERE c.status = 'pending'"
    params = [{"name": "@lim", "value": limit}]
    options = {}
    if workflow_id:
        query += " AND c.workflowId = @workflow"
        params.append({"name": "@workflow", "value": workflow_id})
        options["partition_key"] = workflow_id
    query += " ORDER BY c.createdAt ASC"
    items: list[dict] = []
    # Newer azure-cosmos.aio drops the enable_cross_partition_query kwarg and
    # infers cross-partition from the absence of partition_key. Cross-partition
    # scans are enabled by default when partition_key is not supplied.
    async for it in c.query_items(query=query, parameters=params, **options):
        items.append(it)
    return items


async def list_hitl_resolved(limit: int = 50) -> list[dict]:
    """Approved or rejected work orders, most-recent first — powers the
    Work Orders tab."""
    c = await _container("hitl_queue")
    query = (
        "SELECT TOP @lim * FROM c WHERE c.status IN ('approved', 'rejected') "
        "ORDER BY c.resolvedAt DESC"
    )
    params = [{"name": "@lim", "value": limit}]
    items: list[dict] = []
    async for it in c.query_items(query=query, parameters=params):
        items.append(it)
    return items


async def resolve_hitl(hitl_id: str, workflow_id: str, decision: str, note: str = "") -> dict:
    if decision not in {"approve", "reject"}:
        raise ValueError("Decision must be approve or reject")
    c = await _container("hitl_queue")
    item = await c.read_item(item=hitl_id, partition_key=workflow_id)
    target = "approved" if decision == "approve" else "rejected"
    harness = workflow_id.startswith("wf-harness-")
    insurance = item.get("kind") == ACTION_KIND
    if insurance:
        ClaimActionDraft.model_validate(item["payload"])
        item["execution_status"] = "not_executed"
        item["review_scope"] = "draft_only"
    if (harness or insurance) and item["status"] != "pending":
        if item["status"] == target:
            return {**item, "_decision_replayed": True}
        raise ValueError("This draft has already been decided.")
    item["status"] = "approved" if decision == "approve" else "rejected"
    item["decision"] = decision
    item["note"] = note
    item["resolvedAt"] = _now()
    if harness or insurance:
        try:
            return await c.replace_item(hitl_id, item, etag=item["_etag"],
                                        match_condition=MatchConditions.IfNotModified)
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            winner = await c.read_item(hitl_id, partition_key=workflow_id)
            if winner["status"] == target:
                return {**winner, "_decision_replayed": True}
            raise ValueError("A conflicting approval decision was saved.") from exc
    return await c.upsert_item(item)


async def delete_hitl(hitl_id: str, workflow_id: str) -> None:
    """Hard-delete a HITL queue item. Used by the Work Orders tab so
    resolved dispatches can be pruned once they're no longer relevant to
    the demo (post-dispatch, or accidental duplicates). Idempotent —
    a missing row is treated as already-deleted."""
    c = await _container("hitl_queue")
    try:
        await c.delete_item(item=hitl_id, partition_key=workflow_id)
    except Exception:  # noqa: BLE001 — Cosmos raises for 404; caller only needs "gone".
        return
