"""Local import shim; packaging embeds the canonical shared implementation."""
from apps.api.app.workflow_binding import sign_workflow, signing_key

__all__ = ["sign_workflow", "signing_key"]
