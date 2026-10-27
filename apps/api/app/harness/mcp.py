"""MCP helpers shared by the harness tools: gateway key header and MCP result parsing."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from ..settings import Settings
from .models import HarnessError

GATEWAY_KEY_HEADER = "Ocp-Apim-Subscription-Key"


def gateway_headers(settings: Settings) -> dict[str, str]:
    """API gateway key for MCP calls; empty unless MCP_GATEWAY_SUBSCRIPTION_KEY is set."""
    key = settings.harness_gateway_key.get_secret_value().strip()
    if not key:
        return {}
    try:
        secure = urlsplit(settings.harness_claim_review_mcp).scheme == "https"
    except ValueError:
        secure = False
    if not secure:
        raise HarnessError("mcp_unconfigured", "MCP_CLAIM_REVIEW_URL must be HTTPS when MCP_GATEWAY_SUBSCRIPTION_KEY is set.")
    return {GATEWAY_KEY_HEADER: key}


def mcp_payload(raw: str) -> dict:
    value = json.loads(raw)
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        if value.get("isError"):
            raise HarnessError("draft_failed", "Claim-review MCP reported an error.")
        texts = [c["text"] for c in value["content"] if c.get("type") == "text"]
        if len(texts) != 1:
            raise HarnessError("draft_failed", "Claim-review MCP returned an ambiguous result.")
        value = json.loads(texts[0])
    if not isinstance(value, dict):
        raise HarnessError("draft_failed", "Claim-review MCP returned an invalid result.")
    return value
