"""OpenTelemetry + Azure Monitor wiring. Called once at app startup.

Every span produced by :func:`get_tracer` is stamped at creation time with
the current build's ``git_sha`` and the manifest of prompt versions currently
in use — that's what makes the Foundry Observability → GitHub PR pivot in
`docs/genaiops.md` work (see B6). The manifest is loaded once at process
start from ``PROMPT_MANIFEST_PATH`` (a JSON blob written by the prompt-registry
workflow), so a bad trace can be traced back to the exact commit that
shipped every one of its agent prompts.

**Per-project stamping** — when the app processes a request bound to a
department (see `apps/api/app/company.py`), it records the resolved
``department`` slug and Foundry ``foundry_project`` name via
:func:`set_request_department`. Both attributes are then attached to
every span created for the remainder of the request. That's how the
Foundry Observability blade slices traces by project without any
per-callsite instrumentation.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

from .settings import get_settings

logger = logging.getLogger(__name__)
_configured = False


# Per-request context — set at the boundary (an ASGI middleware or the
# top of a handler); every span picks it up automatically.
_request_department: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "request_department", default=None,
)


def set_request_department(department_slug: str, foundry_project: str) -> None:
    """Record the active department for the current request. Every span
    created afterwards within this asyncio task will carry the slug +
    project as attributes."""
    _request_department.set({
        "department": department_slug,
        "foundry_project": foundry_project,
    })


def _current_dept_stamp() -> dict[str, str]:
    return _request_department.get() or {}


_NOISY_LOGGERS = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.monitor.opentelemetry.exporter.export._base",
    "azure.monitor.opentelemetry.exporter.export.trace._exporter",
    "azure.monitor.opentelemetry.exporter.export.logs._exporter",
    "azure.monitor.opentelemetry.exporter.statsbeat._statsbeat",
    "azure.identity",
    "urllib3.connectionpool",
)


def _quiet_noisy_loggers() -> None:
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


@lru_cache
def build_info() -> dict[str, str]:
    """Read git SHA + prompt-version manifest once and cache. All three env
    vars are set by the deploy workflow (see .github/workflows/prompt-registry.yml
    and deploy-progressive.yml). Falls back to `"local"` for dev."""
    info: dict[str, str] = {
        "git_sha":       os.environ.get("GIT_SHA", "local"),
        "build_env":     os.environ.get("BUILD_ENV", "local"),
        "deployed_at":   os.environ.get("DEPLOYED_AT", ""),
        "repo":          os.environ.get("GITHUB_REPOSITORY", ""),
    }
    packaged = Path(__file__).resolve().parent / "_agent_prompts" / "build.json"
    if packaged.is_file():
        # The image's source stamp, unlike runtime environment variables, follows
        # the bytes promoted from staging to production.
        info.update(json.loads(packaged.read_text(encoding="utf-8")))
    manifest_path = os.environ.get("PROMPT_MANIFEST_PATH", "")
    if manifest_path and Path(manifest_path).exists():
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            for k, v in manifest.items():
                info[f"prompt_sha.{k}"] = str(v)
        except (json.JSONDecodeError, OSError):
            pass
    return info


def configure_observability() -> None:
    global _configured
    if _configured:
        return
    _quiet_noisy_loggers()
    s = get_settings()
    if not s.appinsights_connection_string:
        logger.warning("APPLICATIONINSIGHTS_CONNECTION_STRING not set — OTel export disabled.")
        _configured = True
        return
    configure_azure_monitor(
        connection_string=s.appinsights_connection_string,
        instrumentation_options={
            "azure_sdk": {"enabled": True},
            "fastapi":   {"enabled": True},
            "requests":  {"enabled": True},
            "urllib3":   {"enabled": True},
        },
    )
    _quiet_noisy_loggers()
    _configured = True
    logger.info("OpenTelemetry configured; exporting to App Insights. build=%s",
                build_info().get("git_sha", "unknown"))


class _BuildInfoTracer:
    """Wraps a real Tracer and auto-stamps every span with build_info().

    Rationale: every trace exported to App Insights should carry the git SHA
    and per-agent prompt SHAs so the Foundry Observability dashboard can
    pivot from a failing trace back to the PR that shipped every prompt
    involved in the turn. Doing it at tracer level (not on every callsite)
    means new spans get the stamp for free.
    """
    def __init__(self, inner):
        self._inner = inner
        self._stamp = build_info()

    def start_as_current_span(self, name: str, *args, **kwargs):
        attrs = dict(kwargs.pop("attributes", None) or {})
        for k, v in self._stamp.items():
            attrs.setdefault(k, v)
        for k, v in _current_dept_stamp().items():
            attrs.setdefault(k, v)
        return self._inner.start_as_current_span(name, *args, attributes=attrs, **kwargs)

    def start_span(self, name: str, *args, **kwargs):
        attrs = dict(kwargs.pop("attributes", None) or {})
        for k, v in self._stamp.items():
            attrs.setdefault(k, v)
        for k, v in _current_dept_stamp().items():
            attrs.setdefault(k, v)
        return self._inner.start_span(name, *args, attributes=attrs, **kwargs)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def get_tracer(name: str = "northwind-copilot"):
    return _BuildInfoTracer(trace.get_tracer(name))
