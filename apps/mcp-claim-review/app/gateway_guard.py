"""Opt-in ASGI guard: only requests relayed by the Northwind API gateway reach the app.

Azure API Management injects an Entra app-only access token (issued to its managed
identity) in ``X-Northwind-Gateway-Token``. With ``NORTHWIND_GATEWAY_AUTH_REQUIRED=true``
every request except the exempt health paths must carry a valid one, so calls made
straight to the Container App URL are rejected. In every other case the guard is a no-op.

Environment, read once when the middleware is built:
  NORTHWIND_GATEWAY_AUTH_REQUIRED  only "true" enables the guard
  NORTHWIND_GATEWAY_TENANT_ID      GUID of the Entra tenant that issues the token
  NORTHWIND_GATEWAY_AUDIENCE       API audience, a GUID or api://<GUID>
  NORTHWIND_GATEWAY_ALLOWED_OIDS   comma-separated object IDs allowed to call (APIM's identity)
Enabled with missing or malformed settings it fails closed (503) instead of allowing traffic.
An unknown key ID may force a signing-key refresh at most once per MIN_REFRESH_SECONDS, so
unauthenticated callers cannot turn requests into outbound fetches.

Self-contained on purpose (standard library, PyJWT and Starlette only): the MCP services get
this file copied verbatim by scripts/bundle-insurance-assets.py.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, NamedTuple

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ENABLED_ENV = "NORTHWIND_GATEWAY_AUTH_REQUIRED"
TENANT_ENV = "NORTHWIND_GATEWAY_TENANT_ID"
AUDIENCE_ENV = "NORTHWIND_GATEWAY_AUDIENCE"
OIDS_ENV = "NORTHWIND_GATEWAY_ALLOWED_OIDS"
TOKEN_HEADER = b"x-northwind-gateway-token"
MAX_TOKEN_LENGTH = 16384
MIN_REFRESH_SECONDS = 60
_API_SCHEME = "api://"
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class _Failure(NamedTuple):
    status: int
    code: str
    message: str


_REQUIRED = _Failure(401, "gateway_required", "Requests must come through the Northwind API gateway.")
_MISCONFIGURED = _Failure(
    503, "gateway_guard_misconfigured", "The Northwind API gateway guard is not configured correctly.",
)
_UNAVAILABLE = _Failure(503, "gateway_identity_unavailable", "Microsoft signing keys are temporarily unavailable.")


class _Rejected(ValueError):
    """A token that breaks gateway policy; the message is a fixed reason that is safe to log."""


@dataclass(frozen=True)
class _Config:
    tenant_id: str
    audiences: frozenset[str]
    allowed_oids: frozenset[str]

    @property
    def issuers(self) -> tuple[str, str]:
        return (
            f"https://sts.windows.net/{self.tenant_id}/",
            f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
        )


def _guid(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _GUID.fullmatch(value) else None


def _load_config(environ: Mapping[str, str]) -> tuple[_Config | None, list[str]]:
    """Return the validated settings, or None plus the names of the missing or malformed ones."""
    tenant = _guid(environ.get(TENANT_ENV, "").strip())
    audience = environ.get(AUDIENCE_ENV, "").strip()
    audience_guid = _guid(audience[len(_API_SCHEME):] if audience.lower().startswith(_API_SCHEME) else audience)
    oids = [_guid(item.strip()) for item in environ.get(OIDS_ENV, "").split(",") if item.strip()]
    invalid = [
        name for name, valid in (
            (TENANT_ENV, tenant), (AUDIENCE_ENV, audience_guid), (OIDS_ENV, oids and None not in oids),
        ) if not valid
    ]
    if invalid or tenant is None or audience_guid is None:
        return None, invalid
    return _Config(
        tenant, frozenset({audience_guid, _API_SCHEME + audience_guid}), frozenset(oid for oid in oids if oid),
    ), []


class _RefreshThrottled(jwt.PyJWKClientError):
    """An unknown key ID arrived before another signing-key refresh is allowed."""


class _ThrottledJWKClient(jwt.PyJWKClient):
    """PyJWKClient that honors a forced key-set refresh at most once per MIN_REFRESH_SECONDS.

    PyJWT refetches the key set whenever a token names an unknown kid. This guard answers on a
    publicly reachable URL, so unauthenticated callers could otherwise turn every request into an
    outbound fetch that occupies a worker thread. Inside the window an unknown kid is simply
    "no such key" (a rejection); the first fetch and lookups of cached keys are unchanged.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._refresh_lock = threading.Lock()
        self._last_refresh: float | None = None

    def get_signing_keys(self, refresh: bool = False) -> list[jwt.PyJWK]:
        if refresh:
            with self._refresh_lock:
                now = time.monotonic()
                if self._last_refresh is not None and now - self._last_refresh < MIN_REFRESH_SECONDS:
                    raise _RefreshThrottled("Signing key refresh is not allowed yet")
                self._last_refresh = now  # counted even when the fetch below fails
        return super().get_signing_keys(refresh)


@lru_cache(maxsize=4)
def signing_client(tenant_id: str) -> jwt.PyJWKClient:
    return _ThrottledJWKClient(
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
        cache_jwk_set=True, lifespan=300, timeout=5,
    )


async def _verify(token: str, config: _Config) -> None:
    if not 0 < len(token) <= MAX_TOKEN_LENGTH:
        raise _Rejected("token length out of bounds")
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 200:
        raise _Rejected("unsupported signing metadata")
    key = await run_in_threadpool(signing_client(config.tenant_id).get_signing_key_from_jwt, token)
    # The audience can be a bare GUID or api://<GUID> depending on the token version, so it is
    # matched below rather than by PyJWT (strict_aud accepts exactly one audience value).
    claims = jwt.decode(
        token, key.key, algorithms=["RS256"], issuer=config.issuers, leeway=30,
        options={"require": ["exp", "aud", "iss", "tid", "oid"], "verify_aud": False},
    )
    audience = claims["aud"]
    if not isinstance(audience, str) or audience.lower() not in config.audiences:
        raise _Rejected("audience mismatch")
    if claims.get("nbf") is None and claims.get("iat") is None:
        raise _Rejected("neither nbf nor iat present")
    if "scp" in claims:
        raise _Rejected("delegated user token")
    if _guid(claims["tid"]) != config.tenant_id:
        raise _Rejected("tenant mismatch")
    if _guid(claims["oid"]) not in config.allowed_oids:
        raise _Rejected("principal not allowed")


class GatewayGuardMiddleware:
    def __init__(
        self, app: ASGIApp, *, exempt_paths: frozenset[str] = frozenset(),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        flag = env.get(ENABLED_ENV, "").strip().lower()
        self.app = app
        self.exempt_paths = frozenset(exempt_paths)
        self.enabled = flag == "true"
        self.config: _Config | None = None
        if flag not in ("", "true", "false"):
            logger.warning("%s=%r is not 'true', so the gateway guard stays disabled.", ENABLED_ENV, flag)
        if self.enabled:
            self.config, invalid = _load_config(env)
            if self.config is None:
                logger.error(
                    "Gateway guard is enabled but %s must be set to valid GUID values; every request outside "
                    "the exempt paths is rejected with 503 until then.", ", ".join(invalid),
                )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] not in ("http", "websocket") or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        failure = await self._check(scope)
        if failure is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": failure.code})
        else:
            headers = {"Cache-Control": "no-store"}
            if failure.status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            response = JSONResponse(
                {"error": {"code": failure.code, "message": failure.message}},
                status_code=failure.status, headers=headers,
            )
            await response(scope, receive, send)

    async def _check(self, scope: Scope) -> _Failure | None:
        if self.config is None:
            return _MISCONFIGURED
        tokens = [value for name, value in scope.get("headers", ()) if name.lower() == TOKEN_HEADER]
        if len(tokens) != 1:
            logger.debug("Gateway token rejected: %s", "repeated header" if tokens else "no token")
            return _REQUIRED
        token = tokens[0].decode("latin-1").strip()
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        try:
            await _verify(token, self.config)
        except (jwt.PyJWKClientConnectionError, OSError) as exc:
            logger.warning("Gateway signing keys are unavailable: %s", type(exc).__name__)
            return _UNAVAILABLE
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            logger.debug("Gateway token rejected: %s", exc if isinstance(exc, _Rejected) else type(exc).__name__)
            return _REQUIRED
        return None
