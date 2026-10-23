"""Delegated Entra access-token verification; role mapping remains in the run."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from ..settings import get_settings
from .models import BusinessError

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthConfiguration:
    tenant_id: str
    audience: str
    scope: str
    client_ids: tuple[str, ...]

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"


@dataclass(frozen=True)
class Principal:
    subject_id: str


def subject_id(tenant_id: str, object_id: str) -> str:
    return f"{uuid.UUID(tenant_id)}:{uuid.UUID(object_id)}"


def configuration() -> AuthConfiguration:
    settings = get_settings()
    if not settings.business_api_enabled:
        raise BusinessError("business_api_disabled", "The connected insurance journey is not enabled.", 503)
    try:
        tenant = str(uuid.UUID(settings.business_auth_tenant_id))
        audience = str(uuid.UUID(settings.business_auth_audience))
        clients = tuple(sorted({str(uuid.UUID(value.strip())) for value in settings.business_auth_client_ids.split(",")}))
        if not clients or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,100}", settings.business_auth_scope):
            raise ValueError("Invalid delegated scope configuration")
    except ValueError as exc:
        raise BusinessError(
            "identity_not_configured", "Approved insurance tenant, API audience, scope and client IDs must be configured.", 503,
        ) from exc
    if not settings.cosmos_endpoint:
        raise BusinessError("storage_not_configured", "Canonical insurance storage is not configured.", 503)
    return AuthConfiguration(tenant, audience, settings.business_auth_scope, clients)


@lru_cache(maxsize=4)
def signing_client(tenant_id: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
        cache_jwk_set=True, lifespan=300, timeout=5,
    )


async def verify_access_token(token: str, config: AuthConfiguration) -> Principal:
    try:
        if len(token) > 16384:
            raise ValueError("Token exceeds the bounded limit")
        header = jwt.get_unverified_header(token)
        if (header.get("alg") != "RS256" or not isinstance(header.get("kid"), str)
                or not 1 <= len(header["kid"]) <= 200):
            raise ValueError("Unsupported signing metadata")
        key = await run_in_threadpool(signing_client(config.tenant_id).get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=config.audience, issuer=config.issuer,
            leeway=30,
            options={
                "require": ["exp", "iat", "nbf", "aud", "iss", "tid", "oid", "scp", "ver", "azp"],
                "strict_aud": True,
            },
        )
        if (claims["ver"] != "2.0" or claims.get("idtyp") == "app"
                or not all(isinstance(claims[name], str) for name in ("tid", "oid", "azp", "scp"))
                or str(uuid.UUID(claims["tid"])) != config.tenant_id
                or str(uuid.UUID(claims["azp"])) not in config.client_ids
                or config.scope not in claims["scp"].split()):
            raise ValueError("Token does not grant the required delegated API access")
        return Principal(subject_id(claims["tid"], claims["oid"]))
    except jwt.PyJWKClientConnectionError as exc:
        raise BusinessError("identity_unavailable", "Microsoft signing keys are temporarily unavailable.", 503) from exc
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise BusinessError("invalid_access_token", "A valid delegated insurance API access token is required.", 401) from exc


async def authenticated_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    config = configuration()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise BusinessError("authentication_required", "Sign in to access the insurance account.", 401)
    return await verify_access_token(credentials.credentials, config)
