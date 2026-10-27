"""Identity-authenticated Responses clients for embedded assistants, with no gateway fallback."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openai import AsyncOpenAI

from . import model_gateway
from .orchestrator_client import _async_credential as _async_credential
from .orchestrator_client import _extract_output_text as _extract_output_text
from .orchestrator_client import _project_client
from .settings import Settings


@asynccontextmanager
async def model_client(settings: Settings, deployment: str) -> AsyncIterator[tuple[AsyncOpenAI, str]]:
    """Keep deployment names in the UI, but route outbound model names through the configured gateway."""
    problem = model_gateway.configuration_problem(settings)
    if problem:
        raise model_gateway.GatewayConfigError(problem)
    if model_gateway.gateway_enabled(settings):
        alias = model_gateway.gateway_alias(settings, deployment)
        # The application lifespan, not an individual assistant turn, owns this shared client.
        yield model_gateway.get_gateway_client(settings), alias
    else:
        async with _project_client() as project:
            async with project.get_openai_client() as client:
                yield client, deployment
