"""Replica-safe, single-use Voice Live tickets in the existing workflow store.

Only SHA-256 digests are persisted. Cosmos ETags, not process-local locks, decide
which replica consumes a ticket. No container creation or application write retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import secrets
import time

from aiohttp import ClientError
from azure.core import MatchConditions
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError, ServiceResponseError
from azure.cosmos.exceptions import CosmosHttpResponseError

from .azure_clients import get_cosmos_client
from .settings import get_settings

TICKET_SECONDS = 30
ITEM_TTL_SECONDS = 60
STORE_TIMEOUT_SECONDS = 5
_KIND = "voice_connect_ticket"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_STORE_ERRORS = (
    CosmosHttpResponseError, ClientAuthenticationError, ServiceRequestError,
    ServiceResponseError, ClientError, ConnectionError, TimeoutError,
)


class InvalidVoiceTicket(ValueError):
    """Missing, expired, consumed, wrong-origin or otherwise invalid ticket."""


class VoiceTicketStoreUnavailable(RuntimeError):
    """Ticket storage failed; authorization must fail closed without a retry."""


def _container():
    database = get_cosmos_client().get_database_client(get_settings().cosmos_database)
    return database.get_container_client("workflow_state")


def ticket_record_id(ticket: str) -> str:
    if not isinstance(ticket, str) or not _TOKEN_PATTERN.fullmatch(ticket):
        raise InvalidVoiceTicket("Invalid voice connect ticket.")
    return "voice-ticket-" + hashlib.sha256(ticket.encode("ascii")).hexdigest()


class VoiceTicketStore:
    """Stateless instances share Cosmos; capacity bounds remain per API process."""

    async def issue(self, origin: str, lifetime_seconds: int = TICKET_SECONDS) -> str:
        if not isinstance(origin, str) or not origin or len(origin) > 256:
            raise InvalidVoiceTicket("A permitted browser origin is required.")
        if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= TICKET_SECONDS:
            raise InvalidVoiceTicket("Invalid voice ticket lifetime.")
        ticket = secrets.token_urlsafe(32)
        record_id = ticket_record_id(ticket)
        item = {
            "id": record_id, "workflowId": record_id, "kind": _KIND,
            "ticketDigest": record_id.removeprefix("voice-ticket-"),
            "expiresAtEpoch": time.time() + lifetime_seconds,
            "origin": origin, "status": "issued", "ttl": ITEM_TTL_SECONDS,
        }
        try:
            async with asyncio.timeout(STORE_TIMEOUT_SECONDS):
                await _container().create_item(body=item)
        except _STORE_ERRORS as exc:
            # An ambiguous create is never retried or changed into an upsert.
            raise VoiceTicketStoreUnavailable("Voice ticket store unavailable.") from exc
        return ticket

    async def consume(self, ticket: str, origin: str) -> None:
        record_id = ticket_record_id(ticket)
        try:
            async with asyncio.timeout(STORE_TIMEOUT_SECONDS):
                container = _container()
                try:
                    item = await container.read_item(item=record_id, partition_key=record_id)
                except CosmosHttpResponseError as exc:
                    if exc.status_code == 404:
                        raise InvalidVoiceTicket("Voice ticket expired or was not found.") from exc
                    raise
                expiry = item.get("expiresAtEpoch")
                if (item.get("id") != record_id or item.get("workflowId") != record_id
                        or item.get("kind") != _KIND
                        or item.get("ticketDigest") != record_id.removeprefix("voice-ticket-")
                        or item.get("origin") != origin or item.get("status") != "issued"
                        or type(expiry) not in (int, float) or not math.isfinite(expiry)
                        or expiry <= time.time()):
                    raise InvalidVoiceTicket("Voice ticket expired, was already used or has a different origin.")
                etag = item.get("_etag")
                if not isinstance(etag, str) or not etag:
                    raise VoiceTicketStoreUnavailable("Voice ticket ETag is unavailable.")
                replacement = {
                    key: item[key] for key in
                    ("id", "workflowId", "kind", "ticketDigest", "expiresAtEpoch", "origin")
                }
                replacement.update(status="consumed", ttl=ITEM_TTL_SECONDS)
                try:
                    await container.replace_item(
                        item=record_id, body=replacement, etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                    if expiry <= time.time():
                        raise InvalidVoiceTicket("Voice ticket expired during authorization.")
                except CosmosHttpResponseError as exc:
                    if exc.status_code in (404, 412):
                        raise InvalidVoiceTicket("Voice ticket expired or was already used.") from exc
                    raise
        except (InvalidVoiceTicket, VoiceTicketStoreUnavailable):
            raise
        except _STORE_ERRORS as exc:
            # A timed-out replace may have committed. Never replay it or authorize.
            raise VoiceTicketStoreUnavailable("Voice ticket store unavailable.") from exc


_store = VoiceTicketStore()


async def issue_ticket(origin: str) -> str:
    return await _store.issue(origin)


async def consume_ticket(ticket: str, origin: str) -> None:
    await _store.consume(ticket, origin)
