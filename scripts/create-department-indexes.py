"""Create + seed one Azure AI Search indexer per Northwind department.

The demo now has four departments — Order Concierge, Employee Help Desk,
Claims & Warranty Ops, and Customer Contact Center. Each department
gets its own Search index seeded from its department slice of the
corpus (see `data/northwind/departments/<slug>/seed/`).

**Order Concierge** is a special case: its corpus is generated as PDFs
by `data/seed/generate.py` and indexed by `scripts/create-vector-indexer.py`
into the existing `northwind-knowledge` index. We do NOT re-index it here.

For every other department this script:
  1. Ensures a `dept-seed` blob container exists.
  2. Uploads that department's `seed/*.md` files to
     `dept-seed/<slug>/*.md`.
  3. Ensures the department's Search index (`nw-<slug>`) exists with
     the shared vector schema (1536-dim `content_vector` + AOAI
     vectorizer profile).
  4. Ensures a per-department datasource (`nw-<slug>-ds`) points at
     `dept-seed` with `query: <slug>/` prefix filter.
  5. Ensures a per-department skillset (`nw-<slug>-skillset`) —
     SplitSkill + AzureOpenAIEmbeddingSkill projecting into the index.
  6. Ensures a per-department indexer (`nw-<slug>-indexer`).
  7. Resets + runs the indexer.

Everything is idempotent. Indexer runs are non-blocking — check status
via `Search service → Indexers` in the portal.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "apps" / "api"))

import httpx  # noqa: E402
from azure.core.credentials import AzureNamedKeyCredential  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.storage.blob import BlobServiceClient, ContentSettings  # noqa: E402

from app.settings import get_settings  # noqa: E402
from data.northwind import Department, load_company  # noqa: E402

_SKIP_SLUGS = {"order-concierge"}

_SEARCH_API_VERSION = "2024-11-01-preview"
_VECTOR_DIMS = 1536  # text-embedding-3-small
_BLOB_CONTAINER = "dept-seed"


# ── Search REST helpers ────────────────────────────────────────────────────
def _search_headers() -> dict[str, str]:
    key = os.environ.get("AZURE_SEARCH_KEY", "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["api-key"] = key
        return h
    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    cred = DefaultAzureCredential(managed_identity_client_id=client_id)
    h["Authorization"] = f"Bearer {cred.get_token('https://search.azure.com/.default').token}"
    return h


def _put(client: httpx.Client, url: str, body: dict[str, Any]) -> None:
    r = client.put(url, headers=_search_headers(), json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"PUT {url} -> {r.status_code}: {r.text}")


def _delete(client: httpx.Client, url: str) -> None:
    r = client.delete(url, headers=_search_headers())
    if r.status_code not in (200, 202, 204, 404):
        raise RuntimeError(f"DELETE {url} -> {r.status_code}: {r.text}")


def _get(client: httpx.Client, url: str) -> dict[str, Any] | None:
    r = client.get(url, headers=_search_headers())
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text}")
    return r.json()


def _post(client: httpx.Client, url: str, *, tolerate_409: bool = False) -> None:
    r = client.post(url, headers=_search_headers())
    if r.status_code == 409 and tolerate_409:
        print(f"    (409 conflict on {url.rsplit('/', 2)[-2]} — indexer already active, continuing)")
        return
    if r.status_code >= 400:
        raise RuntimeError(f"POST {url} -> {r.status_code}: {r.text}")


# ── Blob upload ────────────────────────────────────────────────────────────
def _blob_service() -> BlobServiceClient:
    account = os.environ.get("STORAGE_ACCOUNT", "").strip()
    if not account:
        raise RuntimeError("STORAGE_ACCOUNT not set — run azd provision first.")
    key = (os.environ.get("AZURE_STORAGE_KEY", "").strip()
           or os.environ.get("STORAGE_KEY", "").strip())
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


def _ensure_container(svc: BlobServiceClient, name: str) -> None:
    cc = svc.get_container_client(name)
    try:
        cc.create_container()
        print(f"  created blob container '{name}'")
    except Exception:  # noqa: BLE001 — already exists is fine
        pass


def _upload_department_markdown(svc: BlobServiceClient, dept: Department) -> int:
    if not dept.seed_dir.exists():
        return 0
    cc = svc.get_container_client(_BLOB_CONTAINER)
    settings = ContentSettings(content_type="text/markdown")
    count = 0
    for path in sorted(dept.seed_dir.rglob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        blob_name = f"{dept.slug}/{path.name}"
        with path.open("rb") as fh:
            cc.upload_blob(
                name=blob_name, data=fh, overwrite=True,
                content_settings=settings,
                metadata={
                    "title": path.stem.replace("-", " ").title(),
                    "department": dept.slug,
                    "doc_type": ("policy"
                                 if "policy" in path.stem or "handbook" in path.stem
                                 else "reference"),
                },
            )
        count += 1
    return count


# ── Search bodies ──────────────────────────────────────────────────────────
def _index_name_for(dept: Department) -> str:
    return dept.search_index


def _datasource_name_for(dept: Department) -> str:
    return f"nw-{dept.slug}-ds"


def _skillset_name_for(dept: Department) -> str:
    return f"{dept.search_index}-skillset"


def _indexer_name_for(dept: Department) -> str:
    return f"nw-{dept.slug}-indexer"


def _index_body(name: str, aoai_endpoint: str, embedding_deployment: str) -> dict[str, Any]:
    """Fresh schema built for the indexer path. The key field has
    `analyzer: keyword`, which indexProjections requires. This runs
    against a *deleted* previous index (see _cascade_delete_kb_ks_index)
    so we don't have to worry about attribute-drift errors."""
    return {
        "name": name,
        "fields": [
            {"name": "id", "type": "Edm.String",
             "searchable": True, "filterable": True, "retrievable": True,
             "stored": True, "sortable": True, "facetable": False, "key": True,
             "analyzer": "keyword"},
            {"name": "parent_id", "type": "Edm.String",
             "searchable": False, "filterable": True, "retrievable": True,
             "stored": True, "sortable": False, "facetable": False, "key": False},
            {"name": "title", "type": "Edm.String",
             "searchable": True, "filterable": False, "retrievable": True,
             "stored": True, "sortable": False, "facetable": False, "key": False},
            {"name": "content", "type": "Edm.String",
             "searchable": True, "filterable": False, "retrievable": True,
             "stored": True, "sortable": False, "facetable": False, "key": False},
            {"name": "content_vector", "type": "Collection(Edm.Single)",
             "searchable": True, "retrievable": False, "stored": True,
             "dimensions": _VECTOR_DIMS, "vectorSearchProfile": "hnsw-aoai"},
            {"name": "department", "type": "Edm.String",
             "searchable": False, "filterable": True, "retrievable": True,
             "stored": True, "sortable": False, "facetable": True, "key": False},
            {"name": "doc_type", "type": "Edm.String",
             "searchable": False, "filterable": True, "retrievable": True,
             "stored": True, "sortable": False, "facetable": True, "key": False},
            {"name": "source_path", "type": "Edm.String",
             "searchable": False, "filterable": False, "retrievable": True,
             "stored": True, "sortable": False, "facetable": False, "key": False},
        ],
        "vectorSearch": {
            "algorithms": [
                {"name": "hnsw-cfg", "kind": "hnsw",
                 "hnswParameters": {"m": 4, "efConstruction": 400,
                                    "efSearch": 500, "metric": "cosine"}}
            ],
            "vectorizers": [
                {"name": "aoai-vec", "kind": "azureOpenAI",
                 "azureOpenAIParameters": {
                     "resourceUri": aoai_endpoint,
                     "deploymentId": embedding_deployment,
                     "modelName": embedding_deployment,
                 }}
            ],
            "profiles": [
                {"name": "hnsw-aoai", "algorithm": "hnsw-cfg", "vectorizer": "aoai-vec"}
            ],
        },
        "semantic": {
            "configurations": [{
                "name": "default",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [{"fieldName": "content"}],
                    "prioritizedKeywordsFields": [
                        {"fieldName": "doc_type"},
                        {"fieldName": "department"},
                    ],
                },
            }]
        },
    }


def _datasource_body(name: str, dept: Department, storage_account: str,
                     subscription_id: str, rg: str) -> dict[str, Any]:
    conn = (
        f"ResourceId=/subscriptions/{subscription_id}/resourceGroups/{rg}"
        f"/providers/Microsoft.Storage/storageAccounts/{storage_account};"
    )
    return {
        "name": name,
        "type": "azureblob",
        "credentials": {"connectionString": conn},
        "container": {"name": _BLOB_CONTAINER, "query": f"{dept.slug}/"},
    }


def _skillset_body(name: str, aoai_endpoint: str, embedding_deployment: str,
                   index_name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Chunk + embed department markdown for vector search.",
        "skills": [
            {
                "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
                "name": "chunk",
                "description": "Split extracted document text into ~2000-char pages with overlap.",
                "context": "/document",
                "textSplitMode": "pages",
                "maximumPageLength": 2000,
                "pageOverlapLength": 500,
                "inputs": [{"name": "text", "source": "/document/content"}],
                "outputs": [{"name": "textItems", "targetName": "pages"}],
            },
            {
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "embed",
                "description": "Embed each chunk with text-embedding-3-small.",
                "context": "/document/pages/*",
                "resourceUri": aoai_endpoint,
                "deploymentId": embedding_deployment,
                "modelName": embedding_deployment,
                "dimensions": _VECTOR_DIMS,
                "inputs": [{"name": "text", "source": "/document/pages/*"}],
                "outputs": [{"name": "embedding", "targetName": "vector"}],
            },
        ],
        "indexProjections": {
            "selectors": [{
                "targetIndexName": index_name,
                "parentKeyFieldName": "parent_id",
                "sourceContext": "/document/pages/*",
                "mappings": [
                    {"name": "content", "source": "/document/pages/*"},
                    {"name": "content_vector", "source": "/document/pages/*/vector"},
                    {"name": "title", "source": "/document/metadata_title"},
                    {"name": "doc_type", "source": "/document/metadata_doc_type"},
                    {"name": "department", "source": "/document/metadata_department"},
                    {"name": "source_path", "source": "/document/metadata_storage_path"},
                ],
            }],
            # skipIndexingParentDocuments: chunks are the only documents
            # written to the index. The parent's metadata is captured on
            # each chunk via `title` / `doc_type` / etc. mappings above.
            "parameters": {"projectionMode": "skipIndexingParentDocuments"},
        },
    }


def _indexer_body(dept: Department, skillset_name: str) -> dict[str, Any]:
    return {
        "name": _indexer_name_for(dept),
        "dataSourceName": _datasource_name_for(dept),
        "skillsetName": skillset_name,
        "targetIndexName": _index_name_for(dept),
        "parameters": {
            "batchSize": 5,
            "maxFailedItems": 5,
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "parsingMode": "default",
            },
        },
        "fieldMappings": [
            {"sourceFieldName": "metadata_storage_path", "targetFieldName": "parent_id",
             "mappingFunction": {"name": "base64Encode"}},
        ],
        "outputFieldMappings": [],
    }


def _wait_for_index_ready(client: httpx.Client, endpoint: str, name: str) -> None:
    url = f"{endpoint}/indexes/{name}?api-version={_SEARCH_API_VERSION}"
    for _ in range(10):
        if _get(client, url):
            return
        time.sleep(2)
    raise RuntimeError(f"Index {name} did not become readable.")


def _cascade_delete_kb_ks_index(
    client: httpx.Client, endpoint: str, index_name: str,
) -> None:
    """The existing indexes were created by the earlier push-mode script
    without `analyzer: keyword` on their key field. Skillset
    indexProjections requires that analyzer. Since Search doesn't allow
    modifying existing field attributes, we have to drop + recreate the
    index. Foundry IQ knowledge sources reference the index and block
    deletion, and knowledge bases reference knowledge sources — so we
    cascade-delete KB -> KS -> index. `scripts/create-knowledge-base.py`
    then recreates the KB + KS on top of the fresh index."""
    ks_name = f"{index_name}-ks"
    kb_name = f"{index_name}-kb"
    for label, url in (
        ("knowledge base", f"{endpoint}/knowledgebases/{kb_name}?api-version=2026-05-01-preview"),
        ("knowledge source", f"{endpoint}/knowledgesources/{ks_name}?api-version=2026-05-01-preview"),
        ("index", f"{endpoint}/indexes/{index_name}?api-version={_SEARCH_API_VERSION}"),
    ):
        r = client.delete(url, headers=_search_headers())
        if r.status_code in (200, 202, 204):
            print(f"  cascade-deleted {label} {url.rsplit('/', 1)[1].split('?')[0]}")
        elif r.status_code == 404:
            pass  # already gone
        else:
            raise RuntimeError(f"DELETE {url} -> {r.status_code}: {r.text}")


# ── Main flow ──────────────────────────────────────────────────────────────
def _env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def main() -> None:
    s = get_settings()
    endpoint = (s.search_endpoint or os.environ.get("AZURE_SEARCH_ENDPOINT", "")).rstrip("/")
    if not endpoint:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT not set — run azd provision first.")

    aoai_endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or (
        (s.foundry_endpoint or "").rstrip("/")
        .replace(".cognitiveservices.azure.com", ".openai.azure.com")
    )).rstrip("/")
    if not aoai_endpoint:
        raise RuntimeError("Cannot resolve AzureOpenAI endpoint.")

    embedding_deployment = (
        os.environ.get("FOUNDRY_EMBEDDING_DEPLOYMENT", "").strip()
        or "text-embedding-3-small"
    )
    storage_account = _env_required("STORAGE_ACCOUNT")
    subscription_id = _env_required("AZURE_SUBSCRIPTION_ID")
    rg = _env_required("AZURE_RESOURCE_GROUP")

    company = load_company()
    print(f"Indexing {company.name} departments (skipping: {sorted(_SKIP_SLUGS)})")
    print(f"  Search:    {endpoint}")
    print(f"  AzureAI:   {aoai_endpoint}")
    print(f"  Embedding: {embedding_deployment} ({_VECTOR_DIMS}-dim)")
    print(f"  Blob:      {storage_account}/{_BLOB_CONTAINER}/<slug>/")

    svc = _blob_service()
    _ensure_container(svc, _BLOB_CONTAINER)

    total_blobs = 0
    with httpx.Client(timeout=120.0) as http:
        for dept in company.departments:
            if dept.slug in _SKIP_SLUGS:
                continue

            print(f"\n[{dept.slug}]")

            # 1. Upload markdown to blob.
            n = _upload_department_markdown(svc, dept)
            print(f"  uploaded {n} markdown blobs to {_BLOB_CONTAINER}/{dept.slug}/")
            total_blobs += n

            # 2. Cascade-delete existing KB + KS + index, then create fresh.
            # The old index was created without `analyzer: keyword` on the
            # key field, which the skillset indexProjections requires and
            # Search won't let us change in-place.
            index_name = _index_name_for(dept)
            _cascade_delete_kb_ks_index(http, endpoint, index_name)
            idx_url = f"{endpoint}/indexes/{index_name}?api-version={_SEARCH_API_VERSION}"
            print(f"  -> upserting index {index_name}")
            _put(http, idx_url, _index_body(index_name, aoai_endpoint, embedding_deployment))
            _wait_for_index_ready(http, endpoint, index_name)

            # 3. Datasource.
            ds_name = _datasource_name_for(dept)
            ds_url = f"{endpoint}/datasources/{ds_name}?api-version={_SEARCH_API_VERSION}"
            print(f"  -> upserting datasource {ds_name}")
            _put(http, ds_url, _datasource_body(ds_name, dept, storage_account,
                                                subscription_id, rg))

            # 4. Skillset.
            ss_name = _skillset_name_for(dept)
            ss_url = f"{endpoint}/skillsets/{ss_name}?api-version={_SEARCH_API_VERSION}"
            print(f"  -> upserting skillset {ss_name}")
            _put(http, ss_url,
                 _skillset_body(ss_name, aoai_endpoint, embedding_deployment, index_name))

            # 5. Indexer.
            ix_name = _indexer_name_for(dept)
            ix_url = f"{endpoint}/indexers/{ix_name}?api-version={_SEARCH_API_VERSION}"
            print(f"  -> upserting indexer {ix_name}")
            _put(http, ix_url, _indexer_body(dept, ss_name))

            # 6. Reset + run. Reset can 409 if the indexer is already
            # active from a prior partial run — safe to tolerate; the
            # subsequent run kicks a fresh pass either way.
            print(f"  -> resetting + running {ix_name}")
            _post(http, f"{endpoint}/indexers/{ix_name}/reset?api-version={_SEARCH_API_VERSION}",
                  tolerate_409=True)
            _post(http, f"{endpoint}/indexers/{ix_name}/run?api-version={_SEARCH_API_VERSION}",
                  tolerate_409=True)

    print(f"\nDone — uploaded {total_blobs} blobs; "
          f"{len(company.departments) - len(_SKIP_SLUGS)} indexers submitted.")
    print("NOTE: KBs + knowledge sources were deleted for the 3 new departments.")
    print("Run `python scripts/create-knowledge-base.py` next to recreate them,")
    print("then `python scripts/create-prompt-agents.py` to relink the agents.")
    print("Check indexer status: Search service → Indexers.")


if __name__ == "__main__":
    main()
