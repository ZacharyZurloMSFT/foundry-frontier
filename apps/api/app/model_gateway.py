"""Route the API app's model inference through the Azure API Management AI Gateway.

When ``AI_GATEWAY_MODEL_ENDPOINT`` is set, the safety check, the model bake-off,
grounded adjuster support and the Copilot harness send their model calls to the
gateway's OpenAI-compatible ``/openai/v1`` surface with an Entra bearer token
instead of calling the Foundry project directly. The gateway routes on
``<provider>/<registration>`` aliases (``aisvc-zurlo-northwind/gpt-5-4``), not on
Foundry deployment names, so every outbound ``model`` goes through
:func:`gateway_alias`. UI-facing names stay the deployment names.

Everything is off by default: with no gateway settings the existing direct paths
run untouched. ``AI_GATEWAY_REQUIRED`` turns a missing or unusable gateway
configuration into an explicit :class:`GatewayConfigError` instead of a silent
direct call. Agent Service invocations (``orchestrator_client``), Voice Live and
Content Understanding are not routed here.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from .settings import AI_GATEWAY_DEFAULT_SCOPE as DEFAULT_SCOPE
from .settings import get_settings

FEATURES = ("safety", "bakeoff", "adjuster_support", "harness")
_ENDPOINT_SUFFIX = "/openai/v1"
_HOST = re.compile(r"[A-Za-z0-9._-]+")
_ALIAS = re.compile(r"[^\s/]+/[^\s/]+")


class GatewayConfigError(RuntimeError):
    """The AI Gateway is required or selected but its configuration is unusable."""


def _text(settings: Any, name: str) -> str:
    # getattr keeps callers that pass a partial settings object on the direct path.
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


def gateway_required(settings: Any) -> bool:
    return bool(getattr(settings, "ai_gateway_required", False))


def gateway_enabled(settings: Any) -> bool:
    """True when an endpoint is configured; validity is checked when it is used."""
    return bool(_text(settings, "ai_gateway_model_endpoint"))


def gateway_scope(settings: Any) -> str:
    return _text(settings, "ai_gateway_scope") or DEFAULT_SCOPE


def route_for(settings: Any) -> str:
    return "gateway" if gateway_enabled(settings) else "direct"


def validate_endpoint(endpoint: str) -> str:
    """Return the endpoint with exactly one trailing slash, or raise ``GatewayConfigError``."""
    value = (endpoint or "").strip()
    if not value:
        raise GatewayConfigError("AI_GATEWAY_MODEL_ENDPOINT is not configured.")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise GatewayConfigError("AI_GATEWAY_MODEL_ENDPOINT is not a valid URL.") from exc
    host = parts.hostname or ""
    if parts.scheme != "https" or not _HOST.fullmatch(host) or re.search(r"\s", value):
        raise GatewayConfigError("AI_GATEWAY_MODEL_ENDPOINT must be an absolute HTTPS URL.")
    if "@" in parts.netloc:
        raise GatewayConfigError("AI_GATEWAY_MODEL_ENDPOINT must not contain credentials.")
    if "?" in value or "#" in value:
        raise GatewayConfigError("AI_GATEWAY_MODEL_ENDPOINT must not contain a query string or fragment.")
    path = parts.path.rstrip("/")
    if not path.endswith(_ENDPOINT_SUFFIX):
        raise GatewayConfigError(
            "AI_GATEWAY_MODEL_ENDPOINT must end with /openai/v1, for example "
            "https://<gateway>/default/models/openai/v1/.")
    return f"{parts.scheme}://{parts.netloc}{path}/"


def sdk_base_url(settings: Any) -> str:
    """The endpoint without ``/openai/v1/``, for SDKs that append ``/openai/v1/<operation>`` themselves."""
    return validate_endpoint(_text(settings, "ai_gateway_model_endpoint"))[:-len(_ENDPOINT_SUFFIX) - 1]


@lru_cache(maxsize=16)
def _parse_aliases(raw: str) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    shape = "AI_GATEWAY_MODEL_ALIASES must be a JSON object mapping deployment names to gateway aliases."
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GatewayConfigError(shape) from exc
    if not isinstance(data, dict):
        raise GatewayConfigError(shape)
    entries = []
    for deployment, alias in data.items():
        if not deployment.strip() or not isinstance(alias, str) or not _ALIAS.fullmatch(alias.strip()):
            raise GatewayConfigError(
                "AI_GATEWAY_MODEL_ALIASES values must look like <provider>/<registration>.")
        entries.append((deployment.strip(), alias.strip()))
    return tuple(entries)


def _provider(settings: Any) -> str:
    provider = _text(settings, "ai_gateway_model_provider")
    if provider and not re.fullmatch(r"[^\s/]+", provider):
        raise GatewayConfigError("AI_GATEWAY_MODEL_PROVIDER must be a provider name without '/' or spaces.")
    return provider


def registration_name(deployment: str) -> str:
    """Registration name of a deployment: lowercased, every character outside [a-z0-9-] becomes '-'."""
    name = re.sub(r"[^a-z0-9-]", "-", (deployment or "").strip().lower())
    if not name.strip("-"):
        raise GatewayConfigError("A model deployment name is required to derive a gateway alias.")
    return name


def gateway_alias(settings: Any, deployment: str) -> str:
    """The ``model`` value the gateway routes on for a Foundry deployment name."""
    name = (deployment or "").strip()
    overrides = dict(_parse_aliases(_text(settings, "ai_gateway_model_aliases")))
    override = overrides.get(name) or next(
        (alias for key, alias in overrides.items() if key.casefold() == name.casefold()), None)
    if override:
        return override
    provider = _provider(settings)
    if not provider:
        raise GatewayConfigError(
            f"No gateway alias for deployment '{name}': set AI_GATEWAY_MODEL_PROVIDER "
            "or add the deployment to AI_GATEWAY_MODEL_ALIASES.")
    return f"{provider}/{registration_name(name)}"


def configuration_problem(settings: Any) -> str | None:
    """Why the gateway cannot serve model calls right now, or None when it can or is not selected."""
    required = gateway_required(settings)
    if not gateway_enabled(settings):
        return ("AI_GATEWAY_REQUIRED is set but AI_GATEWAY_MODEL_ENDPOINT is not configured, "
                "so direct Foundry model calls are blocked.") if required else None
    try:
        validate_endpoint(_text(settings, "ai_gateway_model_endpoint"))
        overrides = _parse_aliases(_text(settings, "ai_gateway_model_aliases"))
        if not _provider(settings) and not overrides:
            raise GatewayConfigError(
                "AI_GATEWAY_MODEL_PROVIDER or AI_GATEWAY_MODEL_ALIASES must be set to derive gateway model aliases.")
    except GatewayConfigError as exc:
        suffix = " AI_GATEWAY_REQUIRED is set, so direct Foundry model calls are blocked." if required else ""
        return f"{exc}{suffix}"
    return None


def require_gateway(settings: Any) -> None:
    """Fail closed: raise when the gateway is mandatory but not fully configured."""
    if gateway_required(settings):
        problem = configuration_problem(settings)
        if problem:
            raise GatewayConfigError(problem)


def configuration_key(settings: Any) -> tuple[str, ...]:
    """Everything that decides where model calls go and as whom, for cache and verification keys."""
    return tuple(_text(settings, name) for name in (
        "ai_gateway_model_endpoint", "ai_gateway_model_provider",
        "ai_gateway_model_aliases", "ai_gateway_scope"))


class GatewayTokenProvider:
    """Async bearer-token callable for ``AsyncOpenAI(api_key=...)``.

    Accepts a synchronous ``TokenCredential`` (run on a worker thread so a slow
    identity chain never blocks the event loop) or an ``azure.identity.aio``
    credential, and reuses the token until shortly before it expires.
    """

    def __init__(self, credential: Any, scope: str, *, refresh_margin: float = 300.0,
                 clock: Callable[[], float] = time.time) -> None:
        self._credential = credential
        self.scope = scope
        self._refresh_margin = refresh_margin
        self._clock = clock
        self._token = ""
        self._expires_on = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return bool(self._token) and self._clock() < self._expires_on - self._refresh_margin

    async def _acquire(self) -> Any:
        get_token = self._credential.get_token
        if inspect.iscoroutinefunction(get_token):
            access = await get_token(self.scope)
        else:
            access = await asyncio.to_thread(get_token, self.scope)
        return await access if inspect.isawaitable(access) else access

    async def __call__(self) -> str:
        if self._fresh():
            return self._token
        async with self._lock:
            if not self._fresh():
                access = await self._acquire()
                self._token = access.token
                # A token without an expiry is used once and never cached.
                self._expires_on = float(getattr(access, "expires_on", 0) or 0)
            return self._token


# One client per (endpoint, scope, credential, transport); the flag marks clients this
# module owns the HTTP transport of, which are the ones it closes at shutdown.
_clients: dict[tuple[str, str, int, int], tuple[AsyncOpenAI, bool]] = {}


def get_gateway_client(settings: Any = None, credential: Any = None, http_client: Any = None) -> AsyncOpenAI:
    """Cached ``AsyncOpenAI`` for the gateway, authenticated with an Entra bearer token (never a key)."""
    if settings is None:
        settings = get_settings()
    endpoint = validate_endpoint(_text(settings, "ai_gateway_model_endpoint"))
    scope = gateway_scope(settings)
    key = (endpoint, scope, id(credential), id(http_client))
    cached = _clients.get(key)
    if cached is None:
        if credential is None:
            from .azure_clients import get_credential
            credential = get_credential()
        client = AsyncOpenAI(
            base_url=endpoint, api_key=GatewayTokenProvider(credential, scope), http_client=http_client)
        cached = _clients[key] = (client, http_client is None)
    return cached[0]


def reset_gateway_clients() -> None:
    """Forget cached clients without closing them (tests)."""
    _clients.clear()


async def close_gateway_clients() -> None:
    cached = list(_clients.values())
    _clients.clear()
    for client, owned in cached:
        if owned:
            await client.close()


def span_attributes(settings: Any, deployment: str | None = None) -> dict[str, str]:
    """``gateway.route`` plus, when the gateway serves ``deployment``, its ``gateway.model_alias``."""
    attributes = {"gateway.route": route_for(settings)}
    if deployment and gateway_enabled(settings):
        with contextlib.suppress(GatewayConfigError):
            attributes["gateway.model_alias"] = gateway_alias(settings, deployment)
    return attributes


def stamp_span(span: Any, settings: Any, deployment: str | None = None) -> None:
    """Record the model route, and the gateway alias when one applies, on a span."""
    for name, value in span_attributes(settings, deployment).items():
        span.set_attribute(name, value)


def status(settings: Any, deployments: Iterable[str] = ()) -> dict[str, Any]:
    """Non-secret routing summary for ``GET /gateway/status``."""
    route = route_for(settings)
    try:
        host = urlsplit(_text(settings, "ai_gateway_model_endpoint")).hostname or ""
    except ValueError:
        host = ""
    names = (getattr(settings, "planner_deployment", ""), getattr(settings, "worker_deployment", ""),
             getattr(settings, "harness_model", ""), *deployments)
    aliases: dict[str, str | None] = {}
    if gateway_enabled(settings):
        for name in dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()):
            try:
                aliases[name] = gateway_alias(settings, name)
            except GatewayConfigError:
                aliases[name] = None
    return {
        "required": gateway_required(settings),
        "enabled": gateway_enabled(settings),
        "endpoint_host": host,
        "provider": _text(settings, "ai_gateway_model_provider"),
        "scope": gateway_scope(settings),
        "features": dict.fromkeys(FEATURES, route),
        "model_aliases": aliases,
        "problem": configuration_problem(settings),
    }
