"""Upload the explicit historical retail corpus to the `seed` blob container.

Reads `data/seed/generated/manifest.json` (produced by `generate.py`) and
uploads non-image historical retail documents to the storage account's
`seed` container. Insurance manifests are rejected before authentication or
cloud operations; use scripts/publish-insurance-knowledge.py for the isolated
insurance corpus instead.
Each blob is tagged with metadata (`doc_type`, `title`, `sku`, `category`,
`policy_ref`, `source_uri`) which the AI Search indexer projects into the
index as `metadata_*` fields.

Damaged-item photos (if any) are skipped — they go through the Content
Understanding pipeline, not the knowledge index.

Idempotent — overwrites blobs on re-run so the indexer sees fresh content.
Auth: prefers `AZURE_STORAGE_KEY` when populated by fetch-keys; falls back
to `DefaultAzureCredential` (managed identity in Container Apps, az CLI
locally).
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
from pathlib import Path

from azure.core.credentials import AzureNamedKeyCredential
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

_ROOT = Path(__file__).resolve().parents[2]
_GEN = Path(__file__).resolve().parent / "generated"
_MANIFEST = _GEN / "manifest.json"


def _blob_service() -> BlobServiceClient:
    account = os.environ.get("STORAGE_ACCOUNT", "").strip()
    if not account:
        raise RuntimeError("STORAGE_ACCOUNT is not set — run `azd provision` first.")

    key = os.environ.get("AZURE_STORAGE_KEY", "").strip()
    if key:
        return BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=AzureNamedKeyCredential(account, key),
        )
    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    cred = DefaultAzureCredential(managed_identity_client_id=client_id)
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=cred,
    )


def _sanitize_metadata_value(v: str) -> str:
    """Azure Blob metadata values must be ASCII/latin-1. Strip anything else
    (em-dashes, smart quotes, etc.) so uploads don't fail."""
    return "".join(ch if ord(ch) < 128 else "-" for ch in v)


def _blob_metadata(entry: dict) -> dict[str, str]:
    """Blob metadata keys must be ASCII, no dashes; string values must be
    ASCII too (see _sanitize_metadata_value). Azure Search projects these
    into fields called `metadata_<key>` after indexer field mappings, so
    keep names stable.
    """
    m: dict[str, str] = {"doc_type": entry.get("doc_type", "")}
    for k in ("title", "sku", "category", "policy_ref"):
        v = entry.get(k)
        if v:
            m[k] = _sanitize_metadata_value(str(v))
    if source := entry.get("source_uri") or entry.get("path"):
        m["source_uri"] = _sanitize_metadata_value(source)
    return m


def main() -> None:
    if not _MANIFEST.exists():
        raise RuntimeError(f"Missing manifest: {_MANIFEST}. Run generate.py first.")
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    if any(entry.get("business_domain") not in (None, "retail") for entry in manifest):
        raise RuntimeError(
            "This legacy uploader accepts historical retail manifests only. "
            "Use scripts/publish-insurance-knowledge.py (dry-run by default) "
            "for isolated insurance knowledge; the retail seed container must remain unchanged."
        )
    container = os.environ.get("STORAGE_SEED_CONTAINER", "seed").strip() or "seed"
    svc = _blob_service()
    cc = svc.get_container_client(container)
    try:
        cc.create_container()
    except Exception:  # noqa: BLE001 — already-exists is fine
        pass

    uploaded = skipped = 0
    for entry in manifest:
        # Damaged-item photos go through Content Understanding, not the KB index.
        if entry.get("doc_type") in ("damaged_item_photo", "nameplate_photo"):
            skipped += 1
            continue
        # `path` from manifest is relative to data/seed (e.g. "generated/foo.pdf").
        rel = entry.get("path")
        if not rel:
            skipped += 1
            continue
        # Manifest paths are relative to data/seed, not repo root.
        src = (_MANIFEST.parent / rel).resolve() if not (_ROOT / rel).exists() else (_ROOT / rel)
        if not src.exists():
            # Try one more resolution — manifest stores relative to seed/.
            src = _GEN / Path(rel).name
        if not src.exists():
            print(f"  ! missing source {rel}, skipping")
            skipped += 1
            continue

        blob_name = src.name  # flat namespace inside the seed container
        content_type, _ = mimetypes.guess_type(blob_name)
        settings = ContentSettings(content_type=content_type or "application/octet-stream")

        with src.open("rb") as fh:
            cc.upload_blob(
                name=blob_name,
                data=fh,
                overwrite=True,
                metadata=_blob_metadata(entry),
                content_settings=settings,
            )
        uploaded += 1
        print(f"  + {blob_name}")

    print(f"Uploaded {uploaded} blobs to {container} (skipped {skipped}).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
