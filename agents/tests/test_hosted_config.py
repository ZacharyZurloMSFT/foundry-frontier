"""Deployment configuration contracts, runnable without installing the host."""
import pytest

from agents.hosted.config import (
    AGENTS_DIR, ENV_KEYS, PROMPTS, ROLE_ALIASES, SETTING_ALIASES, Settings, read_environment,
)

ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://existing.services.ai.azure.com/api/projects/nw-insurance-claims",
    "MODEL_DEPLOYMENT_NAME": "existing-baseline",
    "AGENT_ROLE": "policy-wording",
    "AGENT_KB_CONNECTION": "nw-insurance-claims-kb-mcp",
    "AZURE_SEARCH_ENDPOINT": "https://existing.search.windows.net",
    "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
    "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
    "MCP_POLICY_LOOKUP_URL": "https://lookup.example/mcp",
    "MCP_CLAIM_REVIEW_URL": "https://review.example/mcp",
}
VERIFIED = {"NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "true"}


def test_knowledge_uses_project_connection_without_search_credentials():
    spec = Settings.from_env(ENV).knowledge_spec()
    assert spec["project_connection_id"] == "nw-insurance-claims-kb-mcp"
    assert spec["allowed_tools"] == ["knowledge_base_retrieve"]
    assert spec["url"] == ("https://existing.search.windows.net/knowledgebases/"
                           "northwind-insurance-kb/mcp?api-version=2026-05-01-preview")
    assert "headers" not in spec


@pytest.mark.parametrize("connection", ["nw-insurance-claims-kb-mcp", "northwind-insurance-kb-mcp"])
def test_both_insurance_knowledge_connection_names_are_accepted(connection):
    spec = Settings.from_env({**ENV, "AGENT_KB_CONNECTION": connection}).knowledge_spec()
    assert spec["project_connection_id"] == connection


def test_another_departments_knowledge_connection_is_not_accepted():
    with pytest.raises(ValueError, match="other knowledge cannot be substituted"):
        Settings.from_env({**ENV, "AGENT_KB_CONNECTION": "nw-claims-kb-mcp"}).knowledge_spec()


@pytest.mark.parametrize("role", list(PROMPTS))
def test_source_deployment_model_env_supported_by_every_role(role):
    config = Settings.from_env({**ENV, "AGENT_ROLE": role})
    assert config.role == role
    assert config.model == "existing-baseline"
    assert config.model_route == "baseline"


def test_promoted_triage_override_wins_without_changing_other_agents():
    env = {**ENV, "TRIAGE_MODEL_DEPLOYMENT": "northwind-insurance-triage-ft-v1", **VERIFIED}
    triage = Settings.from_env({**env, "AGENT_ROLE": "triage"})
    assert triage.model == "northwind-insurance-triage-ft-v1"
    assert triage.model_route == "triage_override"
    assert Settings.from_env(env).model == "existing-baseline"


@pytest.mark.parametrize("env", [
    {"TRIAGE_MODEL_DEPLOYMENT": "northwind-insurance-triage-ft-v1"},
    {"TRIAGE_MODEL_DEPLOYMENT": "other-triage-ft-v1", **VERIFIED},
    {"TRIAGE_MODEL_DEPLOYMENT": "northwind-insurance-triage-ft-v1", "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "yes"},
    {"MODEL_DEPLOYMENT_NAME": "baseline-ft-v1", **VERIFIED},
])
def test_an_override_or_fine_tuned_model_needs_a_verified_insurance_model(env):
    with pytest.raises(ValueError, match="not insurance-ready"):
        Settings.from_env({**ENV, "AGENT_ROLE": "triage", **env})


def test_unrelated_environment_secrets_are_not_copied_into_configuration():
    config = Settings.from_env({**ENV, "NOT_A_RUNTIME_SETTING": "test-only-sentinel"})
    assert "NOT_A_RUNTIME_SETTING" not in config.env


GATEWAY_SETTINGS = ("AI_GATEWAY_REQUIRED", "AI_GATEWAY_MCP_REQUIRED", "AGENT_MCP_GATEWAY")


def test_gateway_enforcement_settings_are_recognised_runtime_settings():
    assert set(GATEWAY_SETTINGS) <= set(ENV_KEYS)
    config = Settings.from_env({**ENV, "AI_GATEWAY_MCP_REQUIRED": "false", "AGENT_MCP_GATEWAY": ""})
    assert config.env["AI_GATEWAY_MCP_REQUIRED"] == "false"


@pytest.mark.parametrize("role", list(PROMPTS))
def test_undefined_azd_values_arrive_empty_and_change_nothing(role):
    env = {**ENV, "AGENT_ROLE": role}
    baseline = Settings.from_env(env)
    unset = Settings.from_env({**env, **dict.fromkeys(GATEWAY_SETTINGS, "")})
    assert unset.metadata == baseline.metadata
    assert not unset.model_gateway_required and unset.mcp_gateway == {}
    assert unset.mcp_route == "direct"


def test_the_removed_domain_flag_is_ignored_and_never_read():
    assert "NORTHWIND_BUSINESS_DOMAIN" not in ENV_KEYS
    config = Settings.from_env({**ENV, "NORTHWIND_BUSINESS_DOMAIN": "other"})
    assert "NORTHWIND_BUSINESS_DOMAIN" not in config.env
    assert config.metadata["business_domain"] == "insurance"
    assert not hasattr(config, "business_domain")


def test_only_the_insurance_prompts_exist():
    assert set(PROMPTS.values()) == {
        "insurance/orchestrator.yaml", "insurance/triage.yaml", "insurance/policy_claim_lookup.yaml",
        "insurance/policy_wording.yaml", "insurance/claim_action_draft.yaml",
    }
    assert sorted(path.name for path in AGENTS_DIR.glob("*.yaml")) == []


# Deprecated names: the pre-cleanup role ids and settings keep working, canonical names win.
WITHOUT_ROLE = {key: value for key, value in ENV.items() if key != "AGENT_ROLE"}


def test_deprecated_role_ids_select_the_canonical_role_and_prompt():
    assert ROLE_ALIASES == {"manager": "orchestrator", "orderlookup": "policy-claim-lookup",
                            "policylookup": "policy-wording", "resolutiondraft": "claim-action-draft"}
    for legacy, canonical in ROLE_ALIASES.items():
        new = Settings.from_env({**WITHOUT_ROLE, "AGENT_ROLE": canonical})
        for variable in ("AGENT_ROLE", "ORDER_AGENT_ROLE"):
            old = Settings.from_env({**WITHOUT_ROLE, variable: legacy})
            assert old.role == canonical and old.prompt_sha256 == new.prompt_sha256
            assert old.metadata == new.metadata and old.metadata["agent_role"] == canonical


def test_the_canonical_role_setting_wins_and_the_default_is_the_orchestrator():
    assert Settings.from_env({**WITHOUT_ROLE, "AGENT_ROLE": "triage", "ORDER_AGENT_ROLE": "orderlookup"}).role == "triage"
    blank = Settings.from_env({**WITHOUT_ROLE, "AGENT_ROLE": "", "ORDER_AGENT_ROLE": "orderlookup"})
    assert blank.role == "policy-claim-lookup"
    assert Settings.from_env(WITHOUT_ROLE).role == "orchestrator"
    with pytest.raises(ValueError, match="Unknown AGENT_ROLE: unknown"):
        Settings.from_env({**WITHOUT_ROLE, "AGENT_ROLE": "unknown"})
    with pytest.raises(ValueError, match="Unknown AGENT_ROLE"):
        Settings.from_env({**WITHOUT_ROLE, "AGENT_ROLE": ""})
    with pytest.raises(ValueError, match="Unknown AGENT_ROLE: unknown"):
        Settings.from_env({**WITHOUT_ROLE, "ORDER_AGENT_ROLE": "unknown"})


@pytest.mark.parametrize("canonical,legacy", [(new, old) for new, olds in SETTING_ALIASES.items() for old in olds])
def test_every_deprecated_setting_name_fills_only_an_unset_canonical_one(canonical, legacy):
    assert canonical.startswith(("AGENT_", "MCP_")) and not legacy.startswith("AGENT_")
    assert {canonical, legacy} <= set(ENV_KEYS)
    assert read_environment({legacy: "old"}) == {canonical: "old"}
    assert read_environment({canonical: "new", legacy: "old"}) == {canonical: "new"}
    assert read_environment({canonical: "", legacy: "old"}) == {canonical: "old"}
    assert read_environment({canonical: "   ", legacy: "old"}) == {canonical: "old"}
    assert read_environment({canonical: "new"}) == {canonical: "new"}
    assert read_environment({canonical: "   "}) == {canonical: "   "}


def test_a_complete_pre_cleanup_environment_configures_exactly_what_the_canonical_one_does():
    legacy = {
        "FOUNDRY_PROJECT_ENDPOINT": ENV["FOUNDRY_PROJECT_ENDPOINT"], "ORDERS_MODEL_DEPLOYMENT": "baseline",
        "ORDERS_OPENAI_ENDPOINT": "https://account.openai.azure.com/openai/v1/",
        "ORDERS_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/default/models/openai/v1/",
        "ORDERS_MODEL_GATEWAY_MODEL": "northwind/baseline", "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "baseline",
        "ORDERS_MODEL_GATEWAY_SCOPE": "api://gateway/.default",
        "ORDERS_MCP_GATEWAY": '{"orderlookup": {"url": "https://gateway.example/default/toolservers/'
                              'northwind-insurance-lookup/mcp", "tool_prefix": "lookup_", "connection": "c"}}',
        "ORDERS_KB_CONNECTION": "northwind-insurance-kb-mcp", "ORDERS_KB_CONNECTION_NAME": "unused",
        "MCP_TECH_LOOKUP_URL": "https://lookup.example/mcp", "MCP_WORK_ORDER_URL": "https://review.example/mcp",
        "AZURE_SEARCH_ENDPOINT": ENV["AZURE_SEARCH_ENDPOINT"], "AZURE_SEARCH_INDEX": ENV["AZURE_SEARCH_INDEX"],
        "AZURE_SEARCH_KNOWLEDGE_BASE": ENV["AZURE_SEARCH_KNOWLEDGE_BASE"],
    }
    canonical = {
        "FOUNDRY_PROJECT_ENDPOINT": ENV["FOUNDRY_PROJECT_ENDPOINT"], "AGENT_MODEL_DEPLOYMENT": "baseline",
        "AGENT_OPENAI_ENDPOINT": "https://account.openai.azure.com/openai/v1/",
        "AGENT_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/default/models/openai/v1/",
        "AGENT_MODEL_GATEWAY_MODEL": "northwind/baseline", "AGENT_MODEL_GATEWAY_DEPLOYMENT": "baseline",
        "AGENT_MODEL_GATEWAY_SCOPE": "api://gateway/.default",
        "AGENT_MCP_GATEWAY": '{"policy-claim-lookup": {"url": "https://gateway.example/default/toolservers/'
                             'northwind-insurance-lookup/mcp", "tool_prefix": "lookup_", "connection": "c"}}',
        "AGENT_KB_CONNECTION": "northwind-insurance-kb-mcp", "AGENT_KB_CONNECTION_NAME": "unused",
        "MCP_POLICY_LOOKUP_URL": "https://lookup.example/mcp", "MCP_CLAIM_REVIEW_URL": "https://review.example/mcp",
        "AZURE_SEARCH_ENDPOINT": ENV["AZURE_SEARCH_ENDPOINT"], "AZURE_SEARCH_INDEX": ENV["AZURE_SEARCH_INDEX"],
        "AZURE_SEARCH_KNOWLEDGE_BASE": ENV["AZURE_SEARCH_KNOWLEDGE_BASE"],
    }
    assert read_environment(legacy).keys() == read_environment(canonical).keys()
    # The role and the gateway JSON legitimately differ in spelling; everything else is identical.
    spelling = {"AGENT_ROLE", "AGENT_MCP_GATEWAY"}
    for role, legacy_role in (("policy-claim-lookup", "orderlookup"), ("policy-wording", "policylookup"),
                              ("claim-action-draft", "resolutiondraft"), ("triage", "triage"),
                              ("orchestrator", "manager")):
        old = Settings.from_env({**legacy, "ORDER_AGENT_ROLE": legacy_role})
        new = Settings.from_env({**canonical, "AGENT_ROLE": role})
        assert {k: v for k, v in old.env.items() if k not in spelling} == {
            k: v for k, v in new.env.items() if k not in spelling}
        assert old.role == new.role == role and old.metadata == new.metadata
        assert old.gateway_scope == new.gateway_scope == "api://gateway/.default"
        assert old.mcp_route == new.mcp_route and old.mcp_gateway == new.mcp_gateway
    old = Settings.from_env({**legacy, "ORDER_AGENT_ROLE": "orderlookup"})
    assert old.mcp_spec() == Settings.from_env({**canonical, "AGENT_ROLE": "policy-claim-lookup"}).mcp_spec()
    assert old.mcp_spec()["allowed_tools"] == ["lookup_lookup_policy", "lookup_lookup_claim"]
    policy = Settings.from_env({**legacy, "ORDER_AGENT_ROLE": "policylookup"})
    assert policy.knowledge_spec()["project_connection_id"] == "northwind-insurance-kb-mcp"


@pytest.mark.parametrize("role,legacy_url,canonical_url", [
    ("policy-claim-lookup", "MCP_TECH_LOOKUP_URL", "MCP_POLICY_LOOKUP_URL"),
    ("claim-action-draft", "MCP_WORK_ORDER_URL", "MCP_CLAIM_REVIEW_URL"),
])
def test_prompt_placeholders_resolve_the_canonical_url_first_then_the_deprecated_one(role, legacy_url, canonical_url):
    env = {key: value for key, value in ENV.items() if not key.startswith("MCP_")}
    prompt_url = Settings.from_env({**env, "AGENT_ROLE": role, canonical_url: "https://new.example/mcp"})
    assert prompt_url.mcp_spec()["server_url"] == "https://new.example/mcp"
    both = Settings.from_env({**env, "AGENT_ROLE": role, canonical_url: "https://new.example/mcp",
                              legacy_url: "https://old.example/mcp"})
    assert both.mcp_spec()["server_url"] == "https://new.example/mcp"
    only_legacy = Settings.from_env({**env, "AGENT_ROLE": role, legacy_url: "https://old.example/mcp"})
    assert only_legacy.mcp_spec()["server_url"] == "https://old.example/mcp"
    with pytest.raises(ValueError, match=f"Missing deployment configuration: {canonical_url}"):
        Settings.from_env({**env, "AGENT_ROLE": role}).mcp_spec()
    # Prompts may name the canonical placeholder too; renaming the placeholder changes nothing.
    assert only_legacy.expand("${" + canonical_url + "}") == "https://old.example/mcp"


@pytest.mark.parametrize("env,expected", [
    ({"AGENT_KB_CONNECTION": "a"}, "a"),
    ({"AGENT_KB_CONNECTION_NAME": "b"}, "b"),
    ({"ORDERS_KB_CONNECTION": "c"}, "c"),
    ({"ORDERS_KB_CONNECTION_NAME": "d"}, "d"),
    ({"AGENT_KB_CONNECTION": "a", "AGENT_KB_CONNECTION_NAME": "b", "ORDERS_KB_CONNECTION": "c",
      "ORDERS_KB_CONNECTION_NAME": "d"}, "a"),
    ({"AGENT_KB_CONNECTION_NAME": "b", "ORDERS_KB_CONNECTION": "c", "ORDERS_KB_CONNECTION_NAME": "d"}, "c"),
    ({"AGENT_KB_CONNECTION_NAME": "b", "ORDERS_KB_CONNECTION_NAME": "d"}, "b"),
])
def test_knowledge_connection_placeholder_prefers_the_canonical_names(env, expected):
    base = {key: value for key, value in ENV.items() if key != "AGENT_KB_CONNECTION"}
    assert Settings.from_env({**base, **env}).expand("${FOUNDRY_KB_CONNECTION_NAME}") == expected


def test_an_arm_style_connection_id_is_accepted_when_it_ends_in_the_insurance_connection_name():
    arm = "/subscriptions/s/resourceGroups/rg/providers/x/accounts/a/projects/p/connections/northwind-insurance-kb-mcp"
    spec = Settings.from_env({**ENV, "AGENT_KB_CONNECTION": arm}).knowledge_spec()
    assert spec["project_connection_id"] == arm
    with pytest.raises(ValueError, match="cannot be substituted"):
        Settings.from_env({**ENV, "AGENT_KB_CONNECTION": arm + "-other"}).knowledge_spec()
