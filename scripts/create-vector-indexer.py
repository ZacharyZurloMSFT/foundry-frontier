"""Stand up the Northwind vector-search pipeline in Azure AI Search.

Creates (or upserts) four resources in the search service via REST so we
don't have to pin the SDK to a preview version:

    1. Index         — `northwind-knowledge` with a 1536-dim vector field and
                       an integrated Azure OpenAI vectorizer pointing at the
                       `text-embedding-3-small` deployment in Foundry.
    2. Data source   — `northwind-blob-ds` on the `seed` blob container, MI
                       auth (Search MI has Storage Blob Data Reader via
                       infra/modules/storage.bicep).
    3. Skillset      — `northwind-skillset` with SplitSkill (2000-char chunks,
                       500 overlap) + AzureOpenAIEmbeddingSkill.
    4. Indexer       — `northwind-indexer` that ties them together with
                       indexProjections (one row per chunk) and is run
                       on-demand at the end of this script.

Idempotent: PUT is upsert. The index is dropped + recreated if the existing
schema is missing the vector field or vectorizer (safe because we only ever
populate it via this pipeline).

Auth: prefers `AZURE_SEARCH_KEY` from fetch-keys; falls back to Entra ID.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential

_API_VERSION = "2024-11-01-preview"

_INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "northwind-knowledge")
_DATASOURCE = "northwind-blob-ds"
_SKILLSET = "northwind-skillset"
_INDEXER = "northwind-indexer"
_VECTOR_DIMS = 1536  # text-embedding-3-small


def _env(name: str, *, default: str = "", required: bool = True) -> str:
    v = os.environ.get(name, default).strip()
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


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


def _post(client: httpx.Client, url: str) -> None:
    r = client.post(url, headers=_search_headers())
    if r.status_code >= 400:
        raise RuntimeError(f"POST {url} -> {r.status_code}: {r.text}")


def _index_body(endpoint: str, aoai_endpoint: str, embedding_deployment: str) -> dict[str, Any]:
    return {
        "name": _INDEX_NAME,
        "fields": [
            {"name": "chunk_id", "type": "Edm.String", "key": True, "filterable": True,
             "sortable": True, "analyzer": "keyword"},
            {"name": "parent_id", "type": "Edm.String", "filterable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "content_vector", "type": "Collection(Edm.Single)", "searchable": True,
             "retrievable": False, "stored": True, "dimensions": _VECTOR_DIMS,
             "vectorSearchProfile": "hnsw-aoai"},
            {"name": "doc_type", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "site_id", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "equipment_vendor", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "equipment_model", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "page", "type": "Edm.Int32", "filterable": True, "retrievable": True},
            {"name": "source_uri", "type": "Edm.String", "retrievable": True},
        ],
        "vectorSearch": {
            "algorithms": [
                {"name": "hnsw-cfg", "kind": "hnsw",
                 "hnswParameters": {"m": 4, "efConstruction": 400, "efSearch": 500, "metric": "cosine"}}
            ],
            "vectorizers": [
                {"name": "aoai-vec", "kind": "azureOpenAI",
                 "azureOpenAIParameters": {
                     "resourceUri": aoai_endpoint,
                     "deploymentId": embedding_deployment,
                     "modelName": embedding_deployment,
                     # MI auth — Search MI has Cognitive Services User on Foundry.
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
                        {"fieldName": "equipment_vendor"},
                        {"fieldName": "equipment_model"},
                    ],
                },
            }]
        },
    }


def _needs_recreate(existing: dict[str, Any] | None) -> bool:
    if not existing:
        return False
    fields = existing.get("fields", [])
    has_vec = any(f.get("name") == "content_vector" for f in fields)
    has_vsearch = bool(existing.get("vectorSearch"))
    return not (has_vec and has_vsearch)


def _datasource_body(storage_account: str, container: str, subscription_id: str, rg: str) -> dict[str, Any]:
    # ResourceId-form connection string → Search uses its MI to auth to storage.
    conn = (
        f"ResourceId=/subscriptions/{subscription_id}/resourceGroups/{rg}"
        f"/providers/Microsoft.Storage/storageAccounts/{storage_account};"
    )
    return {
        "name": _DATASOURCE,
        "type": "azureblob",
        "credentials": {"connectionString": conn},
        "container": {"name": container},
    }


def _skillset_body(aoai_endpoint: str, embedding_deployment: str) -> dict[str, Any]:
    return {
        "name": _SKILLSET,
        "description": "Chunk + embed Northwind manuals/service reports/contracts for vector search.",
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
                "targetIndexName": _INDEX_NAME,
                "parentKeyFieldName": "parent_id",
                "sourceContext": "/document/pages/*",
                "mappings": [
                    {"name": "content", "source": "/document/pages/*"},
                    {"name": "content_vector", "source": "/document/pages/*/vector"},
                    {"name": "title", "source": "/document/metadata_title"},
                    {"name": "doc_type", "source": "/document/metadata_doc_type"},
                    {"name": "site_id", "source": "/document/metadata_site_id"},
                    {"name": "equipment_vendor", "source": "/document/metadata_equipment_vendor"},
                    {"name": "equipment_model", "source": "/document/metadata_equipment_model"},
                    {"name": "source_uri", "source": "/document/metadata_source_uri"},
                ],
            }],
            "parameters": {"projectionMode": "skipIndexingParentDocuments"},
        },
    }


def _indexer_body() -> dict[str, Any]:
    return {
        "name": _INDEXER,
        "dataSourceName": _DATASOURCE,
        "skillsetName": _SKILLSET,
        "targetIndexName": _INDEX_NAME,
        "parameters": {
            "batchSize": 5,
            "maxFailedItems": 5,
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "parsingMode": "default",
            },
        },
        "fieldMappings": [
            # Parent-level fields — used for parent_id + fallback for metadata.
            {"sourceFieldName": "metadata_storage_path", "targetFieldName": "parent_id",
             "mappingFunction": {"name": "base64Encode"}},
        ],
        "outputFieldMappings": [],
    }


def _wait_for_index_ready(client: httpx.Client, endpoint: str) -> None:
    """Search PUT returns immediately but the vectorizer needs a beat before
    the indexer will accept it. Retry the GET a few times."""
    url = f"{endpoint}/indexes/{_INDEX_NAME}?api-version={_API_VERSION}"
    for _ in range(10):
        if _get(client, url):
            return
        time.sleep(2)
    raise RuntimeError(f"Index {_INDEX_NAME} did not become readable.")


def main() -> None:
    endpoint = _env("AZURE_SEARCH_ENDPOINT").rstrip("/")
    storage_account = _env("STORAGE_ACCOUNT")
    container = os.environ.get("STORAGE_SEED_CONTAINER", "seed").strip() or "seed"
    subscription_id = _env("AZURE_SUBSCRIPTION_ID")
    rg = _env("AZURE_RESOURCE_GROUP")

    aoai_endpoint = _env("AZURE_OPENAI_ENDPOINT").rstrip("/")
    embedding_deployment = _env("FOUNDRY_EMBEDDING_DEPLOYMENT")

    print(f"Search endpoint: {endpoint}")
    print(f"  index:        {_INDEX_NAME}")
    print(f"  datasource:   {_DATASOURCE} -> {storage_account}/{container}")
    print(f"  skillset:     {_SKILLSET}")
    print(f"  indexer:      {_INDEXER}")
    print(f"  vectorizer:   {aoai_endpoint} -> {embedding_deployment} ({_VECTOR_DIMS} dims)")

    with httpx.Client(timeout=120.0) as client:
        # 1. Index -- drop + recreate if schema is out of date (missing vector).
        index_url = f"{endpoint}/indexes/{_INDEX_NAME}?api-version={_API_VERSION}"
        existing = _get(client, index_url)
        if _needs_recreate(existing):
            print(f"-> existing index {_INDEX_NAME} lacks vector fields; dropping to recreate")
            _delete(client, index_url)
            existing = None
        print(f"-> upserting index {_INDEX_NAME}")
        _put(client, index_url, _index_body(endpoint, aoai_endpoint, embedding_deployment))
        _wait_for_index_ready(client, endpoint)

        # 2. Data source
        ds_url = f"{endpoint}/datasources/{_DATASOURCE}?api-version={_API_VERSION}"
        print(f"-> upserting datasource {_DATASOURCE}")
        _put(client, ds_url, _datasource_body(storage_account, container, subscription_id, rg))

        # 3. Skillset
        ss_url = f"{endpoint}/skillsets/{_SKILLSET}?api-version={_API_VERSION}"
        print(f"-> upserting skillset {_SKILLSET}")
        _put(client, ss_url, _skillset_body(aoai_endpoint, embedding_deployment))

        # 4. Indexer
        idx_url = f"{endpoint}/indexers/{_INDEXER}?api-version={_API_VERSION}"
        print(f"-> upserting indexer {_INDEXER}")
        _put(client, idx_url, _indexer_body())

        # 5. Kick off a run -- resetting first so re-runs actually re-process
        # the blobs we just re-uploaded (blob timestamps may not have changed).
        print(f"-> resetting + running {_INDEXER}")
        _post(client, f"{endpoint}/indexers/{_INDEXER}/reset?api-version={_API_VERSION}")
        _post(client, f"{endpoint}/indexers/{_INDEXER}/run?api-version={_API_VERSION}")

    print("Vector indexer submitted. Check status:")
    print(f"  az search indexer status --service-name <name> --name {_INDEXER}")
    print("Foundry IQ knowledge base (create-knowledge-base.py) will re-use the same index.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
