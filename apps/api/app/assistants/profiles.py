"""Surface registry. A surface binds roles, tools, prompt and observability stamp.

Callers never configure scope: roles and tools are fixed here, and a surface is reachable only when
the operator lists it in NORTHWIND_ASSISTANT_SURFACES.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]

from .. import model_gateway
from ..settings import get_settings

Kind = Literal["scoped_tools", "extractive_help"]


@dataclass(frozen=True)
class AssistantProfile:
    surface: str
    kind: Kind
    label: str
    agent: str
    roles: frozenset[str]
    department: str
    prompt_file: str
    tools: tuple[str, ...]
    accepts: tuple[Literal["claim", "policy"], ...]
    suggestions: tuple[str, ...]


PROFILES: dict[str, AssistantProfile] = {
    "concierge": AssistantProfile(
        surface="concierge", kind="scoped_tools", label="Claims Concierge",
        agent="northwind-insurance-concierge", roles=frozenset({"policyholder"}),
        department="claims-concierge", prompt_file="concierge.yaml",
        tools=("list_my_policies", "get_policy", "list_my_claims", "get_claim"),
        accepts=("claim", "policy"),
        suggestions=(
            "Where do my claims stand?",
            "What evidence is still needed on my claim?",
            "What does my collision coverage say?",
        ),
    ),
    "help_center": AssistantProfile(
        surface="help_center", kind="extractive_help", label="Help Center",
        agent="northwind-insurance-help-center", roles=frozenset({"policyholder"}),
        department="claims-concierge", prompt_file="help-center.yaml", tools=(), accepts=(),
        suggestions=(
            "How do I report a loss?",
            "What happens after I report a loss?",
            "What evidence should I send?",
            "How do deductibles work?",
        ),
    ),
}
PROMPT_DIRECTORY = Path("agents") / "insurance" / "assistants"
HELP_INDEX = "nw-insurance-help-customer-v1"


def enabled_surfaces() -> frozenset[str]:
    settings = get_settings()
    if not settings.assistants_enabled:
        return frozenset()
    requested = {name.strip() for name in settings.assistant_surfaces.split(",") if name.strip()}
    return frozenset(name for name in requested if name in PROFILES)


def model_name() -> str:
    settings = get_settings()
    return settings.assistant_model or settings.planner_deployment


def load_prompt(profile: AssistantProfile) -> tuple[str, str]:
    """Return (instructions, sha256) from the canonical YAML, or its packaged copy in the image."""
    here = Path(__file__).resolve()
    relative = PROMPT_DIRECTORY / profile.prompt_file
    canonical = next((p / relative for p in here.parents if (p / relative).is_file()), None)
    path = canonical or here.parents[1] / "_agent_prompts" / profile.prompt_file
    text = path.read_text(encoding="utf-8")
    definition = yaml.safe_load(text)
    if (not isinstance(definition, dict) or definition.get("surface") != profile.surface
            or definition.get("business_domain") != "insurance"
            or not isinstance(definition.get("instructions"), str) or not definition["instructions"].strip()):
        raise ValueError("Assistant prompt must declare its surface, insurance domain and instructions.")
    if profile.kind == "extractive_help" and definition.get("index") != HELP_INDEX:
        raise ValueError("A help prompt must name the isolated customer help index.")
    return definition["instructions"], hashlib.sha256(text.encode()).hexdigest()


def _https(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
            and not parts.query and not parts.fragment and not parts.path.rstrip("/"))


def unavailable_reason(profile: AssistantProfile) -> str | None:
    """Why an enabled surface cannot serve right now; configuration only, never proof of readiness."""
    settings = get_settings()
    reasons: list[str] = []
    gateway_problem = model_gateway.configuration_problem(settings)
    if gateway_problem:
        reasons.append("The AI Gateway configuration is unavailable; direct fallback is not allowed.")
    elif model_gateway.gateway_enabled(settings):
        try:
            model_gateway.gateway_alias(settings, model_name())
        except model_gateway.GatewayConfigError:
            reasons.append("The assistant model must have an AI Gateway alias; direct fallback is not allowed.")
    elif not settings.foundry_project_endpoint:
        reasons.append("A Foundry project endpoint is required.")
    if not settings.cosmos_endpoint:
        reasons.append("Conversation storage is required.")
    try:
        load_prompt(profile)
    except (OSError, ValueError, yaml.YAMLError):
        reasons.append("The canonical assistant prompt was not packaged.")
    if profile.kind == "extractive_help":
        endpoint = settings.help_center_search_endpoint or settings.search_endpoint
        if not _https(endpoint):
            reasons.append("An HTTPS Azure Search endpoint is required.")
        if settings.help_center_index != HELP_INDEX:
            reasons.append("The isolated customer help index must be configured.")
        if settings.help_center_search_auth == "query_key" and not settings.help_center_query_key.get_secret_value():
            reasons.append("Explicit Search authentication must be configured; no key fallback is performed.")
    return "; ".join(reasons) or None
