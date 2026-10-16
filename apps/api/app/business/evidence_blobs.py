"""Private immutable blobs in the existing cu-inbox container; no provisioning."""
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Literal, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError
from azure.identity.aio import ManagedIdentityCredential
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobClient, BlobServiceClient

from ..settings import Settings
from .evidence_models import BlobReference
from .models import BusinessError

CONTAINER = "cu-inbox"
MAX_BLOB_BYTES = 9 * 1024 * 1024


class EvidenceBlobs(Protocol):
    async def put(self, key: str, data: bytes, kind: Literal["source", "result"]) -> BlobReference: ...
    async def get(self, reference: BlobReference) -> bytes: ...


def blob_key(run_id: str, data: bytes, kind: str) -> str:
    scope = hashlib.sha256(run_id.encode()).hexdigest()
    return f"northwind-evidence/{scope}/{kind}/{hashlib.sha256(data).hexdigest()}"


def integrity_error() -> BusinessError:
    return BusinessError("evidence_integrity", "Private evidence integrity could not be confirmed.", 503)


class AzureEvidenceBlobs:
    def __init__(self, settings: Settings):
        self.account = settings.storage_account
        self.client_id = settings.azure_client_id

    @asynccontextmanager
    async def _client(self, key: str):
        if not self.account:
            raise BusinessError("evidence_storage_unconfigured", "Private evidence storage is not configured.", 503)
        async with ManagedIdentityCredential(client_id=self.client_id or None) as credential:
            async with BlobServiceClient(
                f"https://{self.account}.blob.core.windows.net", credential=credential,
                connection_timeout=5, read_timeout=15, retry_total=0,
            ) as service:
                yield service.get_blob_client(CONTAINER, key)

    async def put(self, key: str, data: bytes, kind: Literal["source", "result"]) -> BlobReference:
        if kind not in ("source", "result"):
            raise ValueError("Unknown blob kind")
        sha = hashlib.sha256(data).hexdigest()
        metadata = {"sha256": sha, "size_bytes": str(len(data)), "kind": kind}
        async with self._client(key) as client:
            try:
                await client.upload_blob(
                    data, overwrite=False, metadata=metadata,
                    content_settings=ContentSettings(content_type="application/octet-stream"),
                )
            except ResourceExistsError:
                # Shared content-addressed objects are never deleted on a losing CAS.
                pass
            properties = await client.get_blob_properties()
            if properties.metadata != metadata or properties.size != len(data):
                raise integrity_error()
            reference = BlobReference(
                key=key, sha256=sha, size_bytes=len(data), etag=str(properties.etag), kind=kind,
            )
            await self._read(client, reference)
            return reference

    @staticmethod
    async def _read(client: BlobClient, reference: BlobReference) -> bytes:
        maximum = 10 * 1024 * 1024 if reference.kind == "source" else MAX_BLOB_BYTES
        if not 0 < reference.size_bytes <= maximum:
            raise integrity_error()
        properties = await client.get_blob_properties()
        if (
            properties.metadata != {
                "sha256": reference.sha256, "size_bytes": str(reference.size_bytes), "kind": reference.kind,
            }
            or properties.size != reference.size_bytes or str(properties.etag) != reference.etag
        ):
            raise integrity_error()
        download = await client.download_blob(etag=reference.etag, match_condition=MatchConditions.IfNotModified)
        data = bytearray()
        async for chunk in download.chunks():
            if len(data) + len(chunk) > reference.size_bytes:
                raise integrity_error()
            data.extend(chunk)
        if len(data) != reference.size_bytes or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise integrity_error()
        return bytes(data)

    async def get(self, reference: BlobReference) -> bytes:
        async with self._client(reference.key) as client:
            return await self._read(client, reference)
