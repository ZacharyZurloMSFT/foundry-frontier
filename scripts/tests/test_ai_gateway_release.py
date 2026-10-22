"""Offline tests for the AI Gateway release unit: manifest, plan, apply guard rails and MCP verify."""
import copy
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("ai-gateway-release")
common = release.common
def fixed_now():
    return datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


NOW = fixed_now
SECRET = "s3cr3t-search-admin-key-0123456789"
LOOKUP = "northwind-insurance-lookup"
KNOWLEDGE = "northwind-insurance-knowledge"
MANAGED_PATH = re.compile(r"/workspaces/default/toolServers/(northwind-insurance-(?:lookup|knowledge))")
MCP_PATH = re.compile(r"/default/toolservers/([a-z0-9-]+)/mcp")


@pytest.fixture(name="manifest")
def manifest_fixture():
    return release.load_manifest(release.DEFAULT_MANIFEST)


def live_models(manifest):
    provider = manifest["modelProvider"]["name"]
    return [{
        "id": f"{common.GATEWAY_ID}/modelProviders/{provider}/models/{registration}", "name": registration,
        "properties": {
            "deployment": {"modelName": entry["modelName"], "modelVersion": "1"}, "providerKind": "Foundry",
            "supportedEndpoints": [f"/openai/v1/{endpoint}" for endpoint in entry["endpoints"]],
        },
    } for registration, entry in manifest["expectedModels"].items()]


def live_item(manifest, name, *, redaction="drop", etag="etag-live", **changes):
    """A tool server as the service stores it: secret header values are never returned."""
    properties = release.desired_properties(manifest["toolServers"][name])
    for endpoint in properties["endpoints"]:
        credentials = endpoint["credentials"]
        if "headers" in credentials:
            if redaction == "drop":
                del credentials["headers"]
            else:
                credentials["headers"] = {header: ["***"] for header in credentials["headers"]}
    properties.update(changes)
    return {"id": f"{common.GATEWAY_ID}/workspaces/default/toolServers/{name}", "name": name, "etag": etag,
            "properties": properties}


class FakeGateway:
    """The AI Gateway control plane (ARM) and MCP data plane behind the one runner function."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.servers = {}
        self.models = live_models(manifest)
        self.mcp = {name: {"tools": list(entry["expectedTools"]), "mode": "json", "page_size": 100}
                    for name, entry in manifest["toolServers"].items()}
        self.calls = []
        self.bodies = {}
        self.redaction = "drop"
        self.drop_fields = ()
        self.put_errors = {}
        self.counter = 0

    def add(self, name, **changes):
        self.servers[name] = live_item(self.manifest, name, redaction=self.redaction, **changes)

    def puts(self):
        return [call for call in self.calls if call["method"] == "PUT"]

    def methods(self):
        return {call["method"] for call in self.calls}

    def __call__(self, method, url, *, params=None, headers=None, body=None, scope=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "body": body,
                           "scope": scope})
        parts = urlsplit(url)
        if parts.netloc == "management.azure.com":
            return self._arm(method, parts.path, params, body)
        assert parts.netloc == "ai-gateway-northwind.azure-api.net", url
        return self._mcp(method, parts.path, headers, body, scope)

    def _arm(self, method, path, params, body):
        assert params == {"api-version": "2025-09-01-preview"}
        collection = f"{common.GATEWAY_ID}/workspaces/default"
        if method == "GET" and path == f"{collection}/toolServers":
            return common.Response(200, {"value": copy.deepcopy(list(self.servers.values()))})
        if method == "GET" and path == f"{collection}/models":
            return common.Response(200, {"value": copy.deepcopy(self.models)})
        match = MANAGED_PATH.search(path)
        assert method == "PUT" and match, f"unexpected ARM call {method} {path}"
        name = match.group(1)
        if name in self.put_errors:
            raise self.put_errors[name]
        self.bodies[name] = copy.deepcopy(body)
        properties = copy.deepcopy(body["properties"])
        for endpoint in properties["endpoints"]:
            if "headers" in endpoint["credentials"]:
                if self.redaction == "drop":
                    del endpoint["credentials"]["headers"]
                else:
                    endpoint["credentials"]["headers"] = {k: ["***"] for k in endpoint["credentials"]["headers"]}
        for field in self.drop_fields:
            properties.pop(field, None)
        self.counter += 1
        self.servers[name] = {"id": f"{collection}/toolServers/{name}", "name": name,
                              "etag": f"etag-{self.counter}", "properties": properties}
        return common.Response(200, copy.deepcopy(self.servers[name]))

    def _mcp(self, method, path, headers, body, scope):
        assert method == "POST"
        match = MCP_PATH.fullmatch(path)
        assert match, path
        server = self.mcp.get(match.group(1))
        if server is None:
            raise common.RunnerError("Azure request POST failed (HTTP 404)", status=404)
        assert scope == common.TOKEN_SCOPE
        assert headers["Accept"] == "application/json, text/event-stream"
        assert headers["MCP-Protocol-Version"] == "2025-06-18"
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Azure API Management", "version": "1"}}
        else:
            assert body["method"] == "tools/list", body["method"]
            start = int((body["params"] or {}).get("cursor", 0))
            size = server["page_size"]
            chunk = server["tools"][start:start + size]
            result = {"tools": [{"name": tool, "inputSchema": {"type": "object"}} for tool in chunk]}
            if start + size < len(server["tools"]):
                result["nextCursor"] = str(start + size)
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        return self._frame(message, server["mode"])

    @staticmethod
    def _frame(message, mode):
        if mode == "json":
            return common.Response(200, message)
        if mode == "sse":
            return common.Response(200, None, "event: message\ndata: " + json.dumps(message) + "\n\n")
        pretty = "\n".join("data: " + line for line in json.dumps(message, indent=1).splitlines())
        noise = 'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message"}\n\n'
        return common.Response(200, None, ": keep-alive\n\n" + noise + "event: message\n" + pretty + "\n\n")


def run(gateway, *args, environ=None):
    return release.main(list(args), runner=gateway, environ=environ if environ is not None else {}, now=NOW)


def make_plan(gateway, tmp_path, *extra):
    path = tmp_path / "plan.json"
    assert run(gateway, "plan", "--out", str(path), *extra) == 0
    return path


def actions_by_name(plan):
    return {action["name"]: action for action in plan["actions"]}


# --- manifest -------------------------------------------------------------------------------------


def test_repository_manifest_defines_the_managed_boundary(manifest):
    assert set(manifest["toolServers"]) == {LOOKUP, KNOWLEDGE}
    lookup, knowledge = manifest["toolServers"][LOOKUP], manifest["toolServers"][KNOWLEDGE]
    assert lookup["endpoints"][0]["namespace"] == "lookup" and knowledge["endpoints"][0]["namespace"] == "knowledge"
    assert lookup["endpoints"][0]["mcp"] == {
        "transport": "streamableHttp",
        "url": "https://ca-insurance-lookup.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/"}
    assert knowledge["endpoints"][0]["mcp"]["url"] == (
        "https://srch-zurlo-northwind.search.windows.net/knowledgebases/northwind-insurance-kb/mcp"
        "?api-version=2026-05-01-preview")
    assert lookup["endpoints"][0]["credentials"] == {"type": "none"}
    assert knowledge["endpoints"][0]["credentials"] == {
        "type": "header", "headers": {"api-key": ["${SEARCH_ADMIN_KEY}"]}}
    assert lookup["expectedTools"] == ["lookup_lookup_policy", "lookup_lookup_claim"]
    assert knowledge["expectedTools"] == ["knowledge_knowledge_base_retrieve"]
    assert set(manifest["observed"]) == {
        "northwind-api", "northwind-claims-knowledge", "northwind-contact-center-knowledge",
        "northwind-hr-knowledge", "northwind-orders-knowledge"}
    assert set(manifest["stale"]) == {"northwind-order-lookup", "northwind-resolutions"}
    assert set(manifest["deferred"]) == {"northwind-insurance-review"}


def test_deferred_work_order_server_is_documented_and_never_managed(manifest):
    reason = manifest["deferred"]["northwind-insurance-review"]["reason"]
    assert "x-orders-workflow" in reason and "x-harness-workflow" in reason and "not registered" in reason
    assert "northwind-insurance-review" not in manifest["toolServers"]
    concern = manifest["observed"]["northwind-api"]["concern"].lower()
    assert "decide" in concern and "delete" in concern and "never agent tools" in concern
    assert all(entry["retiredHostFragment"] == "bluecliff" for entry in manifest["stale"].values())


def test_manifest_models_match_the_verified_registrations(manifest):
    expected = {
        "gpt-4o": ("gpt-4o", ["chat/completions", "responses"]),
        "gpt-5-4": ("gpt-5.4", ["chat/completions", "responses"]),
        "gpt-5-4-mini": ("gpt-5.4-mini", ["chat/completions", "responses"]),
        "gpt-5-6-luna": ("gpt-5.6-luna", ["chat/completions", "responses"]),
        "kimi-k2-6": ("Kimi-K2.6", ["chat/completions"]),
        "text-embedding-3-small": ("text-embedding-3-small", ["embeddings"]),
    }
    assert {key: (entry["modelName"], entry["endpoints"]) for key, entry in manifest["expectedModels"].items()} == expected
    for registration, entry in manifest["expectedModels"].items():
        assert common.model_alias(entry["modelName"], manifest["modelProvider"]["name"]) == (
            f"{common.MODEL_PROVIDER}/{registration}")


def test_manifest_urls_are_https_and_placeholders_only_hold_secrets(manifest):
    urls = [endpoint["mcp"]["url"] for entry in manifest["toolServers"].values() for endpoint in entry["endpoints"]]
    urls.append(manifest["deferred"]["northwind-insurance-review"]["backend"])
    assert urls and all(urlsplit(url).scheme == "https" for url in urls)
    text = release.DEFAULT_MANIFEST.read_text(encoding="utf-8")
    assert re.findall(r"\$\{[A-Z0-9_]+\}", text) == ["${SEARCH_ADMIN_KEY}"]
    assert manifest["gateway"]["url"] == common.GATEWAY_URL


def test_expected_tools_are_namespace_prefixed(manifest):
    for entry in manifest["toolServers"].values():
        namespaces = [endpoint["namespace"] for endpoint in entry["endpoints"]]
        for tool in entry["expectedTools"]:
            namespace, _, base = tool.partition("_")
            assert namespace in namespaces and base
            assert tool == common.prefixed_tool(namespace, base)


def _literal_secret(manifest):
    manifest["toolServers"][KNOWLEDGE]["endpoints"][0]["credentials"]["headers"]["api-key"] = ["literal-key-value"]


def _placeholder_in_url(manifest):
    manifest["toolServers"][LOOKUP]["endpoints"][0]["mcp"]["url"] += "?key=${SEARCH_ADMIN_KEY}"


def _placeholder_in_description(manifest):
    manifest["toolServers"][LOOKUP]["description"] = "uses ${TOKEN}"


def _plain_http(manifest):
    manifest["toolServers"][LOOKUP]["endpoints"][0]["mcp"]["url"] = "http://example.com/mcp/"


def _url_with_credentials(manifest):
    manifest["toolServers"][LOOKUP]["endpoints"][0]["mcp"]["url"] = "https://user:pass@example.com/mcp/"


def _unprefixed_tool(manifest):
    manifest["toolServers"][LOOKUP]["expectedTools"] = ["get_policy"]


def _alias_mismatch(manifest):
    manifest["expectedModels"]["gpt-5.4"] = manifest["expectedModels"].pop("gpt-5-4")


def _overlap_with_stale(manifest):
    manifest["stale"][LOOKUP] = {"reason": "x", "retiredHostFragment": "x", "decision": "x"}


def _gateway_changed(manifest):
    manifest["gateway"]["name"] = "another-gateway"


def _unknown_section(manifest):
    manifest["extra"] = {}


def _managed_identity(manifest):
    manifest["toolServers"][LOOKUP]["endpoints"][0]["credentials"] = {"type": "managedIdentity"}


def _openapi_kind(manifest):
    manifest["toolServers"][LOOKUP]["endpoints"][0]["kind"] = "openApi"


def _bad_failure_mode(manifest):
    manifest["toolServers"][LOOKUP]["failureMode"] = "ignore"


def _duplicate_namespace(manifest):
    endpoints = manifest["toolServers"][LOOKUP]["endpoints"]
    endpoints.append(copy.deepcopy(endpoints[0]))


def _unknown_endpoint(manifest):
    manifest["expectedModels"]["gpt-4o"]["endpoints"] = ["chat/completions", "images"]


def _wrong_schema_version(manifest):
    manifest["schemaVersion"] = 2


def _bad_name(manifest):
    manifest["toolServers"]["Bad Name"] = manifest["toolServers"].pop(LOOKUP)


@pytest.mark.parametrize(("mutation", "fragment"), [
    (_literal_secret, "environment placeholder"),
    (_placeholder_in_url, "only allowed in credential header values"),
    (_placeholder_in_description, "only allowed in credential header values"),
    (_plain_http, "https URL"),
    (_url_with_credentials, "https URL"),
    (_unprefixed_tool, "<namespace>_<tool>"),
    (_alias_mismatch, "alias rule"),
    (_overlap_with_stale, "more than one manifest section"),
    (_gateway_changed, "gateway coordinates"),
    (_unknown_section, "Unknown manifest sections"),
    (_managed_identity, "not managed by this unit"),
    (_openapi_kind, "other source kinds are not managed"),
    (_bad_failure_mode, "failureMode"),
    (_duplicate_namespace, "repeats a namespace"),
    (_unknown_endpoint, "endpoints must be"),
    (_wrong_schema_version, "schemaVersion"),
    (_bad_name, "lowercase letters"),
])
def test_invalid_manifests_are_rejected(manifest, mutation, fragment):
    candidate = copy.deepcopy(manifest)
    mutation(candidate)
    with pytest.raises(release.ManifestError, match=re.escape(fragment)):
        release.validate_manifest(candidate)


def test_manifest_errors_never_echo_secret_looking_values(manifest):
    candidate = copy.deepcopy(manifest)
    _literal_secret(candidate)
    with pytest.raises(release.ManifestError) as literal:
        release.validate_manifest(candidate)
    candidate = copy.deepcopy(manifest)
    _url_with_credentials(candidate)
    with pytest.raises(release.ManifestError) as url:
        release.validate_manifest(candidate)
    assert "literal-key-value" not in str(literal.value)
    assert "user:pass" not in str(url.value)


# --- plan -----------------------------------------------------------------------------------------


def test_plan_creates_missing_managed_servers_without_secrets(manifest):
    gateway = FakeGateway(manifest)
    plan, _ = release.build_plan(gateway, manifest)
    actions = actions_by_name(plan)
    assert {name: action["action"] for name, action in actions.items()} == {LOOKUP: "create", KNOWLEDGE: "create"}
    assert actions[KNOWLEDGE]["requiredEnvironment"] == ["SEARCH_ADMIN_KEY"]
    assert actions[LOOKUP]["requiredEnvironment"] == []
    desired = actions[KNOWLEDGE]["desired"]["endpoints"][0]["credentials"]
    assert desired == {"type": "header", "headers": {"api-key": ["${SEARCH_ADMIN_KEY}"]}}
    assert re.fullmatch(r"[0-9a-f]{64}", plan["planSha256"])
    assert plan["summary"]["create"] == 2
    assert [(call["method"], urlsplit(call["url"]).path.rsplit("/", 1)[-1]) for call in gateway.calls] == [
        ("GET", "toolServers"), ("GET", "models")]
    assert all(call["params"] == {"api-version": common.ARM_API_VERSION} for call in gateway.calls)


@pytest.mark.parametrize("redaction", ["drop", "mask"])
def test_plan_is_noop_when_live_matches_even_though_secrets_are_redacted(manifest, redaction):
    gateway = FakeGateway(manifest)
    gateway.redaction = redaction
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    plan, _ = release.build_plan(gateway, manifest)
    assert [action["action"] for action in plan["actions"]] == ["noop", "noop"]
    assert plan["summary"]["noop"] == 2 and not any(action["differences"] for action in plan["actions"])


def test_secret_values_alone_never_schedule_an_update(manifest):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    gateway.servers[KNOWLEDGE]["properties"]["endpoints"][0]["credentials"]["headers"] = {"api-key": ["a-different-secret"]}
    plan, _ = release.build_plan(gateway, manifest)
    assert actions_by_name(plan)[KNOWLEDGE]["action"] == "noop"
    assert "a-different-secret" not in json.dumps(plan)


def _change_display_name(properties):
    properties["displayName"] = "Old name"


def _change_description(properties):
    properties["description"] = "Old description"


def _change_failure_mode(properties):
    properties["failureMode"] = "failOpen"


def _drop_failure_mode(properties):
    del properties["failureMode"]


def _change_namespace(properties):
    properties["endpoints"][0]["namespace"] = "orders"


def _change_url(properties):
    properties["endpoints"][0]["mcp"]["url"] = "https://retired.example.com/mcp/"


def _change_transport(properties):
    properties["endpoints"][0]["mcp"]["transport"] = "sse"


def _change_credential_type(properties):
    properties["endpoints"][0]["credentials"] = {"type": "header", "headers": {"x-key": ["***"]}}


def _change_required(properties):
    properties["endpoints"][0]["required"] = False


@pytest.mark.parametrize(("change", "difference"), [
    (_change_display_name, "displayName"),
    (_change_description, "description"),
    (_change_failure_mode, "failureMode"),
    (_drop_failure_mode, "failureMode"),
    (_change_namespace, "endpoints[lookup].namespace"),
    (_change_url, "endpoints[lookup].url"),
    (_change_transport, "endpoints[lookup].transport"),
    (_change_credential_type, "endpoints[lookup].credentialType"),
    (_change_required, "endpoints[lookup].required"),
])
def test_plan_updates_when_managed_fields_differ(manifest, change, difference):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    change(gateway.servers[LOOKUP]["properties"])
    plan, _ = release.build_plan(gateway, manifest)
    actions = actions_by_name(plan)
    assert actions[LOOKUP]["action"] == "update" and difference in actions[LOOKUP]["differences"]
    assert actions[KNOWLEDGE]["action"] == "noop"


def test_plan_updates_when_header_names_change_but_not_when_values_do(manifest):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    gateway.servers[KNOWLEDGE]["properties"]["endpoints"][0]["credentials"]["headers"] = {"x-other-key": ["***"]}
    plan, _ = release.build_plan(gateway, manifest)
    action = actions_by_name(plan)[KNOWLEDGE]
    assert action["action"] == "update" and "endpoints[knowledge].headerNames" in action["differences"]


def test_extra_live_endpoints_are_a_conflict_and_apply_refuses_them(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    extra = copy.deepcopy(gateway.servers[LOOKUP]["properties"]["endpoints"][0])
    extra["namespace"] = "extra"
    gateway.servers[LOOKUP]["properties"]["endpoints"].append(extra)
    plan_path = make_plan(gateway, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert actions_by_name(plan)[LOOKUP]["action"] == "conflict"
    assert "extra" in actions_by_name(plan)[LOOKUP]["conflicts"][0]
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes") == 1
    assert not gateway.puts()


def test_plan_is_deterministic_and_changes_with_the_manifest(manifest):
    first, _ = release.build_plan(FakeGateway(manifest), manifest)
    second, _ = release.build_plan(FakeGateway(manifest), manifest)
    assert common.canonical_json(first) == common.canonical_json(second)
    changed = copy.deepcopy(manifest)
    changed["toolServers"][LOOKUP]["description"] = "A different description."
    third, _ = release.build_plan(FakeGateway(changed), changed)
    assert third["planSha256"] != first["planSha256"]


def test_plan_reports_observed_stale_deferred_unlisted_and_model_findings(manifest):
    gateway = FakeGateway(manifest)
    api = copy.deepcopy(live_item(manifest, LOOKUP))
    api["name"] = "northwind-api"
    api["properties"]["endpoints"] = [{"kind": "openApi", "namespace": "api", "credentials": {"type": "none"},
                                       "openApi": {"specSource": {"type": "inline"}}, "required": True}]
    gateway.servers["northwind-api"] = api
    stale = copy.deepcopy(live_item(manifest, LOOKUP))
    stale["name"] = "northwind-order-lookup"
    stale["properties"]["endpoints"][0]["mcp"]["url"] = "https://ca-mcp-tech-lookup.bluecliff-1976581f.canadacentral.azurecontainerapps.io/mcp/"
    gateway.servers["northwind-order-lookup"] = stale
    fixed = copy.deepcopy(stale)
    fixed["name"] = "northwind-resolutions"
    fixed["properties"]["endpoints"][0]["mcp"]["url"] = "https://ca-insurance-review.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/"
    gateway.servers["northwind-resolutions"] = fixed
    review = copy.deepcopy(stale)
    review["name"] = "northwind-insurance-review"
    gateway.servers["northwind-insurance-review"] = review
    mystery = copy.deepcopy(stale)
    mystery["name"] = "mystery-server"
    gateway.servers["mystery-server"] = mystery
    gateway.models = [model for model in gateway.models if model["name"] != "gpt-4o"]
    gateway.models[0]["properties"]["deployment"]["modelName"] = "gpt-5.5"
    gateway.models[1]["properties"]["supportedEndpoints"] = ["/openai/v1/chat/completions"]
    gateway.models[2]["properties"]["supportedEndpoints"].append("/openai/v1/embeddings")
    gateway.models.append({"name": "surprise-model", "properties": {"deployment": {"modelName": "x"}}})
    plan, _ = release.build_plan(gateway, manifest)
    observed = {row["name"]: row for row in plan["observed"]}
    assert observed["northwind-api"]["present"] and "decide" in observed["northwind-api"]["concern"]
    assert not observed["northwind-hr-knowledge"]["present"]
    stale_rows = {row["name"]: row for row in plan["stale"]}
    assert stale_rows["northwind-order-lookup"]["status"] == "needs-human-decision"
    assert stale_rows["northwind-resolutions"]["status"] == "backend-changed-review-manifest"
    assert plan["deferred"][0]["status"] == "present-unexpectedly"
    assert plan["unlisted"] == ["mystery-server"]
    problems = {row["registration"]: row["problems"] for row in plan["models"]["problems"]}
    assert problems["gpt-4o"] == ["registration is missing"]
    assert any("gpt-5.5" in problem for problem in problems["gpt-5-4"])
    assert problems["gpt-5-4-mini"] == ["endpoint responses is missing"]
    assert problems["gpt-5-6-luna"] == ["endpoint embeddings is not expected"]
    assert plan["models"]["unexpected"] == ["surprise-model"]
    assert plan["summary"]["staleNeedingDecision"] == 1 and plan["summary"]["governanceConcerns"] == 1
    assert [action["action"] for action in plan["actions"]] == ["create", "create"]


def test_model_endpoint_comparison_accepts_the_live_spelling(manifest):
    gateway = FakeGateway(manifest)
    for model in gateway.models:
        model["properties"]["supportedEndpoints"] = [
            endpoint.replace("/openai/v1/", "").replace("chat/completions", "ChatCompletions").title()
            for endpoint in model["properties"]["supportedEndpoints"]]
    plan, _ = release.build_plan(gateway, manifest)
    assert plan["models"]["problems"] == []


def test_refresh_secrets_forces_an_update_only_when_requested(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    plan_path = make_plan(gateway, tmp_path, "--refresh-secrets", KNOWLEDGE)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    action = actions_by_name(plan)[KNOWLEDGE]
    assert action["action"] == "update" and action["differences"] == ["credentials.headers (refresh requested)"]
    assert actions_by_name(plan)[LOOKUP]["action"] == "noop" and plan["refreshSecrets"] == [KNOWLEDGE]
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"SEARCH_ADMIN_KEY": SECRET}) == 0
    assert [call["url"].rsplit("/", 1)[-1] for call in gateway.puts()] == [KNOWLEDGE]
    assert gateway.bodies[KNOWLEDGE]["properties"]["endpoints"][0]["credentials"]["headers"] == {"api-key": [SECRET]}
    for name in (LOOKUP, "not-managed"):
        with pytest.raises(release.ManifestError, match="not a managed tool server with header credentials"):
            release.validate_refresh(manifest, [name])


# --- apply ----------------------------------------------------------------------------------------


def test_apply_requires_yes_and_makes_no_calls_without_it(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    gateway.calls.clear()
    assert run(gateway, "apply", "--plan", str(plan_path)) == 1
    assert not gateway.calls


def test_apply_creates_only_the_managed_servers_with_the_contract_body(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    capsys.readouterr()
    receipt_path = tmp_path / "receipt.json"
    code = run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path),
               environ={"SEARCH_ADMIN_KEY": SECRET})
    assert code == 0
    puts = gateway.puts()
    assert [call["url"].rsplit("/", 1)[-1] for call in puts] == [KNOWLEDGE, LOOKUP]
    assert all(call["params"] == {"api-version": "2025-09-01-preview"} for call in puts)
    assert all(call["headers"] == {"If-None-Match": "*"} for call in puts)
    assert all(urlsplit(call["url"]).path.startswith(f"{common.GATEWAY_ID}/workspaces/default/toolServers/")
               for call in puts)
    assert gateway.bodies[LOOKUP] == {"properties": {
        "type": "mcp", "displayName": "Northwind insurance lookup",
        "description": manifest["toolServers"][LOOKUP]["description"], "failureMode": "failClosed",
        "endpoints": [{"kind": "mcp", "namespace": "lookup", "required": True,
                       "mcp": {"transport": "streamableHttp", "url": "https://ca-insurance-lookup.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/"},
                       "credentials": {"type": "none"}}]}}
    knowledge = gateway.bodies[KNOWLEDGE]["properties"]["endpoints"][0]
    assert knowledge["credentials"] == {"type": "header", "headers": {"api-key": [SECRET]}}
    assert knowledge["mcp"]["url"].endswith("/knowledgebases/northwind-insurance-kb/mcp?api-version=2026-05-01-preview")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["complete"] is True and receipt["kind"] == "ai-gateway-release-receipt"
    assert receipt["planSha256"] == json.loads(plan_path.read_text(encoding="utf-8"))["planSha256"]
    assert [(row["name"], row["outcome"], row["converged"]) for row in receipt["results"]] == [
        (KNOWLEDGE, "applied", True), (LOOKUP, "applied", True)]
    assert gateway.methods() == {"GET", "PUT"}


def test_apply_updates_with_the_etag_and_preserves_live_policies(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP, etag="etag-live", displayName="Old name")
    gateway.add(KNOWLEDGE)
    gateway.servers[LOOKUP]["properties"]["policies"] = [{"kind": "rate-limit"}]
    plan_path = make_plan(gateway, tmp_path)
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={}) == 0
    (put,) = gateway.puts()
    assert put["headers"] == {"If-Match": "etag-live"}
    assert gateway.bodies[LOOKUP]["properties"]["policies"] == [{"kind": "rate-limit"}]
    assert gateway.bodies[LOOKUP]["properties"]["displayName"] == "Northwind insurance lookup"


def test_apply_update_without_an_etag_requires_the_resource_to_exist(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP, displayName="Old name")
    gateway.add(KNOWLEDGE)
    del gateway.servers[LOOKUP]["etag"]
    plan_path = make_plan(gateway, tmp_path)
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes") == 0
    assert gateway.puts()[0]["headers"] == {"If-Match": "*"}


def test_apply_refuses_when_the_live_gateway_changed_after_the_plan(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    gateway.add(LOOKUP)
    capsys.readouterr()
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"SEARCH_ADMIN_KEY": SECRET}) == 1
    assert "no longer matches the reviewed plan" in capsys.readouterr().err
    assert not gateway.puts()


def test_apply_refuses_when_the_manifest_changed_after_the_plan(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    changed = copy.deepcopy(manifest)
    changed["toolServers"][LOOKUP]["displayName"] = "Renamed"
    with pytest.raises(common.GatewayError, match="no longer matches"):
        release.apply_plan(gateway, changed, json.loads(plan_path.read_text(encoding="utf-8")), {})
    assert not gateway.puts()


@pytest.mark.parametrize("tamper", ["actions", "hash", "kind"])
def test_apply_refuses_a_modified_plan_file(manifest, tmp_path, tamper):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if tamper == "actions":
        plan["actions"][0]["action"] = "noop"
    elif tamper == "hash":
        plan["planSha256"] = "0" * 64
    else:
        plan["kind"] = "something-else"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"SEARCH_ADMIN_KEY": SECRET}) == 1
    assert not gateway.puts()


def test_apply_with_a_missing_secret_fails_clearly_before_any_write(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    capsys.readouterr()
    for environ in ({}, {"SEARCH_ADMIN_KEY": ""}):
        assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ=environ) == 1
        assert "SEARCH_ADMIN_KEY" in capsys.readouterr().err
    assert not gateway.puts()


def test_apply_with_nothing_to_do_writes_nothing(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    plan_path = make_plan(gateway, tmp_path)
    receipt_path = tmp_path / "receipt.json"
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path)) == 0
    assert not gateway.puts()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["results"] == []


def test_apply_never_overwrites_an_existing_receipt_and_checks_before_writing(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("keep", encoding="utf-8")
    gateway.calls.clear()
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path),
               environ={"SEARCH_ADMIN_KEY": SECRET}) == 1
    assert receipt_path.read_text(encoding="utf-8") == "keep"
    assert not gateway.calls


def test_apply_leaves_observed_stale_deferred_and_models_untouched(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    for name in ("northwind-api", "northwind-order-lookup", "northwind-resolutions", "northwind-insurance-review",
                 "northwind-hr-knowledge", "mystery-server"):
        item = copy.deepcopy(live_item(manifest, LOOKUP))
        item["name"] = name
        gateway.servers[name] = item
    before = copy.deepcopy({name: gateway.servers[name] for name in gateway.servers})
    models_before = copy.deepcopy(gateway.models)
    plan_path = make_plan(gateway, tmp_path)
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"SEARCH_ADMIN_KEY": SECRET}) == 0
    assert {call["url"].rsplit("/", 1)[-1] for call in gateway.puts()} == {LOOKUP, KNOWLEDGE}
    for name, item in before.items():
        assert gateway.servers[name] == item
    assert gateway.models == models_before
    assert gateway.methods() <= {"GET", "PUT"}


def test_a_changed_service_that_ignores_a_field_is_reported_as_not_converged(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.drop_fields = ("failureMode",)
    plan_path = make_plan(gateway, tmp_path)
    receipt = release.apply_plan(gateway, manifest, json.loads(plan_path.read_text(encoding="utf-8")),
                                 {"SEARCH_ADMIN_KEY": SECRET}, now=NOW)
    assert receipt["complete"] is True
    assert all(row["converged"] is False and row["remainingDifferences"] == ["failureMode"]
               for row in receipt["results"])


def test_a_failed_write_stops_with_a_partial_receipt_and_scrubs_secrets(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    gateway.put_errors[KNOWLEDGE] = common.RunnerError(f"Azure request failed with {SECRET}", status=400)
    plan_path = make_plan(gateway, tmp_path)
    capsys.readouterr()
    receipt_path = tmp_path / "receipt.json"
    code = run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path),
               environ={"SEARCH_ADMIN_KEY": SECRET})
    captured = capsys.readouterr()
    assert code == 1
    assert SECRET not in captured.out + captured.err + receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["complete"] is False
    assert [(row["name"], row["outcome"]) for row in receipt["results"]] == [(KNOWLEDGE, "failed")]
    assert "***" in receipt["results"][0]["error"]
    assert [call["url"].rsplit("/", 1)[-1] for call in gateway.puts()] == [KNOWLEDGE]
    assert "Nothing was retried or rolled back" in captured.err


def test_secrets_never_reach_outputs_receipts_plans_or_argv(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path, "--refresh-secrets", KNOWLEDGE)
    receipt_path = tmp_path / "receipt.json"
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path),
               environ={"SEARCH_ADMIN_KEY": SECRET}) == 0
    captured = capsys.readouterr()
    for text in (captured.out, captured.err, plan_path.read_text(encoding="utf-8"),
                 receipt_path.read_text(encoding="utf-8")):
        assert SECRET not in text
    assert SECRET in json.dumps(gateway.bodies[KNOWLEDGE])
    assert SECRET not in json.dumps([{key: value for key, value in call.items() if key != "body"}
                                     for call in gateway.calls])


def test_release_script_uses_only_the_injected_runner_and_takes_no_secret_arguments():
    source = (ROOT / "scripts" / "ai-gateway-release.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+(?:subprocess|httpx|requests|urllib\.request|http\.client|socket|azure)\b",
                         source, re.MULTILINE)
    options = option_strings(release.build_parser())
    assert {"--manifest", "--plan", "--yes", "--receipt", "--out", "--strict", "--refresh-secrets"} <= options
    assert not [option for option in options if re.search(r"key|token|password|secret", option)
                and option != "--refresh-secrets"]


def option_strings(parser):
    found = set()
    for action in parser._actions:
        found.update(action.option_strings)
        if isinstance(getattr(action, "choices", None), dict):
            for subparser in action.choices.values():
                found |= option_strings(subparser)
    return found


def test_release_script_has_no_delete_or_patch_operation(manifest, tmp_path):
    source = (ROOT / "scripts" / "ai-gateway-release.py").read_text(encoding="utf-8")
    assert not re.search(r"""["'](DELETE|PATCH)["']""", source)
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"SEARCH_ADMIN_KEY": SECRET}) == 0
    assert run(gateway, "verify") == 0
    assert gateway.methods() == {"GET", "PUT", "POST"}


# --- verify ---------------------------------------------------------------------------------------


def verify(gateway, capsys, *flags):
    code = run(gateway, "verify", *flags)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("mode", ["json", "sse", "sse-multi"])
def test_verify_parses_json_and_sse_frames(manifest, capsys, mode):
    gateway = FakeGateway(manifest)
    for server in gateway.mcp.values():
        server["mode"] = mode
    code, result = verify(gateway, capsys)
    assert code == 0 and result["ok"] is True
    servers = {row["name"]: row for row in result["servers"]}
    assert servers[LOOKUP]["tools"] == ["lookup_lookup_claim", "lookup_lookup_policy"]
    assert servers[KNOWLEDGE]["tools"] == ["knowledge_knowledge_base_retrieve"]
    assert servers[LOOKUP]["protocolVersion"] == "2025-06-18" and servers[LOOKUP]["serverName"] == "Azure API Management"
    assert servers[LOOKUP]["url"] == "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp"


def test_verify_sends_only_initialize_and_tools_list_with_the_gateway_token_scope(manifest, capsys):
    gateway = FakeGateway(manifest)
    code, _ = verify(gateway, capsys)
    assert code == 0
    assert [call["body"]["method"] for call in gateway.calls] == ["initialize", "tools/list"] * 2
    for call in gateway.calls:
        assert call["method"] == "POST" and call["scope"] == "https://azure-api.net/aigateway/.default"
        assert call["headers"] == {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                                   "MCP-Protocol-Version": "2025-06-18"}
        assert "/models/" not in call["url"] and call["body"]["jsonrpc"] == "2.0"
        assert call["url"] == common.toolserver_url(call["url"].split("/")[-2])


def test_verify_fails_when_an_expected_tool_is_missing_or_unprefixed(manifest, capsys):
    gateway = FakeGateway(manifest)
    gateway.mcp[LOOKUP]["tools"] = ["lookup_policy", "lookup_lookup_claim"]
    code, result = verify(gateway, capsys)
    row = {item["name"]: item for item in result["servers"]}[LOOKUP]
    assert code == 1 and result["ok"] is False
    assert row["missingTools"] == ["lookup_lookup_policy"] and row["unexpectedTools"] == ["lookup_policy"]


def test_verify_tolerates_unexpected_tools_unless_strict(manifest, capsys):
    gateway = FakeGateway(manifest)
    gateway.mcp[KNOWLEDGE]["tools"].append("knowledge_new_tool")
    code, result = verify(gateway, capsys)
    assert code == 0 and result["servers"][0]["unexpectedTools"] == ["knowledge_new_tool"]
    code, result = verify(gateway, capsys, "--strict")
    assert code == 1 and result["ok"] is False


def test_verify_follows_tool_list_pagination(manifest, capsys):
    gateway = FakeGateway(manifest)
    gateway.mcp[LOOKUP]["page_size"] = 1
    code, result = verify(gateway, capsys)
    assert code == 0
    methods = [call["body"]["method"] for call in gateway.calls if call["url"].endswith(f"{LOOKUP}/mcp")]
    assert methods == ["initialize", "tools/list", "tools/list"]
    assert {row["name"]: row for row in result["servers"]}[LOOKUP]["tools"] == [
        "lookup_lookup_claim", "lookup_lookup_policy"]


def test_verify_reports_transport_and_jsonrpc_errors_per_server(manifest, capsys):
    gateway = FakeGateway(manifest)
    del gateway.mcp[LOOKUP]
    original = gateway._frame

    def error_frame(message, mode):
        return common.Response(200, {"jsonrpc": "2.0", "id": message["id"],
                                     "error": {"code": -32601, "message": "Method not found"}})

    gateway._frame = error_frame
    code, result = verify(gateway, capsys)
    gateway._frame = original
    rows = {row["name"]: row for row in result["servers"]}
    assert code == 1
    assert "HTTP 404" in rows[LOOKUP]["error"]
    assert "-32601" in rows[KNOWLEDGE]["error"] and rows[KNOWLEDGE]["ok"] is False


def test_verify_can_flag_forbidden_tools_on_observed_servers(manifest, capsys):
    gateway = FakeGateway(manifest)
    gateway.mcp["northwind-api"] = {"tools": ["api_list_orders", "api_decide_approval", "api_delete_order"],
                                    "mode": "json", "page_size": 100}
    gateway.mcp["northwind-hr-knowledge"] = {"tools": ["knowledge_knowledge_base_retrieve"], "mode": "sse",
                                             "page_size": 100}
    code, result = verify(gateway, capsys, "--include-observed")
    observed = {row["name"]: row for row in result["observed"]}
    assert code == 0 and result["ok"] is True
    assert observed["northwind-api"]["governanceFindings"] == ["api_decide_approval", "api_delete_order"]
    assert observed["northwind-api"]["toolCount"] == 3
    assert observed["northwind-hr-knowledge"]["reachable"] is True
    assert observed["northwind-claims-knowledge"]["reachable"] is False


@pytest.mark.parametrize(("response", "fragment"), [
    (common.Response(200, None, ""), "empty MCP response"),
    (common.Response(200, None, "{not json"), "invalid MCP JSON"),
    (common.Response(200, {"jsonrpc": "2.0", "id": 99, "result": {}}), "did not contain a result"),
    (common.Response(200, None, "data: not-json\n\n"), "did not contain a result"),
])
def test_unusable_mcp_payloads_are_reported(response, fragment):
    with pytest.raises(release.McpError, match=fragment):
        release.rpc_message(response, 1)


def test_rpc_message_accepts_batches_text_bodies_and_string_ids():
    assert release.rpc_message(common.Response(200, [{"jsonrpc": "2.0", "id": "1", "result": {"a": 1}}]), 1)["result"] == {"a": 1}
    assert release.rpc_message(common.Response(200, '{"jsonrpc": "2.0", "id": 2, "result": {}}'), 2)["result"] == {}


def test_sse_messages_joins_data_lines_and_ignores_comments():
    text = (
        ": comment\n"
        'data: {"jsonrpc": "2.0", "method": "notifications/message"}\n'
        "\n"
        "data: {\n"
        'data:   "id": 2,\n'
        'data:   "result": {}\n'
        "data: }\n"
        "\n"
        "event: ping\n"
        "\n"
    )
    assert [message.get("id") for message in release.sse_messages(text)] == [None, 2]


# --- command line ---------------------------------------------------------------------------------


def test_help_documents_the_required_roles(capsys):
    with pytest.raises(SystemExit) as stop:
        release.main(["--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "API Management Service Contributor" in text and "AI Gateway Tools User" in text
    assert "no tool is invoked and no model is called" in text and "never sees one" in text


def test_plan_writes_the_same_json_to_the_out_file(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    target = tmp_path / "nested" / "plan.json"
    assert run(gateway, "plan", "--out", str(target)) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(target.read_text(encoding="utf-8"))


def test_a_missing_or_invalid_manifest_is_refused(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    assert run(gateway, "plan", "--manifest", str(tmp_path / "missing.json")) == 1
    assert "File not found" in capsys.readouterr().err
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert run(gateway, "plan", "--manifest", str(broken)) == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert not gateway.calls
