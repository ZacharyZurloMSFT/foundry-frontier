"""Async Foundry project helpers shared by the embedded assistants.

Note: ``azure-ai-projects`` >=2.x no longer exposes a threads/messages/runs API on
``project.agents``. Model calls go through the Responses API on
``project.get_openai_client()``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.identity.aio import (
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)

from .settings import get_settings


@lru_cache
def _async_credential():
    """Async credential — the sync `DefaultAzureCredential` in azure_clients.py
    is fine for Cosmos/Search/Blob (they support both), but when used with
    `AIProjectClient.aio` its sync token-chain (Azure CLI → VSCode → env → …)
    blocks uvicorn's event loop for seconds on first call. The async variant
    runs the same chain via httpx on the loop instead."""
    s = get_settings()
    if s.azure_client_id:
        return AsyncManagedIdentityCredential(client_id=s.azure_client_id)
    return AsyncDefaultAzureCredential()


def _project_client() -> AIProjectClient:
    # AIProjectClient stores the credential but doesn't call it until you hit
    # an ARM data-plane surface, so this credential path only fires when we're
    # running under managed identity in Container Apps. Preview enables the SDK's
    # API version and protocol headers.
    return AIProjectClient(
        endpoint=get_settings().foundry_project_endpoint,
        credential=_async_credential(),
        allow_preview=True,
    )


def _extract_output_text(response: Any) -> str:
    """Pull the assistant's plain text out of a Responses API result."""
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            t = getattr(content, "text", None)
            if isinstance(t, str) and t:
                parts.append(t)
            elif t is not None:
                val = getattr(t, "value", None)
                if val:
                    parts.append(val)
    return "".join(parts)
