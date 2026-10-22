"""Prompt agents routed through the AI Gateway model connection (offline; no Foundry calls)."""
import asyncio
import hashlib
import importlib.util
import json
import sys
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
setup_script = load("setup_orders_foundry_demo_gateway", "setup-orders-foundry-demo.py")
connect_script = load("connect_model_gateway_for_agents", "connect-model-gateway.py")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in GATEWAY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINTS", "FOUNDRY_ENDPOINT",
                 "FOUNDRY_WORKER_DEPLOYMENT", "ORDER_AGENTS_HOSTED", "AZURE_SEARCH_INDEX",
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
def test_required_without_a_connection_fails_closed(flag):
    with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED.*AI_GATEWAY_MODEL_CONNECTION"):
        agents_script.gateway_models_from_env({"AI_GATEWAY_REQUIRED": flag})


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


def test_dry_run_prints_every_planned_agent_with_its_gateway_model(monkeypatch, capsys, prompt_environment):
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Gateway model connection: my-conn" in out
    for line in (
        "northwind-hr-assistant-kimi (model=my-conn/aisvc-zurlo-northwind/kimi-k2-6; tools=mcp:nw-hr-kb@direct)",
        ("northwind-hr-assistant-gpt-5-4-mini (model=my-conn/aisvc-zurlo-northwind/gpt-5-4-mini; "
         "tools=mcp:nw-hr-kb@direct)"),
        "northwind-hr-assistant (model=my-conn/aisvc-zurlo-northwind/gpt-4o; tools=mcp:nw-hr-kb@direct)",
        "northwind-claims-triage (model=my-conn/aisvc-zurlo-northwind/gpt-4o; tools=none)",
        "northwind-intent-classifier (model=my-conn/aisvc-zurlo-northwind/gpt-4o; tools=none)",
    ):
        assert line in out
    assert "nothing was sent to Foundry" in out


def test_dry_run_without_a_gateway_shows_the_direct_models(monkeypatch, capsys, prompt_environment):
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "Gateway model connection" not in out
    assert "northwind-hr-assistant-kimi (model=Kimi-K2.6;" in out
    assert "northwind-claims-triage (model=gpt-4o; tools=none)" in out


def test_dry_run_reports_a_planning_error_and_exits_non_zero(monkeypatch, capsys, prompt_environment):
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
    forbid_foundry(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(agents_script.main(dry_run=True))
    assert raised.value.code == 1
    assert "AZURE_SEARCH_ENDPOINT" in capsys.readouterr().out


@pytest.mark.parametrize("dry_run", [False, True])
def test_required_without_a_connection_exits_before_any_agent_is_created(monkeypatch, capsys, prompt_environment,
                                                                         dry_run):
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", "true")
    forbid_foundry(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(agents_script.main(dry_run=dry_run))
    assert raised.value.code == 1
    assert "AI_GATEWAY_MODEL_CONNECTION" in capsys.readouterr().err


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
    "order-concierge": "https://e.example/oc", "employee-help-desk": "https://e.example/hr",
    "claims-warranty": "https://e.example/cw", "contact-center": "https://e.example/cc",
}


@pytest.fixture
def recorded_run(monkeypatch, prompt_environment):
    created = {}
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINTS", json.dumps(DEPARTMENT_ENDPOINTS))
    monkeypatch.setattr(agents_script, "AIProjectClient", recording_project(created))
    monkeypatch.setattr(agents_script, "_get_credential", lambda: object())
    return created


def test_default_run_still_creates_agents_with_the_direct_models(recorded_run, capsys):
    asyncio.run(agents_script.main())
    assert len(recorded_run) == 7
    assert recorded_run["northwind-hr-assistant-kimi"] == ("https://e.example/hr", "Kimi-K2.6")
    assert recorded_run["northwind-hr-assistant-gpt-5-4"] == ("https://e.example/hr", "gpt-5.4")
    assert recorded_run["northwind-claims-triage"] == ("https://e.example/cw", "gpt-4o")
    assert recorded_run["northwind-intent-classifier"] == ("https://e.example/cc", "gpt-4o")
    assert "All 7 prompt agents ready" in capsys.readouterr().out


def test_gateway_run_creates_the_same_agents_with_connection_qualified_models(monkeypatch, recorded_run, capsys):
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", "true")
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    monkeypatch.setenv("AI_GATEWAY_MODEL_ALIASES", '{"gpt-5.4": "aisvc-zurlo-northwind/gpt-5-4"}')
    asyncio.run(agents_script.main())
    assert len(recorded_run) == 7
    assert recorded_run["northwind-hr-assistant-kimi"] == (
        "https://e.example/hr", "my-conn/aisvc-zurlo-northwind/kimi-k2-6")
    assert recorded_run["northwind-hr-assistant-gpt-5-4"][1] == "my-conn/aisvc-zurlo-northwind/gpt-5-4"
    assert recorded_run["northwind-claims-triage"] == ("https://e.example/cw", "my-conn/aisvc-zurlo-northwind/gpt-4o")
    assert all(model.startswith("my-conn/") for _, model in recorded_run.values())
    assert "All 7 prompt agents ready" in capsys.readouterr().out


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


def setup_arguments(monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", [
        "setup-orders-foundry-demo.py",
        "--project-endpoint", "https://aisvc.example/api/projects/northwind-orders",
        "--tenant", "tenant-fixture",
        "--template-id", "/subscriptions/s/projects/northwind-orders/humanevaluationtemplates/t",
        *extra,
    ])


def test_review_agent_model_is_unchanged_by_default(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("The gateway helpers must not even load without gateway settings")

    monkeypatch.setattr(setup_script.importlib.util, "spec_from_file_location", refuse)
    assert setup_script.agent_model("gpt-4o") == "gpt-4o"


def test_review_agent_model_follows_the_prompt_agent_rule(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    assert setup_script.agent_model("gpt-4o") == "my-conn/aisvc-zurlo-northwind/gpt-4o"
    assert setup_script.agent_model("Kimi-K2.6") == "my-conn/aisvc-zurlo-northwind/kimi-k2-6"
    monkeypatch.delenv("AI_GATEWAY_MODEL_CONNECTION")
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", "true")
    with pytest.raises(ValueError, match="AI_GATEWAY_MODEL_CONNECTION"):
        setup_script.agent_model("gpt-4o")


def test_review_dry_run_report_only_names_the_model_in_gateway_mode(monkeypatch, capsys):
    setup_arguments(monkeypatch)
    setup_script.main()
    assert "model" not in json.loads(capsys.readouterr().out)
    monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", CONNECTION)
    setup_script.main()
    assert json.loads(capsys.readouterr().out)["model"] == "my-conn/aisvc-zurlo-northwind/gpt-4o"


def test_review_required_without_a_connection_exits_before_any_client(monkeypatch, capsys):
    setup_arguments(monkeypatch, "--apply")
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", "yes")
    monkeypatch.setattr(setup_script, "AzureCliCredential", Offline)
    monkeypatch.setattr(setup_script, "AIProjectClient", Offline)
    with pytest.raises(SystemExit) as raised:
        setup_script.main()
    assert "AI_GATEWAY_MODEL_CONNECTION" in str(raised.value)
    assert capsys.readouterr().out == ""


def review_project(versions, created):
    """Stand-in for the Foundry project client used by the review-agent setup (apply path)."""

    class Agents:
        def list_versions(self, name):
            return list(versions)

        def create_version(self, name, *, definition, description, metadata):
            created.append(definition.model)
            return SimpleNamespace(version="8")

    class Rules:
        def create_or_update(self, rule_id, rule):
            return SimpleNamespace(as_dict=lambda: {"id": rule_id})

    class OpenAI:
        evals = SimpleNamespace(list=lambda: [SimpleNamespace(name=setup_script.EVALUATION_NAME, id="eval-1")])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Project:
        def __init__(self, **kwargs):
            self.agents = Agents()
            self.evaluation_rules = Rules()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_openai_client(self):
            return OpenAI()

    return Project


class ReviewCredential:
    def __init__(self, **kwargs):
        pass

    def close(self):
        pass


def review_digest():
    prompt = yaml.safe_load((SCRIPTS.parent / "agents" / "triage.yaml").read_text(encoding="utf-8"))
    return hashlib.sha256(prompt["instructions"].encode()).hexdigest()


@pytest.mark.parametrize("connection,expected", [
    (None, "gpt-4o"), (CONNECTION, "my-conn/aisvc-zurlo-northwind/gpt-4o"),
])
def test_review_apply_creates_once_per_effective_model(monkeypatch, capsys, connection, expected):
    setup_arguments(monkeypatch, "--apply")
    if connection:
        monkeypatch.setenv("AI_GATEWAY_MODEL_CONNECTION", connection)
    monkeypatch.setattr(setup_script, "AzureCliCredential", ReviewCredential)
    versions, created = [], []
    monkeypatch.setattr(setup_script, "AIProjectClient", review_project(versions, created))
    setup_script.main()
    assert created == [expected]
    assert json.loads(capsys.readouterr().out)["review_agent_version"] == "8"
    # A version that already carries the effective model is reused rather than duplicated.
    versions.append(SimpleNamespace(
        version="7", metadata={"instructions_sha256": review_digest()}, definition=SimpleNamespace(model=expected)))
    setup_script.main()
    assert created == [expected]
    assert json.loads(capsys.readouterr().out)["review_agent_version"] == "7"
    # A version that carries a different model does not count as the same agent.
    versions[:] = [SimpleNamespace(
        version="6", metadata={"instructions_sha256": review_digest()}, definition=SimpleNamespace(model="other"))]
    setup_script.main()
    assert created == [expected, expected]


# --- knowledge-base MCP tools through the gateway tool servers -----------------------------------------

HR_TOOLSERVER = "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-hr-knowledge/mcp"
HR_ROUTES = json.dumps({"nw-hr-kb": {"toolserver": "northwind-hr-knowledge", "connection": "nw-hr-kb-gw"}})


def hr_route(prefix="knowledge_"):
    return agents_script.KbRoute(HR_TOOLSERVER, "nw-hr-kb-gw", prefix)


def test_kb_routes_parse_with_the_default_tool_prefix():
    routes = agents_script._parse_kb_routes(HR_ROUTES)
    assert routes == {"nw-hr-kb": hr_route()}
    custom = agents_script._parse_kb_routes(json.dumps({" nw-hr-kb ": {
        "toolserver": "northwind-hr-knowledge", "connection": " nw-hr-kb-gw ", "tool_prefix": "kb_"}}))
    assert custom == {"nw-hr-kb": hr_route("kb_")}
    assert agents_script._parse_kb_routes("") == {} and agents_script._parse_kb_routes("  ") == {}
    assert agents_script._parse_kb_routes("{}") == {}


@pytest.mark.parametrize("raw", [
    "not json", "[]", '"x"', '{"nw-hr-kb": "northwind-hr-knowledge"}', '{"": {"toolserver": "a-b", "connection": "abc"}}',
    '{"k": {"toolserver": "northwind-hr-knowledge"}}',
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
    routed = agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_KB_ROUTES": HR_ROUTES})
    assert routed.required is False and routed.routes == {"nw-hr-kb": hr_route()}
    required = agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_REQUIRED": "true"})
    assert required.required is True and dict(required.routes) == {}
    with pytest.raises(ValueError, match="must be true, 1 or yes"):
        agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_REQUIRED": "ture"})
    with pytest.raises(ValueError, match="AI_GATEWAY_MCP_KB_ROUTES"):
        agents_script.gateway_mcp_from_env({"AI_GATEWAY_MCP_KB_ROUTES": "nope"})


def test_a_routed_kb_uses_its_tool_server_without_needing_the_search_endpoint(monkeypatch):
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT", raising=False)
    mcp = agents_script.GatewayMcp(False, {"nw-hr-kb": hr_route()})
    assert agents_script._kb_mcp_entry("agent", "nw-hr-kb", "nw-hr-kb-mcp", mcp) == {
        "type": "mcp", "server_label": "nw-hr-kb", "server_url": HR_TOOLSERVER, "require_approval": "never",
        "allowed_tools": ["knowledge_knowledge_base_retrieve"], "project_connection_id": "nw-hr-kb-gw",
    }
    custom = agents_script.GatewayMcp(False, {"nw-hr-kb": hr_route("kb_")})
    assert agents_script._kb_mcp_entry("agent", "nw-hr-kb", "x", custom)["allowed_tools"] == [
        "kb_knowledge_base_retrieve"]


def test_an_unrouted_kb_keeps_its_direct_search_endpoint(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net/")
    direct = {
        "type": "mcp", "server_label": "nw-hr-kb", "require_approval": "never",
        "server_url": "https://search.example.net/knowledgebases/nw-hr-kb/mcp?api-version=2026-05-01-preview",
        "allowed_tools": ["knowledge_base_retrieve"], "project_connection_id": "nw-hr-kb-mcp",
    }
    assert agents_script._kb_mcp_entry("agent", "nw-hr-kb", "nw-hr-kb-mcp", None) == direct
    other = agents_script.GatewayMcp(False, {"another-kb": hr_route()})
    assert agents_script._kb_mcp_entry("agent", "nw-hr-kb", "nw-hr-kb-mcp", other) == direct
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
    with pytest.raises(RuntimeError, match="AZURE_SEARCH_ENDPOINT"):
        agents_script._kb_mcp_entry("agent", "nw-hr-kb", "nw-hr-kb-mcp", None)


def test_required_mcp_refuses_a_kb_without_a_route(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")
    mcp = agents_script.GatewayMcp(True, {"another-kb": hr_route()})
    with pytest.raises(ValueError, match=r"AI_GATEWAY_MCP_REQUIRED.*no route for knowledge base 'nw-hr-kb'.*'agent'"):
        agents_script._kb_mcp_entry("agent", "nw-hr-kb", "nw-hr-kb-mcp", mcp)
    assert agents_script._kb_mcp_entry("agent", "another-kb", "x", mcp)["server_url"] == HR_TOOLSERVER


def test_both_knowledge_block_styles_are_routed(monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.net")
    monkeypatch.setenv("AZURE_SEARCH_KNOWLEDGE_BASE", "northwind-kb")
    mcp = agents_script.GatewayMcp(True, {"nw-hr-kb": hr_route(), "northwind-kb": agents_script.KbRoute(
        "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-orders-knowledge/mcp",
        "northwind-kb-gw", "knowledge_")})
    tool_block = {"name": "hr", "instructions": "x", "tools": [{"type": "knowledge", "index": "nw-hr"}]}
    [entry] = agents_script._knowledge_tools_for_agent(tool_block, mcp=mcp)
    assert entry["server_url"] == HR_TOOLSERVER and entry["project_connection_id"] == "nw-hr-kb-gw"
    legacy_block = {"name": "orders", "instructions": "x", "knowledge": [
        {"kind": "foundry_iq", "knowledge_base": "northwind-kb", "connection": "northwind-kb-mcp"}]}
    [entry] = agents_script._knowledge_tools_for_agent(legacy_block, mcp=mcp)
    assert entry["server_url"].endswith("/northwind-orders-knowledge/mcp")
    assert entry["project_connection_id"] == "northwind-kb-gw"
    # Without routing the same blocks still go straight to Search, exactly as before.
    [direct] = agents_script._knowledge_tools_for_agent(tool_block)
    assert direct["server_url"].startswith("https://search.example.net/knowledgebases/nw-hr-kb/mcp")


def test_required_mcp_also_covers_yaml_declared_mcp_tools():
    gateway_tool = {"type": "mcp", "server_label": "lookup", "server_url": HR_TOOLSERVER}
    direct_tool = {"type": "mcp", "server_label": "lookup", "server_url": "https://ca-x.example.io/mcp"}
    cfg = {"name": "agent-one", "instructions": "x"}
    mcp = agents_script.GatewayMcp(True, {})
    [resolved] = agents_script._tools_for_agent({**cfg, "tools": [gateway_tool]}, mcp=mcp)
    assert resolved["server_url"] == HR_TOOLSERVER
    with pytest.raises(ValueError, match=r"AI_GATEWAY_MCP_REQUIRED.*'agent-one'.*directly"):
        agents_script._tools_for_agent({**cfg, "tools": [direct_tool]}, mcp=mcp)
    # Not required (or no MCP settings at all): a direct MCP server is left alone.
    for relaxed in (None, agents_script.GatewayMcp(False, {})):
        [kept] = agents_script._tools_for_agent({**cfg, "tools": [direct_tool]}, mcp=relaxed)
        assert kept["server_url"] == "https://ca-x.example.io/mcp/"


def test_tool_summary_marks_where_each_mcp_tool_goes():
    tools = [
        {"type": "mcp", "server_label": "nw-hr-kb", "server_url": HR_TOOLSERVER},
        {"type": "mcp", "server_label": "x", "server_url": "https://search.example.net/knowledgebases/x/mcp"},
        {"type": "function", "name": "lookup"},
    ]
    assert agents_script._tool_summary(tools) == "mcp:nw-hr-kb@gateway, mcp:x@direct, function:lookup"
    assert agents_script._tool_summary([]) == "none"


def test_dry_run_shows_the_routed_knowledge_base_and_needs_no_search_endpoint(monkeypatch, capsys, prompt_environment):
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
    monkeypatch.setenv("AI_GATEWAY_MCP_KB_ROUTES", HR_ROUTES)
    monkeypatch.setenv("AI_GATEWAY_MCP_REQUIRED", "true")
    forbid_foundry(monkeypatch)
    asyncio.run(agents_script.main(dry_run=True))
    out = capsys.readouterr().out
    assert "Gateway MCP: 1 knowledge-base route(s); knowledge bases without a route are an error" in out
    assert "northwind-hr-assistant (model=gpt-4o; tools=mcp:nw-hr-kb@gateway)" in out
    assert "@direct" not in out and "nothing was sent to Foundry" in out


@pytest.mark.parametrize("dry_run", [False, True])
def test_required_mcp_without_a_route_fails_before_anything_is_created(monkeypatch, capsys, recorded_run, dry_run):
    monkeypatch.setenv("AI_GATEWAY_MCP_REQUIRED", "true")
    with pytest.raises(SystemExit) as raised:
        asyncio.run(agents_script.main(dry_run=dry_run))
    assert raised.value.code == 1
    assert "no route for knowledge base 'nw-hr-kb'" in capsys.readouterr().out
    # The agents without a knowledge base are not the problem; the HR assistants never reach Foundry.
    assert not any(name.startswith("northwind-hr-assistant") for name in recorded_run)


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


def test_routed_run_hands_the_gateway_tool_to_the_prompt_agent_definition(monkeypatch, prompt_environment):
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
    monkeypatch.setenv("AI_GATEWAY_MCP_KB_ROUTES", HR_ROUTES)
    asyncio.run(agents_script.main())
    [tool] = created["northwind-hr-assistant"][2]
    assert tool["server_url"] == HR_TOOLSERVER
    assert tool["project_connection_id"] == "nw-hr-kb-gw"
    assert tool["allowed_tools"] == ["knowledge_knowledge_base_retrieve"]
    assert created["northwind-hr-assistant"][1] == "gpt-4o"

