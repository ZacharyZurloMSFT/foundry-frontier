"""Local import shim; packaging embeds the canonical shared contract."""
from apps.api.app.insurance_config import (
    ALIASES,
    INSURANCE_CONNECTION,
    INSURANCE_INDEX,
    INSURANCE_KB,
    INSURANCE_PROJECT,
    ROLE_DIRECTORIES,
    configuration_hash,
    normalize_environment,
    normalize_role,
    valid_project_endpoint,
    validate_insurance_connection,
)

__all__ = [
    "ALIASES", "INSURANCE_CONNECTION", "INSURANCE_INDEX", "INSURANCE_KB",
    "INSURANCE_PROJECT", "ROLE_DIRECTORIES", "configuration_hash",
    "normalize_environment", "normalize_role", "valid_project_endpoint",
    "validate_insurance_connection",
]
