"""Azure credential + shared clients.

Prefers API-key auth when keys are present in the environment (populated by
`scripts/fetch-keys.{ps1,sh}` into `.env`), otherwise falls back to the
User-Assigned Managed Identity — so container-app runtime and local dev use
the same code path.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Union

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.cosmos.aio import CosmosClient
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import BlobServiceClient

from .settings import get_settings


@lru_cache
def get_credential() -> TokenCredential:
    """Managed-identity / interactive credential — used when no API key is set
    and for services that don't accept keys (e.g., Voice Live)."""
    settings = get_settings()
    if settings.azure_client_id:
        return ManagedIdentityCredential(client_id=settings.azure_client_id)
    return DefaultAzureCredential()


def get_foundry_credential() -> Union[AzureKeyCredential, TokenCredential]:
    """Foundry AI Services credential — key first, MI fallback."""
    s = get_settings()
    if s.foundry_api_key:
        return AzureKeyCredential(s.foundry_api_key)
    return get_credential()


@lru_cache
def get_search_client() -> SearchClient:
    s = get_settings()
    cred: Union[AzureKeyCredential, TokenCredential]
    cred = AzureKeyCredential(s.azure_search_key) if s.azure_search_key else get_credential()
    return SearchClient(endpoint=s.search_endpoint, index_name=s.search_index, credential=cred)


@lru_cache
def get_cosmos_client() -> CosmosClient:
    s = get_settings()
    # CosmosClient accepts a raw master-key string as credential.
    cred: Union[str, TokenCredential] = s.cosmos_key if s.cosmos_key else get_credential()
    return CosmosClient(url=s.cosmos_endpoint, credential=cred)


@lru_cache
def get_blob_client() -> BlobServiceClient:
    s = get_settings()
    if s.storage_connection_string:
        return BlobServiceClient.from_connection_string(s.storage_connection_string)
    return BlobServiceClient(
        account_url=f"https://{s.storage_account}.blob.core.windows.net",
        credential=get_credential(),
    )
