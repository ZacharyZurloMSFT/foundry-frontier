"""Cosmos DB access helpers shared by the business store and the assistants' history."""
from __future__ import annotations

from datetime import datetime, timezone

from azure.cosmos.aio import ContainerProxy

from .azure_clients import get_cosmos_client
from .settings import get_settings


async def _container(name: str) -> ContainerProxy:
    s = get_settings()
    db = get_cosmos_client().get_database_client(s.cosmos_database)
    return db.get_container_client(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
