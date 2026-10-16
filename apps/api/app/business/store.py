"""One bounded run aggregate: claims, receipts and history share an atomic write."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import TypeAdapter

from ..repositories import _container
from .models import BusinessError, ConcurrentChange, DemoRun, RunId

MAX_RUN_BYTES = 1_500_000
RUN_ID: TypeAdapter[str] = TypeAdapter(RunId)
RECORD_KIND = "insurance_business_run"


@dataclass(frozen=True)
class StoredRun:
    state: DemoRun
    etag: str


class BusinessRepository(Protocol):
    async def create(self, state: DemoRun) -> StoredRun: ...
    async def get(self, run_id: str) -> StoredRun: ...
    async def replace(self, state: DemoRun, etag: str) -> StoredRun: ...


def workflow_id(run_id: str) -> str:
    return f"insurance-run-{RUN_ID.validate_python(run_id)}"


def _document(state: DemoRun) -> dict:
    key = workflow_id(state.run_id)
    document = {
        "id": key, "workflowId": key, "record_kind": RECORD_KIND,
        "state": state.model_dump(mode="json"),
    }
    if len(json.dumps(document, ensure_ascii=True).encode("utf-8")) > MAX_RUN_BYTES:
        raise BusinessError("run_capacity", "This bounded demo run has reached its storage limit.", 409)
    return document


def _stored(document: dict, run_id: str) -> StoredRun:
    if document.get("record_kind") != RECORD_KIND:
        raise BusinessError("not_found", "Demo run not found.", 404)
    state = DemoRun.model_validate(document["state"])
    if state.run_id != run_id or document["id"] != workflow_id(run_id):
        raise BusinessError("invalid_storage_scope", "Stored demo run scope does not match.", 500)
    return StoredRun(state, document["_etag"])


class CosmosBusinessRepository:
    async def create(self, state: DemoRun) -> StoredRun:
        document = _document(state)
        container = await _container("workflow_state")
        try:
            result = await container.create_item(document)
        except CosmosResourceExistsError as exc:
            raise BusinessError("run_exists", "This demo run already exists; it was not reseeded.", 409) from exc
        return _stored(result, state.run_id)

    async def get(self, run_id: str) -> StoredRun:
        key = workflow_id(run_id)
        container = await _container("workflow_state")
        try:
            result = await container.read_item(key, partition_key=key)
        except CosmosResourceNotFoundError as exc:
            raise BusinessError("not_found", "Demo run not found.", 404) from exc
        return _stored(result, run_id)

    async def replace(self, state: DemoRun, etag: str) -> StoredRun:
        document = _document(state)
        container = await _container("workflow_state")
        try:
            result = await container.replace_item(
                document["id"], document, etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            raise ConcurrentChange() from exc
        return _stored(result, state.run_id)
