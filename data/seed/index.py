"""Index the explicit historical retail corpus into Azure AI Search.

Creates the index if it doesn't exist, then uploads chunked documents.
Insurance manifests are rejected before authentication or any cloud operation;
use scripts/publish-insurance-knowledge.py for the isolated insurance corpus.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "apps" / "api"))

from azure.core.credentials import AzureKeyCredential  # noqa: E402
from azure.search.documents import SearchClient  # noqa: E402
from azure.search.documents.indexes import SearchIndexClient  # noqa: E402
from azure.search.documents.indexes.models import (  # noqa: E402
    SearchableField, SearchField, SearchFieldDataType, SearchIndex,
    SemanticConfiguration, SemanticField, SemanticPrioritizedFields, SemanticSearch,
    SimpleField,
)

from app.azure_clients import get_credential  # noqa: E402
from app.settings import get_settings  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "generated" / "manifest.json"


def _search_credential():
    """Key first, MI fallback — matches app-runtime behaviour."""
    s = get_settings()
    if s.azure_search_key:
        return AzureKeyCredential(s.azure_search_key)
    return get_credential()


def _index_schema(name: str) -> SearchIndex:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(name="doc_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="site_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="equipment_vendor", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="equipment_model", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="source_uri", type=SearchFieldDataType.String, retrievable=True),
    ]
    semantic = SemanticSearch(configurations=[SemanticConfiguration(
        name="default",
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"),
            content_fields=[SemanticField(field_name="content")],
        ),
    )])
    return SearchIndex(name=name, fields=fields, semantic_search=semantic)


def _chunk(text: str, size: int = 1500) -> list[str]:
    """Simple paragraph-boundary chunker."""
    out, buf = [], []
    length = 0
    for para in (text or "").split("\n\n"):
        if length + len(para) > size and buf:
            out.append("\n\n".join(buf))
            buf, length = [para], len(para)
        else:
            buf.append(para); length += len(para)
    if buf:
        out.append("\n\n".join(buf))
    return out or [""]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if any(entry.get("business_domain") not in (None, "retail") for entry in manifest):
        raise RuntimeError(
            "This legacy indexer accepts historical retail manifests only. "
            "Use scripts/publish-insurance-knowledge.py (dry-run by default) "
            "for isolated insurance knowledge; the retail index must remain unchanged."
        )
    s = get_settings()
    endpoint = s.search_endpoint or os.environ["AZURE_SEARCH_ENDPOINT"]
    index_name = s.search_index

    idx_client = SearchIndexClient(endpoint=endpoint, credential=_search_credential())
    existing = [i.name for i in idx_client.list_indexes()]
    if index_name not in existing:
        print(f"Creating index {index_name}...")
        idx_client.create_index(_index_schema(index_name))
    else:
        print(f"Index {index_name} already exists.")

    docs = []
    for entry in manifest:
        if entry["doc_type"] == "nameplate_photo":
            # Nameplate photos go through Content Understanding, not raw text upload.
            continue
        chunks = _chunk(entry.get("text", ""))
        for i, chunk in enumerate(chunks):
            docs.append({
                "id": f"{entry['id']}-c{i}",
                "title": entry.get("title", ""),
                "content": chunk,
                "doc_type": entry["doc_type"],
                "site_id": entry.get("site_id"),
                "equipment_vendor": entry.get("equipment_vendor"),
                "equipment_model": entry.get("equipment_model"),
                "page": i + 1,
                "source_uri": entry.get("source_uri") or entry.get("path"),
            })

    search = SearchClient(endpoint=endpoint, index_name=index_name, credential=_search_credential())
    result = search.upload_documents(documents=docs)
    print(f"Uploaded {sum(1 for r in result if r.succeeded)}/{len(docs)} chunks.")


if __name__ == "__main__":
    main()
