"""Prompt agents routed through the AI Gateway model connection (offline; no Foundry calls)."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
CONNECTION = "my-conn"
PROVIDER = "aisvc-zurlo-northwind"
GATEWAY_VARS = (
    "AI_GATEWAY_REQUIRED", "AI_GATEWAY_MODEL_CONNECTION", "AI_GATEWAY_MODEL_PROVIDER",
    "AI_GATEWAY_MODEL_ALIASES", "AI_GATEWAY_MCP_REQUIRED", "AI_GATEWAY_MCP_KB_ROUTES",
)


def load(name, filename):
    # Deliberately not registered in sys.modules: this is how the other script tests load scripts.
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agents_script = load("create_prompt_agents_gateway", "create-prompt-agents.py")
connect_script = load("connect_model_gateway_for_agents", "connect-model-gateway.py")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in GATEWAY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINTS", "FOUNDRY_ENDPOINT",
                 "FOUNDRY_WORKER_DEPLOYMENT", "AZURE_SEARCH_INDEX",
                 "AZURE_SEARCH_KNOWLEDGE_BASE", "FOUNDRY_KB_CONNECTION_NAME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def prompt_environment(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROMPT_AGENT_DEPLOYMENT", "gpt-4o")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")


@pytest.mark.parametrize("deployment,expected", [
    ("gpt-4o", "my-conn/aisvc-zurlo-northwind/gpt-4o"),
    ("gpt-5.4", "my-conn/aisvc-zurlo-northwind/gpt-5-4"),
    ("gpt-5.4-mini", "my-conn/aisvc-zurlo-northwind/gpt-5-4-mini"),
    ("gpt-5.6-luna", "my-conn/aisvc-zurlo-northwind/gpt-5-6-luna"),
    ("Kimi-K2.6", "my-conn/aisvc-zurlo-northwind/kimi-k2-6"),
    (" gpt-4o ", "my-conn/aisvc-zurlo-northwind/gpt-4o"),
])
def test_model_string_is_connection_slash_provider_slash_registration(deployment, expected):
    assert agents_script.gateway_model_string(deployment, CONNECTION) == expected


def test_provider_and_alias_overrides():
    assert agents_script.gateway_model_string("gpt-4o", CONNECTION, "other-provider") == "my-conn/other-provider/gpt-4o"
    aliases = {"gpt-5.4": "aisvc-zurlo-northwind/gpt-5-4-custom"}
    assert agents_script.gateway_model_string("gpt-5.4", CONNECTION, PROVIDER, aliases) == (
        "my-conn/aisvc-zurlo-northwind/gpt-5-4-custom")
    # An override for one deployment does not change the derivation of the others.
    assert agents_script.gateway_model_string("gpt-4o", CONNECTION, PROVIDER, aliases) == (
        "my-conn/aisvc-zurlo-northwind/gpt-4o")


def test_connection_qualified_models_are_idempotent_but_other_connections_are_refused():
    qualified = "my-conn/aisvc-zurlo-northwind/gpt-4o"
    assert agents_script.gateway_model_string(qualified, CONNECTION) == qualified
    with pytest.raises(ValueError, match="different connection"):
        agents_script.gateway_model_string("elsewhere/gpt-4o", CONNECTION)


def test_an_unresolved_placeholder_is_not_turned_into_a_bogus_alias():
    with pytest.raises(ValueError, match="unresolved placeholder"):
        agents_script.gateway_model_string("${PROMPT_MODEL_FIXTURE}", CONNECTION)
    # Default behaviour is untouched: the model is passed through as before.
    assert agents_script.resolve_agent_model("${PROMPT_MODEL_FIXTURE}", None) == "${PROMPT_MODEL_FIXTURE}"


def test_without_a_connection_the_model_is_untouched():
    assert agents_script.resolve_agent_model("gpt-4o", None) == "gpt-4o"
    assert agents_script.resolve_agent_model("Kimi-K2.6", None) == "Kimi-K2.6"
    assert agents_script.gateway_models_from_env({}) is None
    # Provider and aliases alone never switch routing on.
    assert agents_script.gateway_models_from_env({
        "AI_GATEWAY_MODEL_PROVIDER": "p", "AI_GATEWAY_MODEL_ALIASES": '{"a": "p/a"}'}) is None


def test_settings_from_environment():
    settings = agents_script.gateway_models_from_env({
        "AI_GATEWAY_MODEL_CONNECTION": " my-conn ",
        "AI_GATEWAY_MODEL_PROVIDER": "other-provider",
        "AI_GATEWAY_MODEL_ALIASES": '{"gpt-4o": "other-provider/gpt-4o-v2"}',
    })
    assert settings.connection == "my-conn" and settings.provider == "other-provider"
    assert agents_script.resolve_agent_model("gpt-4o", settings) == "my-conn/other-provider/gpt-4o-v2"
    default = agents_script.gateway_models_from_env({
        "AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_PROVIDER": "  "})
    assert default.provider == PROVIDER and dict(default.aliases) == {}


@pytest.mark.parametrize("flag", ["true", "1", "yes", " TRUE ", "Yes"])
def test_required_without_a_connection_leaves_prompt_agent_models_direct(flag):
    # Prompt-agent models cannot be routed through the AI Gateway tier (docs/ai-gateway.md finding 9), so a flag
    # that applies to the hosted agents and the API must not make a model connection mandatory here.
    assert agents_script.gateway_models_from_env({"AI_GATEWAY_REQUIRED": flag}) is None


@pytest.mark.parametrize("flag", ["", "false", "0", "no", "off", " OFF "])
def test_a_disabled_flag_changes_nothing(flag):
    assert agents_script.gateway_models_from_env({"AI_GATEWAY_REQUIRED": flag}) is None
    assert agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_REQUIRED": flag}) is None
    settings = agents_script.gateway_models_from_env({
        "AI_GATEWAY_REQUIRED": flag, "AI_GATEWAY_MODEL_CONNECTION": CONNECTION})
    assert settings.connection == CONNECTION


def test_a_flag_typo_does_not_silently_disable_the_control():
    with pytest.raises(ValueError, match="must be true, 1 or yes"):
        agents_script.gateway_models_from_env({"AI_GATEWAY_REQUIRED": "ture"})


@pytest.mark.parametrize("environment", [
    {"AI_GATEWAY_MODEL_CONNECTION": "ab"},
    {"AI_GATEWAY_MODEL_CONNECTION": "x" * 34},
    {"AI_GATEWAY_MODEL_CONNECTION": "has/slash"},
    {"AI_GATEWAY_MODEL_CONNECTION": "-leading"},
    {"AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_PROVIDER": "a/b"},
    {"AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_ALIASES": "not json"},
    {"AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_ALIASES": "[]"},
    {"AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_ALIASES": '{"gpt-4o": 4}'},
    {"AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_ALIASES": '{"gpt-4o": "bare-name"}'},
    {"AI_GATEWAY_MODEL_CONNECTION": "my-conn", "AI_GATEWAY_MODEL_ALIASES": '{"": "p/a"}'},
])
def test_malformed_settings_are_rejected(environment):
    with pytest.raises(ValueError):
        agents_script.gateway_models_from_env(environment)


def test_plan_translates_the_default_and_the_yaml_override_but_not_the_tools():
    function_tool = {"type": "function", "name": "lookup", "description": "look",
                     "parameters": {"type": "object", "properties": {}}}
    cfg = {"name": "agent-one", "instructions": "Answer.", "tools": [function_tool]}
    gateway = agents_script.GatewayModels(CONNECTION, PROVIDER, {})
    model, tools = agents_script._plan_agent(cfg, "gpt-4o", gateway=gateway)
    assert model == "my-conn/aisvc-zurlo-northwind/gpt-4o"
    assert agents_script._tool_summary(tools) == "function:lookup"
    override, override_tools = agents_script._plan_agent({**cfg, "model": "Kimi-K2.6"}, "gpt-4o", gateway=gateway)
    assert override == "my-conn/aisvc-zurlo-northwind/kimi-k2-6" and override_tools == tools
    # Default behaviour: nothing is translated.
    assert agents_script._plan_agent(cfg, "gpt-4o")[0] == "gpt-4o"
    assert agents_script._plan_agent({**cfg, "model": "Kimi-K2.6"}, "gpt-4o")[0] == "Kimi-K2.6"


def test_yaml_model_placeholders_are_expanded_before_translation(monkeypatch):
    monkeypatch.setenv("PROMPT_MODEL_FIXTURE", "gpt-5.4-mini")
    gateway = agents_script.GatewayModels(CONNECTION, PROVIDER, {})
    cfg = {"name": "agent-one", "instructions": "Answer.", "model": "${PROMPT_MODEL_FIXTURE}"}
    assert agents_script._plan_agent(cfg, "gpt-4o", gateway=gateway)[0] == "my-conn/aisvc-zurlo-northwind/gpt-5-4-mini"


class RecordingAgents:
    def __init__(self, sink, endpoint):
        self.sink, self.endpoint = sink, endpoint

    async def get(self, name):
        raise LookupError(name)

    async def create_version(self, *, agent_name, definition, description):
        self.sink[agent_name] = (self.endpoint, definition.model)


def test_upsert_hands_the_translated_model_to_the_prompt_agent_definition(capsys):
    sink = {}
    cfg = {"name": "agent-one", "instructions": "Answer.", "description": "d", "model": "Kimi-K2.6"}
    gateway = agents_script.GatewayModels(CONNECTION, PROVIDER, {})
    asyncio.run(agents_script._upsert_agent(RecordingAgents(sink, "e1"), cfg, "gpt-4o", gateway=gateway))
    assert sink["agent-one"] == ("e1", "my-conn/aisvc-zurlo-northwind/kimi-k2-6")
    assert "(model=my-conn/aisvc-zurlo-northwind/kimi-k2-6)" in capsys.readouterr().out
    asyncio.run(agents_script._upsert_agent(RecordingAgents(sink, "e1"), cfg, "gpt-4o"))
    assert sink["agent-one"] == ("e1", "Kimi-K2.6")


class Offline:
    """Fails the test if any Foundry client or credential is created."""

    def __init__(self, *args, **kwargs):
        pytest.fail("A dry run or a rejected configuration must not create Foundry clients")


def forbid_foundry(monkeypatch):
    monkeypatch.setattr(agents_script, "AIProjectClient", Offline)
    monkeypatch.setattr(agents_script, "_get_credential", Offline)


# --- a hermetic company: department prompts change with the departments' content --------------------------

CLAIMS_ROUTES = json.dumps({"nw-claims-kb": {"toolserver": "nw-claims-knowledge", "connection": "nw-claims-kb-gw"}})
CLAIMS_TOOLSERVER = "https://ai-gateway-northwind.azure-api.net/default/toolservers/nw-claims-knowledge/mcp"
SEARCH_AGENT = "northwind-claims-evidence-search"
ADJUSTER_AGENT = "northwind-insurance-adjuster-support"
TRIAGE_AGENT = "northwind-claims-triage"
CLASSIFIER_AGENT = "northwind-intent-classifier"


def write_prompt(directory, name, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    document = {"name": name, "description": name + " test prompt", "instructions": "Answer.", **extra}
    (directory / (name + ".yaml")).write_text(yaml.safe_dump(document), encoding="utf-8")


def department(slug, prompts_dir, search_index):
    return agents_script.Department(
        slug=slug, name=slug, tagline="", pillar="", icon="", brand_color="", foundry_project="project-" + slug,
        search_index=search_index, cu_inbox_prefix=slug, enabled_tabs=(), seed_dir=prompts_dir,
        golden_path=prompts_dir, prompts_dir=prompts_dir, cu_analyzer_dir=None, customer_archetype="")


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    indexes = {"claims-concierge": "northwind-insurance-knowledge", "adjuster-support": "nw-insurance-adjuster-support",
               "claims-documents": "nw-claims", "contact-center": "nw-contact-center"}
    prompts = {slug: tmp_path / slug for slug in indexes}
    write_prompt(prompts["claims-concierge"], "northwind-hosted-flagship-stray")
    write_prompt(prompts["adjuster-support"], ADJUSTER_AGENT)
    write_prompt(prompts["claims-documents"], TRIAGE_AGENT)
    write_prompt(prompts["claims-documents"], SEARCH_AGENT, model="gpt-5.4",
                 tools=[{"type": "knowledge", "index": "nw-claims"}])
    write_prompt(prompts["contact-center"], CLASSIFIER_AGENT)
    company = SimpleNamespace(
        name="Northwind Insurance",
        departments=tuple(department(slug, path, indexes[slug]) for slug, path in prompts.items()))
    monkeypatch.setattr(agents_script, "load_company", lambda: company)
    return company


def test_dry_run_prints_every_planned_agent_with_its_gateway_model(monkeypatch, capsys, prompt_environment, manifest):
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Gateway model connection: my-conn" in out
    for line in (
        SEARCH_AGENT + " (model=my-conn/aisvc-zurlo-northwind/gpt-5-4; tools=mcp:nw-claims-kb@direct)",
        TRIAGE_AGENT + " (model=my-conn/aisvc-zurlo-northwind/gpt-4o; tools=none)",
        ADJUSTER_AGENT + " (model=my-conn/aisvc-zurlo-northwind/gpt-4o; tools=none)",
        CLASSIFIER_AGENT + " (model=my-conn/aisvc-zurlo-northwind/gpt-4o; tools=none)",
    ):
        assert line in out
    assert "northwind-hosted-flagship-stray" not in out
    assert "nothing was sent to Foundry" in out


def test_dry_run_without_a_gateway_shows_the_direct_models(monkeypatch, capsys, prompt_environment, manifest):
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "Gateway model connection" not in out
    assert SEARCH_AGENT + " (model=gpt-5.4;" in out
    assert TRIAGE_AGENT + " (model=gpt-4o; tools=none)" in out


def test_dry_run_reports_a_planning_error_and_exits_non_zero(monkeypatch, capsys, prompt_environment, manifest):
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
    forbid_foundry(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(agents_script.main(dry_run=True))
    assert raised.value.code == 1
    assert "AZURE_SEARCH_ENDPOINT" in capsys.readouterr().out


def test_required_without_a_connection_still_plans_direct_prompt_agents(monkeypatch, capsys, prompt_environment,
                                                                        manifest):
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", "true")
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "Gateway model connection" not in out
    assert TRIAGE_AGENT + " (model=gpt-4o; tools=none)" in out


def test_a_configured_model_connection_warns_that_foundry_rejects_the_alias(monkeypatch, capsys, prompt_environment,
                                                                            manifest):
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    err = capsys.readouterr().err
    assert "Foundry rejects a model id that contains '/'" in err and "finding 9" in err


def test_without_a_model_connection_nothing_is_warned(monkeypatch, capsys, prompt_environment, manifest):
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    assert "finding 9" not in capsys.readouterr().err


def recording_project(sink):
    class Project:
        def __init__(self, *, endpoint, credential):
            self.agents = RecordingAgents(sink, endpoint)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return Project


DEPARTMENT_ENDPOINTS = {
    "claims-concierge": "https://e.example/cc1", "adjuster-support": "https://e.example/as",
    "claims-documents": "https://e.example/cd", "contact-center": "https://e.example/cc",
}


@pytest.fixture
def recorded_run(monkeypatch, prompt_environment):
    created = {}
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps(DEPARTMENT_ENDPOINTS))
    monkeypatch.setattr(agents_script, "AIProjectClient", recording_project(created))
    monkeypatch.setattr(agents_script, "_get_credential", lambda: object())
    return created


def test_default_run_still_creates_agents_with_the_direct_models(manifest, recorded_run, capsys):
    asyncio.run(agents_script.main())
    assert recorded_run == {
        ADJUSTER_AGENT: ("https://e.example/as", "gpt-4o"),
        TRIAGE_AGENT: ("https://e.example/cd", "gpt-4o"),
        SEARCH_AGENT: ("https://e.example/cd", "gpt-5.4"),
        CLASSIFIER_AGENT: ("https://e.example/cc", "gpt-4o"),
    }
    assert "All 4 prompt agents ready" in capsys.readouterr().out


def test_gateway_run_creates_the_same_agents_with_connection_qualified_models(monkeypatch, manifest, recorded_run,
                                                                              capsys):
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", "true")
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    monkeypatch.setenv("AI_GATEWAY_MODEL_ALIASES", '{"gpt-5.4": "aisvc-zurlo-northwind/gpt-5-4"}')
    asyncio.run(agents_script.main())
    assert len(recorded_run) == 4
    assert recorded_run[SEARCH_AGENT] == ("https://e.example/cd", "my-conn/aisvc-zurlo-northwind/gpt-5-4")
    assert recorded_run[TRIAGE_AGENT] == ("https://e.example/cd", "my-conn/aisvc-zurlo-northwind/gpt-4o")
    assert all(model.startswith("my-conn/") for _, model in recorded_run.values())
    assert "All 4 prompt agents ready" in capsys.readouterr().out


def test_hosted_flagship_agents_are_never_created_as_prompt_agents(monkeypatch, manifest, recorded_run, capsys):
    monkeypatch.setenv("HOSTED_AGENTS_ENABLED", "false")
    asyncio.run(agents_script.main())
    assert "northwind-hosted-flagship-stray" not in recorded_run
    assert "Hosted agents are deployed by scripts/deploy-hosted-agents.py" in capsys.readouterr().out


def test_deprecated_department_slugs_in_the_endpoint_map_still_resolve(monkeypatch, manifest, recorded_run):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps({
        "order-concierge": "https://e.example/cc1", "employee-help-desk": "https://e.example/as",
        "claims-warranty": "https://e.example/cd", "contact-center": "https://e.example/cc",
    }))
    asyncio.run(agents_script.main())
    assert recorded_run[ADJUSTER_AGENT][0] == "https://e.example/as"
    assert recorded_run[TRIAGE_AGENT][0] == "https://e.example/cd"
    assert recorded_run[CLASSIFIER_AGENT][0] == "https://e.example/cc"


@pytest.mark.parametrize("raw", [
    {"claims-warranty": "https://old", "claims-documents": "https://new"},
    {"claims-documents": "https://new", "claims-warranty": "https://old"},
])
def test_a_canonical_slug_wins_over_its_deprecated_alias(monkeypatch, raw):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps(raw))
    assert agents_script._resolve_endpoints() == {"claims-documents": "https://new"}


def shipped_agent_names():
    names = set()
    for dept in agents_script.load_company().departments:
        if dept.slug in agents_script._HOSTED_DEPARTMENTS:
            continue
        names.update(yaml.safe_load(path.read_text(encoding="utf-8"))["name"] for path in dept.prompts_dir.glob("*.yaml"))
    return names


def test_every_shipped_department_prompt_is_created(recorded_run, capsys):
    asyncio.run(agents_script.main())
    assert recorded_run and set(recorded_run) == shipped_agent_names()
    assert {TRIAGE_AGENT, CLASSIFIER_AGENT} <= set(recorded_run)
    assert f"All {len(recorded_run)} prompt agents ready" in capsys.readouterr().out


def test_every_shipped_department_prompt_plans_through_the_gateway(monkeypatch, capsys, prompt_environment):
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert f"Dry run complete: {len(shipped_agent_names())} prompt agent(s) planned" in out


def test_connection_models_and_agent_models_agree():
    """Every model the connection registers is exactly what a prompt agent is told to use."""
    entries = connect_script.model_entries(connect_script.DEFAULT_DEPLOYMENTS)
    for deployment, entry in zip(connect_script.DEFAULT_DEPLOYMENTS, entries, strict=True):
        assert f"{CONNECTION}/{entry['name']}" == agents_script.gateway_model_string(deployment, CONNECTION)


@pytest.mark.parametrize("raw", [
    "", '{"gpt-4o": "p/gpt-4o-x"}', "not json", "[]", '{"gpt-4o": 4}', '{"gpt-4o": "bare"}', '{"": "p/a"}',
])
def test_alias_maps_are_parsed_identically_by_the_agent_and_connection_scripts(raw):
    try:
        expected = agents_script._parse_aliases(raw)
    except ValueError:
        with pytest.raises(ValueError):
            connect_script.parse_aliases(raw)
    else:
        assert connect_script.parse_aliases(raw) == expected


# --- knowledge-base MCP tools through the gateway tool servers -----------------------------------------

KB = "northwind-insurance-kb"
KB_CONNECTION = "northwind-insurance-kb-mcp"
KB_TOOLSERVER = "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-knowledge/mcp"
KB_ROUTES = json.dumps({KB: {"toolserver": "northwind-insurance-knowledge", "connection": "nw-insurance-kb-gw"}})


def kb_route(prefix="knowledge_"):
    return agents_script.KbRoute(KB_TOOLSERVER, "nw-insurance-kb-gw", prefix)


def test_kb_routes_parse_with_the_default_tool_prefix():
    routes = agents_script._parse_kb_routes(KB_ROUTES)
    assert routes == {KB: kb_route()}
    custom = agents_script._parse_kb_routes(json.dumps({" " + KB + " ": {
        "toolserver": "northwind-insurance-knowledge", "connection": " nw-insurance-kb-gw ", "tool_prefix": "kb_"}}))
    assert custom == {KB: kb_route("kb_")}
    assert agents_script._parse_kb_routes("") == {} and agents_script._parse_kb_routes("  ") == {}
    assert agents_script._parse_kb_routes("{}") == {}


@pytest.mark.parametrize("raw", [
    "not json", "[]", '"x"', '{"northwind-insurance-kb": "northwind-insurance-knowledge"}',
    '{"": {"toolserver": "a-b", "connection": "abc"}}',
    '{"k": {"toolserver": "northwind-insurance-knowledge"}}',
    '{"k": {"connection": "abc"}}',
    '{"k": {"toolserver": 5, "connection": "abc"}}',
    '{"k": {"toolserver": "Bad Name", "connection": "abc"}}',
    '{"k": {"toolserver": "a/b", "connection": "abc"}}',
    '{"k": {"toolserver": "ok", "connection": "ab"}}',
    '{"k": {"toolserver": "ok", "connection": "has/slash"}}',
    '{"k": {"toolserver": "ok", "connection": "abc", "tool_prefix": "Bad-Prefix"}}',
    '{"k": {"toolserver": "ok", "connection": "abc", "tool_prefix": 3}}',
    '{"k": {"toolserver": "ok", "connection": "abc", "url": "https://x"}}',
])
def test_malformed_kb_routes_are_rejected(raw):
    with pytest.raises(ValueError, match="AI_GATEWAY_MCP_KB_ROUTES"):
        agents_script._parse_kb_routes(raw)


def test_mcp_settings_from_environment():
    assert agents_script.gateway_mcp_from_env({}) is None
    assert agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_REQUIRED": "false", "AI_GATEWAY_MCP_KB_ROUTES": ""}) is None
    routed = agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_KB_ROUTES": KB_ROUTES})
    assert routed.required is False and routed.routes == {KB: kb_route()}
    required = agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_REQUIRED": "true"})
    assert required.required is True and dict(required.routes) == {}
    with pytest.raises(ValueError, match="must be true, 1 or yes"):
        agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_REQUIRED": "ture"})
    with pytest.raises(ValueError, match="AI_GATEWAY_MCP_KB_ROUTES"):
        agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_KB_ROUTES": "nope"})


def test_a_routed_kb_uses_its_tool_server_without_needing_the_search_endpoint(monkeypatch):
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT", raising=False)
    mcp = agents_script.GatewayMcp(False, {KB: kb_route()})
    assert agents_script._kb_mcp_entry("agent", KB, KB_CONNECTION, mcp) == {
        "type": "mcp", "server_label": KB, "server_url": KB_TOOLSERVER, "require_approval": "never",
        "allowed_tools": ["knowledge_knowledge_base_retrieve"], "project_connection_id": "nw-insurance-kb-gw",
    }
    custom = agents_script.GatewayMcp(False, {KB: kb_route("kb_")})
    assert agents_script._kb_mcp_entry("agent", KB, "x", custom)["allowed_tools"] == [
        "kb_knowledge_base_retrieve"]


def test_an_unrouted_kb_keeps_its_direct_search_endpoint(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net/")
    direct = {
        "type": "mcp", "server_label": KB, "require_approval": "never",
        "server_url": f"https://search.example.net/knowledgebases/{KB}/mcp?api-version=2026-05-01-preview",
        "allowed_tools": ["knowledge_base_retrieve"], "project_connection_id": KB_CONNECTION,
    }
    assert agents_script._kb_mcp_entry("agent", KB, KB_CONNECTION, None) == direct
    other = agents_script.GatewayMcp(False, {"another-kb": kb_route()})
    assert agents_script._kb_mcp_entry("agent", KB, KB_CONNECTION, other) == direct
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
    with pytest.raises(RuntimeError, match="AZURE_SEARCH_ENDPOINT"):
        agents_script._kb_mcp_entry("agent", KB, KB_CONNECTION, None)


def test_required_mcp_refuses_a_kb_without_a_route(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")
    mcp = agents_script.GatewayMcp(True, {"another-kb": kb_route()})
    with pytest.raises(ValueError, match=rf"AI_GATEWAY_MCP_REQUIRED.*no route for knowledge base '{KB}'.*'agent'"):
        agents_script._kb_mcp_entry("agent", KB, KB_CONNECTION, mcp)
    assert agents_script._kb_mcp_entry("agent", "another-kb", "x", mcp)["server_url"] == KB_TOOLSERVER


def test_both_knowledge_block_styles_are_routed(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")
    monkeypatch.setenv("AZURE_SEARCH_KNOWLEDGE_BASE", KB)
    mcp = agents_script.GatewayMcp(True, {
        "nw-claims-kb": agents_script.KbRoute(CLAIMS_TOOLSERVER, "nw-claims-kb-gw", "knowledge_"),
        KB: agents_script.KbRoute(KB_TOOLSERVER, "northwind-insurance-kb-gw", "knowledge_")})
    tool_block = {"name": "claims", "instructions": "x", "tools": [{"type": "knowledge", "index": "nw-claims"}]}
    [entry] = agents_script._knowledge_tools_for_agent(tool_block, mcp=mcp)
    assert entry["server_url"] == CLAIMS_TOOLSERVER and entry["project_connection_id"] == "nw-claims-kb-gw"
    legacy_block = {"name": "insurance", "instructions": "x", "knowledge": [
        {"kind": "foundry_iq", "knowledge_base": KB, "connection": KB_CONNECTION}]}
    [entry] = agents_script._knowledge_tools_for_agent(legacy_block, mcp=mcp)
    assert entry["server_url"] == KB_TOOLSERVER
    assert entry["project_connection_id"] == "northwind-insurance-kb-gw"
    # Without routing the same blocks still go straight to Search, exactly as before.
    [direct] = agents_script._knowledge_tools_for_agent(tool_block)
    assert direct["server_url"].startswith("https://search.example.net/knowledgebases/nw-claims-kb/mcp")


def test_unresolved_knowledge_placeholders_default_to_the_isolated_insurance_knowledge_base(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")
    block = {"name": "agent", "instructions": "x", "knowledge": [{
        "kind": "foundry_iq", "knowledge_base": "${AZURE_SEARCH_KNOWLEDGE_BASE}",
        "connection": "${FOUNDRY_KB_CONNECTION_NAME}"}]}
    [entry] = agents_script._knowledge_tools_for_agent(block)
    assert entry["server_url"].startswith(f"https://search.example.net/knowledgebases/{KB}/mcp")
    assert entry["project_connection_id"] == KB_CONNECTION
    monkeypatch.setenv("AZURE_SEARCH_KNOWLEDGE_BASE", "override-kb")
    monkeypatch.setenv("FOUNDRY_KB_CONNECTION_NAME", "override-connection")
    [entry] = agents_script._knowledge_tools_for_agent(block)
    assert "/knowledgebases/override-kb/mcp" in entry["server_url"]
    assert entry["project_connection_id"] == "override-connection"


def test_the_flagship_index_keeps_its_published_knowledge_base_and_connection(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")
    block = {"name": "agent", "instructions": "x", "tools": [
        {"type": "knowledge", "index": "northwind-insurance-knowledge"}]}
    [entry] = agents_script._knowledge_tools_for_agent(block)
    assert f"/knowledgebases/{KB}/mcp" in entry["server_url"]
    assert entry["project_connection_id"] == KB_CONNECTION


def test_required_mcp_also_covers_yaml_declared_mcp_tools():
    gateway_tool = {"type": "mcp", "server_label": "lookup", "server_url": KB_TOOLSERVER}
    direct_tool = {"type": "mcp", "server_label": "lookup", "server_url": "https://ca-x.example.io/mcp"}
    cfg = {"name": "agent-one", "instructions": "x"}
    mcp = agents_script.GatewayMcp(True, {})
    [resolved] = agents_script._tools_for_agent({**cfg, "tools": [gateway_tool]}, mcp=mcp)
    assert resolved["server_url"] == KB_TOOLSERVER
    with pytest.raises(ValueError, match=r"AI_GATEWAY_MCP_REQUIRED.*'agent-one'.*directly"):
        agents_script._tools_for_agent({**cfg, "tools": [direct_tool]}, mcp=mcp)
    # Not required (or no MCP settings at all): a direct MCP server is left alone.
    for relaxed in (None, agents_script.GatewayMcp(False, {})):
        [kept] = agents_script._tools_for_agent({**cfg, "tools": [direct_tool]}, mcp=relaxed)
        assert kept["server_url"] == "https://ca-x.example.io/mcp/"


def test_tool_summary_marks_where_each_mcp_tool_goes():
    tools = [
        {"type": "mcp", "server_label": KB, "server_url": KB_TOOLSERVER},
        {"type": "mcp", "server_label": "x", "server_url": "https://search.example.net/knowledgebases/x/mcp"},
        {"type": "mcp", "server_label": "api", "server_url": "https://apim-northwind.azure-api.net/insurance/mcp/work-order"},
        {"type": "function", "name": "lookup"},
    ]
    assert agents_script._tool_summary(tools) == (
        f"mcp:{KB}@ai-gateway, mcp:x@direct, mcp:api@apim, function:lookup")
    assert agents_script._tool_summary([]) == "none"


def test_the_apim_connections_are_never_attached_to_an_ai_gateway_tool_server(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", "apim-policy-lookup")
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_CLAIM_REVIEW", "apim-claim-review")
    apim_url = "https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup"
    cfg = {"name": "agent-one", "instructions": "x", "tools": [
        {"type": "mcp", "server_label": "apim", "server_url": apim_url},
        {"type": "mcp", "server_label": "ai", "server_url": KB_TOOLSERVER},
        {"type": "mcp", "server_label": "own", "server_url": apim_url, "project_connection_id": "explicit-connection"},
    ]}
    apim, ai, own = agents_script._tools_for_agent(cfg)
    assert apim["server_url"] == apim_url and apim["project_connection_id"] == "apim-policy-lookup"
    # The AI Gateway authenticates with its own key, never with an API Management subscription key.
    assert ai["server_url"] == KB_TOOLSERVER and "project_connection_id" not in ai
    assert own["project_connection_id"] == "explicit-connection"


def test_dry_run_shows_the_routed_knowledge_base_and_needs_no_search_endpoint(monkeypatch, capsys, prompt_environment,
                                                                              manifest):
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
    monkeypatch.setenv("AI_GATEWAY_MCP_KB_ROUTES", CLAIMS_ROUTES)
    monkeypatch.setenv("AI_GATEWAY_MCP_REQUIRED", "true")
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "Gateway MCP: 1 knowledge-base route(s); knowledge bases without a route are an error" in out
    assert SEARCH_AGENT + " (model=gpt-5.4; tools=mcp:nw-claims-kb@ai-gateway)" in out
    assert "@direct" not in out and "nothing was sent to Foundry" in out


@pytest.mark.parametrize("dry_run", [False, True])
def test_required_mcp_without_a_route_fails_before_anything_is_created(monkeypatch, capsys, manifest, recorded_run,
                                                                       dry_run):
    monkeypatch.setenv("AI_GATEWAY_MCP_REQUIRED", "true")
    with pytest.raises(SystemExit) as raised:
        asyncio.run(agents_script.main(dry_run=dry_run))
    assert raised.value.code == 1
    assert "no route for knowledge base 'nw-claims-kb'" in capsys.readouterr().out
    # The agents without a knowledge base are not the problem; the one that needs a route never reaches Foundry.
    assert SEARCH_AGENT not in recorded_run


def test_malformed_mcp_settings_exit_before_any_agent_is_created(monkeypatch, capsys, prompt_environment):
    monkeypatch.setenv("AI_GATEWAY_MCP_KB_ROUTES", "nope")
    forbid_foundry(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(agents_script.main(dry_run=True))
    assert raised.value.code == 1
    assert "AI_GATEWAY_MCP_KB_ROUTES" in capsys.readouterr().err


class RecordingToolAgents(RecordingAgents):
    def __init__(self, sink, endpoint):
        super().__init__(sink, endpoint)

    async def create_version(self, *, agent_name, definition, description):
        self.sink[agent_name] = (self.endpoint, definition.model, definition.tools)


def test_routed_run_hands_the_gateway_tool_to_the_prompt_agent_definition(monkeypatch, prompt_environment, manifest):
    created = {}

    class Project:
        def __init__(self, *, endpoint, credential):
            self.agents = RecordingToolAgents(created, endpoint)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps(DEPARTMENT_ENDPOINTS))
    monkeypatch.setattr(agents_script, "AIProjectClient", Project)
    monkeypatch.setattr(agents_script, "_get_credential", lambda: object())
    monkeypatch.setenv("AI_GATEWAY_MCP_KB_ROUTES", CLAIMS_ROUTES)
    asyncio.run(agents_script.main())
    [tool] = created[SEARCH_AGENT][2]
    assert tool["server_url"] == CLAIMS_TOOLSERVER
    assert tool["project_connection_id"] == "nw-claims-kb-gw"
    assert tool["allowed_tools"] == ["knowledge_knowledge_base_retrieve"]
    assert created[SEARCH_AGENT][1] == "gpt-5.4"
