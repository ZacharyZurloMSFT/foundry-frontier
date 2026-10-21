"""Runtime configuration — all values come from environment variables or the
repo-root `.env` file (populated by `scripts/fetch-keys.{ps1,sh}` after a
`azd provision`). Container Apps ignore the `.env` file because it's not
shipped in the image; they read the same names from the environment injected
by the containerapps.bicep module.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .insurance_config import ALIASES, INSURANCE_PROJECT, normalize_environment

# Search repo root and cwd for a .env; harmless if neither exists.
# In the container the file lives at /app/app/settings.py (only 2 parents),
# on the host it's <repo>/apps/api/app/settings.py (4 parents). Walk up as far
# as we can and try each candidate; env_file entries that don't exist are ignored.
_HERE = Path(__file__).resolve()
_ENV_FILES = tuple(str(p / ".env") for p in _HERE.parents) + (".env",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        def normalized(source):
            def read():
                values = dict(source())
                raw = getattr(source, "env_vars", {})
                selected = {}
                for primary in ("INSURANCE_PROJECT_NAME", "INSURANCE_PROJECT_ENDPOINT",
                                "AGENTS_HOSTED", "MCP_INSURANCE_REVIEW_URL"):
                    for name in (primary, *ALIASES[primary]):
                        if name.casefold() in raw and raw[name.casefold()] is not None:
                            values[name] = raw[name.casefold()]
                        if name in values:
                            value = values.pop(name)
                            if not isinstance(value, (str, bool)):
                                raise ValueError(f"{name} must be a string or boolean")
                            selected[name] = str(value).lower() if isinstance(value, bool) else value
                return {**values, **normalize_environment(selected)}
            return read
        return tuple(normalized(source) for source in (
            init_settings, env_settings, dotenv_settings, file_secret_settings,
        ))

    @model_validator(mode="after")
    def insurance_project_default(self):
        if self.business_domain == "insurance" and "order_project_name" not in self.model_fields_set:
            self.order_project_name = INSURANCE_PROJECT
        return self

    # Identity
    azure_client_id: str = Field(default="", alias="AZURE_CLIENT_ID")
    business_api_enabled: bool = Field(default=False, alias="NORTHWIND_BUSINESS_API_ENABLED")
    business_auth_tenant_id: str = Field(default="", alias="INSURANCE_AUTH_TENANT_ID")
    business_auth_audience: str = Field(default="", alias="INSURANCE_AUTH_AUDIENCE")
    business_auth_scope: str = Field(default="access_as_user", alias="INSURANCE_AUTH_REQUIRED_SCOPE")
    business_auth_client_ids: str = Field(default="", alias="INSURANCE_AUTH_CLIENT_IDS")

    # Foundry
    foundry_endpoint: str = Field(default="", alias="FOUNDRY_ENDPOINT")
    foundry_project_endpoint: str = Field(default="", alias="INSURANCE_PROJECT_ENDPOINT")
    foundry_project_endpoints: str = Field(default="", alias="FOUNDRY_PROJECT_ENDPOINTS")
    order_project_name: str = Field(default="northwind-orders", alias="INSURANCE_PROJECT_NAME")
    order_agents_hosted: bool = Field(default=False, alias="AGENTS_HOSTED")
    business_domain: Literal["retail", "insurance"] = Field(default="retail", alias="NORTHWIND_BUSINESS_DOMAIN")
    insurance_agents_ready: bool = Field(default=False, alias="NORTHWIND_INSURANCE_AGENTS_READY")
    insurance_orchestrator_agent_id: str = Field(default="", alias="FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID")
    planner_deployment: str = Field(default="gpt-5.4", alias="FOUNDRY_PLANNER_DEPLOYMENT")
    worker_deployment: str = Field(default="gpt-5.4-mini", alias="FOUNDRY_WORKER_DEPLOYMENT")
    # Hosted orchestrator agent id. In Foundry the id equals the agent name;
    # set this to override the default of "northwind-orchestrator".
    orchestrator_agent_id: str = Field(default="", alias="FOUNDRY_ORCHESTRATOR_AGENT_ID")
    # Prompt agent for policy/KB lookup ("Policy Lookup" tab / policy_lookup).
    # Env var is legacy — the agent is northwind-policy-lookup.
    manual_agent_id: str = Field(default="northwind-policy-lookup", alias="FOUNDRY_MANUAL_AGENT_ID")
    # Prompt agent that looks up customer + order + shipment ("Order Lookup"
    # tab). Env var is legacy — the agent is northwind-order-lookup.
    tech_agent_id: str = Field(default="northwind-order-lookup", alias="FOUNDRY_TECH_AGENT_ID")
    # Prompt agent that drafts structured customer resolutions ("Resolution
    # Draft" tab). Env var is legacy — the agent is northwind-resolution-draft.
    work_order_agent_id: str = Field(default="northwind-resolution-draft", alias="FOUNDRY_WORK_ORDER_AGENT_ID")

    # Search
    search_endpoint: str = Field(default="", alias="AZURE_SEARCH_ENDPOINT")
    search_index: str = Field(default="northwind-knowledge", alias="AZURE_SEARCH_INDEX")
    search_knowledge_base: str = Field(default="northwind-kb", alias="AZURE_SEARCH_KNOWLEDGE_BASE")
    search_knowledge_source: str = Field(default="northwind-knowledge-ks", alias="AZURE_SEARCH_KNOWLEDGE_SOURCE")

    # Cosmos
    cosmos_endpoint: str = Field(default="", alias="COSMOS_ENDPOINT")
    cosmos_database: str = Field(default="northwind", alias="COSMOS_DATABASE")

    # Storage
    storage_account: str = Field(default="", alias="STORAGE_ACCOUNT")

    # ── API keys (populated by scripts/fetch-keys.{ps1,sh} → .env) ──
    # When set, apps prefer key auth over managed identity. Corp subs that
    # enforce disableLocalAuth via policy can leave these empty and MI is used.
    foundry_api_key: str = Field(default="", alias="FOUNDRY_API_KEY")
    cosmos_key: str = Field(default="", alias="COSMOS_KEY")
    storage_key: str = Field(default="", alias="STORAGE_KEY")
    storage_connection_string: str = Field(default="", alias="STORAGE_CONNECTION_STRING")
    azure_search_key: str = Field(default="", alias="AZURE_SEARCH_KEY")

    adjuster_support_enabled: bool = Field(default=False, alias="ADJUSTER_SUPPORT_ENABLED")
    adjuster_support_index: str = Field(default="nw-insurance-adjuster-support", alias="ADJUSTER_SUPPORT_INDEX")
    adjuster_support_search_endpoint: str = Field(default="", alias="ADJUSTER_SUPPORT_SEARCH_ENDPOINT")
    adjuster_support_search_auth: Literal["entra_id", "query_key"] = Field(default="entra_id", alias="ADJUSTER_SUPPORT_SEARCH_AUTH")
    adjuster_support_query_key: SecretStr = Field(default=SecretStr(""), alias="ADJUSTER_SUPPORT_SEARCH_QUERY_KEY")
    adjuster_support_timeout: int = Field(default=60, ge=1, le=180, alias="ADJUSTER_SUPPORT_TIMEOUT_SECONDS")
    adjuster_support_concurrency: int = Field(default=4, ge=1, le=16, alias="ADJUSTER_SUPPORT_MAX_CONCURRENT_REQUESTS")

    # Observability
    appinsights_connection_string: str = Field(default="", alias="APPLICATIONINSIGHTS_CONNECTION_STRING")

    cu_enabled: bool = Field(default=False, alias="NORTHWIND_CU_ENABLED")
    cu_endpoint: str = Field(default="", alias="NORTHWIND_CU_ENDPOINT")
    cu_analyzer_id: str = Field(default="northwindInsuranceClaimsV1", alias="NORTHWIND_CU_ANALYZER_ID")
    cu_completion_model: Literal["gpt-4.1", "gpt-4o"] = Field(default="gpt-4.1", alias="NORTHWIND_CU_COMPLETION_MODEL")
    cu_completion_deployment: str = Field(default="", alias="NORTHWIND_CU_COMPLETION_DEPLOYMENT")
    cu_embedding_deployment: str = Field(default="", alias="NORTHWIND_CU_EMBEDDING_DEPLOYMENT")
    cu_timeout_seconds: int = Field(default=90, ge=1, le=180, alias="NORTHWIND_CU_TIMEOUT_SECONDS")
    cu_max_upload_bytes: int = Field(default=10485760, ge=1, le=10485760, alias="NORTHWIND_CU_MAX_UPLOAD_BYTES")

    evidence_enabled: bool = Field(default=False, alias="NORTHWIND_EVIDENCE_ENABLED")
    evidence_uploads_enabled: bool = Field(default=False, alias="NORTHWIND_EVIDENCE_UPLOADS_ENABLED")
    evidence_extraction_enabled: bool = Field(default=False, alias="NORTHWIND_EVIDENCE_EXTRACTION_ENABLED")
    evidence_worker_enabled: bool = Field(default=False, alias="NORTHWIND_EVIDENCE_WORKER_ENABLED")
    evidence_worker_run_ids: str = Field(default="", alias="NORTHWIND_EVIDENCE_WORKER_RUN_IDS")
    evidence_worker_lease_seconds: int = Field(default=60, ge=30, le=300, alias="NORTHWIND_EVIDENCE_WORKER_LEASE_SECONDS")
    evidence_worker_batch_size: int = Field(default=5, ge=1, le=20, alias="NORTHWIND_EVIDENCE_WORKER_BATCH_SIZE")
    evidence_worker_max_attempts: int = Field(default=3, ge=1, le=5, alias="NORTHWIND_EVIDENCE_WORKER_MAX_ATTEMPTS")

    voice_live_enabled: bool = Field(default=False, alias="VOICE_LIVE_ENABLED")
    voice_live_ready: bool = Field(default=False, alias="VOICE_LIVE_READY")
    voice_live_model: str = Field(default="", alias="VOICE_LIVE_MODEL")
    voice_live_api_version: str = Field(default="", alias="VOICE_LIVE_API_VERSION")
    voice_live_transcription_enabled: bool = Field(default=True, alias="VOICE_LIVE_TRANSCRIPTION_ENABLED")
    voice_live_allowed_origins: str = Field(
        default="https://ca-web-voice.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io,https://ca-web-tech.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io,http://localhost:5173,http://localhost:5174",
        alias="VOICE_LIVE_ALLOWED_ORIGINS",
    )

    port: int = Field(default=8000, alias="PORT")

    harness_enabled: bool = Field(default=False, alias="COPILOT_HARNESS_ENABLED")
    harness_model: str = Field(default="", alias="COPILOT_HARNESS_MODEL")
    harness_prompt_path: str = Field(default="", alias="COPILOT_HARNESS_PROMPT_PATH")
    harness_runtime_dir: str = Field(default="", alias="COPILOT_HARNESS_RUNTIME_DIR")
    harness_resolution_mcp: str = Field(default="", alias="MCP_INSURANCE_REVIEW_URL")
    harness_timeout: int = Field(default=180, ge=10, le=900, alias="COPILOT_HARNESS_TIMEOUT_SECONDS")
    harness_max_tools: int = Field(default=8, ge=1, le=16, alias="COPILOT_HARNESS_MAX_TOOL_CALLS")
    harness_concurrency: int = Field(default=2, ge=1, le=8, alias="COPILOT_HARNESS_MAX_CONCURRENT_RUNS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
