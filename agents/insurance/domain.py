"""Local import shim; packaging embeds the canonical shared implementation."""
from apps.api.app.insurance_domain import ClaimActionDraft, action_payload

__all__ = ["ClaimActionDraft", "action_payload"]
