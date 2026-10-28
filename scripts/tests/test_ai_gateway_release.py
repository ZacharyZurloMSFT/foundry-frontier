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
SECRET = "s3cr3t-search-query-key-0123456789"
APIM_KEY = "s3cr3t-apim-subscription-key-9876543210"
ENVIRON = {"SEARCH_QUERY_KEY": SECRET, "INSURANCE_AGENTS_KEY": APIM_KEY}
KEY_HEADER = {"type": "header", "headers": {"Ocp-Apim-Subscription-Key": ["${INSURANCE_AGENTS_KEY}"]}}
LOOKUP = "northwind-insurance-lookup"
KNOWLEDGE = "northwind-insurance-knowledge"
REVIEW = "northwind-insurance-review"
MANAGED_PATH = re.compile(r"/workspaces/default/toolServers/(northwind-insurance-(?:lookup|knowledge|review))")
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
        "transport": "streamableHttp", "url": "https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup"}
    assert knowledge["endpoints"][0]["mcp"]["url"] == (
        "https://srch-zurlo-northwind.search.windows.net/knowledgebases/northwind-insurance-kb/mcp"
        "?api-version=2026-05-01-preview")
    assert lookup["endpoints"][0]["credentials"] == KEY_HEADER
    assert knowledge["endpoints"][0]["credentials"] == {
        "type": "header", "headers": {"api-key": ["${SEARCH_QUERY_KEY}"]}}
    assert lookup["expectedTools"] == ["lookup_lookup_policy", "lookup_lookup_claim"]
    assert knowledge["expectedTools"] == ["knowledge_knowledge_base_retrieve"]
    assert set(manifest["observed"]) == {
        "northwind-api", "northwind-claims-knowledge", "northwind-contact-center-knowledge", REVIEW}
    assert manifest["observed"][REVIEW]["reason"].find("workflow-binding header") >= 0
    assert manifest["stale"] == {} and manifest["deferred"] == {}


def test_review_server_is_observed_not_managed_because_the_gateway_cannot_forward_its_headers(manifest):
    assert REVIEW not in manifest["toolServers"]
    reason = manifest["observed"][REVIEW]["reason"]
    assert "workflow-binding header" in reason and "apim-northwind" in reason
    assert "Do not add this endpoint" in reason
    concern = manifest["observed"]["northwind-api"]["concern"].lower()
    assert "decide" in concern and "delete" in concern and "never agent tools" in concern


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
    assert urls and all(urlsplit(url).scheme == "https" for url in urls)
    text = release.DEFAULT_MANIFEST.read_text(encoding="utf-8")
    assert sorted(re.findall(r"\$\{[A-Z0-9_]+\}", text)) == [
        "${INSURANCE_AGENTS_KEY}", "${SEARCH_QUERY_KEY}"]
    assert manifest["gateway"]["url"] == common.GATEWAY_URL


def test_the_locked_insurance_apps_are_registered_only_through_api_management(manifest):
    # ca-insurance-lookup accepts only API Management's gateway token (a direct call is a 401).
    endpoint = manifest["toolServers"][LOOKUP]["endpoints"][0]
    assert urlsplit(endpoint["mcp"]["url"]).hostname == "apim-northwind.azure-api.net"
    assert endpoint["credentials"] == KEY_HEADER
    assert REVIEW not in manifest["toolServers"]
    hosts = {urlsplit(endpoint["mcp"]["url"]).hostname for entry in manifest["toolServers"].values()
             for endpoint in entry["endpoints"]}
    assert not [host for host in hosts if host.endswith(".azurecontainerapps.io")]


def test_expected_tools_are_namespace_prefixed(manifest):
    for entry in manifest["toolServers"].values():
        namespaces = [endpoint["namespace"] for endpoint in entry["endpoints"]]
        for tool in entry["expectedTools"]:
            namespace, _, base = tool.partition("_")
            assert namespace in namespaces and base
            assert tool == common.prefixed_tool(namespace, base)


def test_inline_policies_are_included_in_writes_and_drift_detection(manifest):
    properties = release.desired_properties(manifest["toolServers"][LOOKUP])
    assert properties["policies"] == [{
        "type": "requestRateLimit", "scope": "resource", "callsPerPeriod": 120,
        "periodSeconds": 60, "counterKey": ["identity"],
    }]
    missing = copy.deepcopy(properties)
    missing.pop("policies")
    assert release.diff_views(release.comparable(properties), release.comparable(missing)) == (["policies"], [])
    changed = copy.deepcopy(properties)
    changed["policies"][0]["callsPerPeriod"] = 121
    assert release.diff_views(release.comparable(properties), release.comparable(changed)) == (["policies"], [])


def test_additional_live_inline_policies_are_a_conflict_not_silently_removed(manifest):
    properties = release.desired_properties(manifest["toolServers"][LOOKUP])
    live = copy.deepcopy(properties)
    live["policies"].append({"type": "ipFilter", "action": "allow", "cidrRanges": ["192.0.2.0/24"]})
    differences, conflicts = release.diff_views(release.comparable(properties), release.comparable(live))
    assert differences == ["policies"]
    assert conflicts and "additional or duplicate" in conflicts[0]


@pytest.mark.parametrize("changes", [
    {"counterKey": []}, {"counterKey": ["identity", "identity"]}, {"callsPerPeriod": 0},
    {"callsPerPeriod": True}, {"periodSeconds": -1}, {"type": "cors"}, {"scope": "workspace"},
])
def test_invalid_inline_policies_are_rejected(manifest, changes):
    manifest["toolServers"][LOOKUP]["policies"][0].update(changes)
    with pytest.raises(release.ManifestError, match="policies"):
        release.validate_manifest(manifest)


def _literal_secret(manifest):
    manifest["toolServers"][KNOWLEDGE]["endpoints"][0]["credentials"]["headers"]["api-key"] = ["literal-key-value"]


def _literal_subscription_key(manifest):
    headers = manifest["toolServers"][LOOKUP]["endpoints"][0]["credentials"]["headers"]
    headers["Ocp-Apim-Subscription-Key"] = ["literal-apim-key-value"]


def _placeholder_in_url(manifest):
    manifest["toolServers"][LOOKUP]["endpoints"][0]["mcp"]["url"] += "?key=${SEARCH_QUERY_KEY}"


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
    (_literal_subscription_key, "environment placeholder"),
    (_placeholder_in_url, "only allowed in credential header values"),
    (_placeholder_in_description, "only allowed in credential header values"),
    (_plain_http, "https URL"),
    (_url_with_credentials, "https URL"),
    (_unprefixed_tool, "<namespace>_<tool>"),
    (_alias_mismatch, "alias rule"),
    (_overlap_with_stale, "more than one manifest section"),
    (_gateway_changed, "gateway coordinates"),
    (_unknown_section, "Unknown manifest sections"),
    (_managed_identity, "exactly type and managedIdentity"),
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


def staged_manifest():
    return release.load_manifest(release.MIGRATION_MANIFEST)


def attestation_for(manifest, plan):
    return {
        "schemaVersion": 1, "kind": release.ATTESTATION_KIND, "gateway": plan["gateway"],
        "manifestSha256": plan["manifestSha256"], "planSha256": plan["planSha256"],
        "reviewedBy": "reviewer", "reviewedAt": "2026-10-22T11:00:00Z",
        "expiresAt": "2026-10-22T13:00:00Z",
        "gatewayPrincipalId": common.GATEWAY_IDENTITY_PRINCIPAL_ID,
        "evidence": "https://github.com/ZacharyZurloMSFT/foundry-frontier/issues/1",
        "checks": dict.fromkeys(release.COMPATIBILITY_CHECKS, True),
    }


def test_staged_manifest_uses_direct_identity_and_draft_only_tools():
    manifest = staged_manifest()
    assert set(manifest["toolServers"]) == {LOOKUP, KNOWLEDGE, REVIEW}
    for name in (LOOKUP, REVIEW):
        endpoint = manifest["toolServers"][name]["endpoints"][0]
        assert ".azurecontainerapps.io/mcp/" in endpoint["mcp"]["url"]
        assert endpoint["credentials"]["type"] == "managedIdentity"
    assert manifest["toolServers"][REVIEW]["expectedTools"] == ["review_draft_claim_action"]
    gateway = FakeGateway(manifest)
    plan, _ = release.build_plan(gateway, manifest)
    assert plan["applyGate"]["status"] == "blocked-until-reviewed-compatibility-attestation"
    with pytest.raises(release.ManifestError, match="blocked"):
        release.apply_plan(gateway, manifest, plan, ENVIRON, now=NOW)
    assert not gateway.puts()


@pytest.mark.parametrize("field,value", [
    ("expiresAt", "2026-10-22T11:59:59Z"), ("reviewedAt", "2026-10-22T12:01:00Z"),
    ("expiresAt", "2026-10-24T12:00:00Z"), ("reviewedAt", "2026-10-22T11:00:00"),
    ("planSha256", "wrong"), ("manifestSha256", "wrong"), ("checks", {}),
    ("gatewayPrincipalId", "bad"), ("reviewedBy", ""), ("evidence", "http://unsafe.invalid"),
])
def test_staged_attestation_rejects_stale_unbound_or_incomplete_evidence(field, value):
    manifest = staged_manifest()
    gateway = FakeGateway(manifest)
    plan, _ = release.build_plan(gateway, manifest)
    attestation = {**attestation_for(manifest, plan), field: value}
    with pytest.raises(release.ManifestError):
        release.apply_plan(gateway, manifest, plan, ENVIRON, now=NOW, attestation=attestation)
    assert not gateway.puts()


def test_staged_identity_apply_requires_readback_convergence():
    manifest = staged_manifest()
    gateway = FakeGateway(manifest)
    plan, _ = release.build_plan(gateway, manifest)
    receipt = release.apply_plan(gateway, manifest, plan, ENVIRON, now=NOW,
                                 attestation=attestation_for(manifest, plan))
    assert receipt["complete"] is True
    assert receipt["compatibilityAttestationSha256"]
    assert all(result["converged"] for result in receipt["results"])
    properties = gateway.servers[LOOKUP]["properties"]
    properties["endpoints"][0]["credentials"]["managedIdentity"]["resource"] = "api://11111111-1111-4111-8111-111111111111"
    drift, _ = release.build_plan(gateway, manifest)
    assert "endpoints[lookup].managedIdentity.resource" in actions_by_name(drift)[LOOKUP]["differences"]


def test_managed_identity_needs_explicit_staging_and_an_audience_not_scope(manifest):
    endpoint = manifest["toolServers"][LOOKUP]["endpoints"][0]
    endpoint["credentials"] = {"type": "managedIdentity", "managedIdentity": {
        "resource": "api://0940b5c3-18ec-46f0-8962-7894247c2710"}}
    with pytest.raises(release.ManifestError, match="staged"):
        release.validate_manifest(manifest)
    manifest["migration"] = release.MIGRATION
    endpoint["credentials"]["managedIdentity"]["resource"] += "/.default"
    with pytest.raises(release.ManifestError, match="not a token scope"):
        release.validate_manifest(manifest)


def test_manifest_errors_never_echo_secret_looking_values(manifest):
    candidate = copy.deepcopy(manifest)
    _literal_secret(candidate)
    with pytest.raises(release.ManifestError) as literal:
        release.validate_manifest(candidate)
    candidate = copy.deepcopy(manifest)
    _literal_subscription_key(candidate)
    with pytest.raises(release.ManifestError) as subscription:
        release.validate_manifest(candidate)
    candidate = copy.deepcopy(manifest)
    _url_with_credentials(candidate)
    with pytest.raises(release.ManifestError) as url:
        release.validate_manifest(candidate)
    assert "literal-key-value" not in str(literal.value)
    assert "literal-apim-key-value" not in str(subscription.value)
    assert "user:pass" not in str(url.value)


# --- plan -----------------------------------------------------------------------------------------


def test_plan_creates_missing_managed_servers_without_secrets(manifest):
    gateway = FakeGateway(manifest)
    plan, _ = release.build_plan(gateway, manifest)
    actions = actions_by_name(plan)
    assert {name: action["action"] for name, action in actions.items()} == {
        LOOKUP: "create", KNOWLEDGE: "create"}
    assert actions[KNOWLEDGE]["requiredEnvironment"] == ["SEARCH_QUERY_KEY"]
    assert actions[LOOKUP]["requiredEnvironment"] == ["INSURANCE_AGENTS_KEY"]
    assert REVIEW not in actions
    desired = actions[KNOWLEDGE]["desired"]["endpoints"][0]["credentials"]
    assert desired == {"type": "header", "headers": {"api-key": ["${SEARCH_QUERY_KEY}"]}}
    assert actions[LOOKUP]["desired"]["endpoints"][0]["credentials"] == KEY_HEADER
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


def _change_namespace(properties):
    properties["endpoints"][0]["namespace"] = "claims"


def _change_url(properties):
    properties["endpoints"][0]["mcp"]["url"] = "https://retired.example.com/mcp/"


def _change_transport(properties):
    properties["endpoints"][0]["mcp"]["transport"] = "sse"


def _change_credential_type(properties):
    properties["endpoints"][0]["credentials"] = {"type": "none"}


def _change_required(properties):
    properties["endpoints"][0]["required"] = False


@pytest.mark.parametrize(("change", "difference"), [
    (_change_display_name, "displayName"),
    (_change_description, "description"),
    (_change_failure_mode, "failureMode"),
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


def test_a_service_that_never_returns_failure_mode_is_not_drift(manifest):
    gateway = FakeGateway(manifest)
    for name in (LOOKUP, KNOWLEDGE):
        gateway.add(name)
        del gateway.servers[name]["properties"]["failureMode"]
    plan, _ = release.build_plan(gateway, manifest)
    assert [action["action"] for action in plan["actions"]] == ["noop", "noop"]
    assert not any(action["differences"] for action in plan["actions"])


def test_plan_updates_when_header_names_change_but_not_when_values_do(manifest):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP)
    gateway.add(KNOWLEDGE)
    gateway.servers[KNOWLEDGE]["properties"]["endpoints"][0]["credentials"]["headers"] = {"x-other-key": ["***"]}
    plan, _ = release.build_plan(gateway, manifest)
    action = actions_by_name(plan)[KNOWLEDGE]
    assert action["action"] == "update" and "endpoints[knowledge].headerNames" in action["differences"]


def test_the_subscription_key_header_is_compared_by_name_only_and_without_regard_to_case(manifest):
    gateway = FakeGateway(manifest)
    for name in (LOOKUP, KNOWLEDGE):
        gateway.add(name)
    gateway.servers[LOOKUP]["properties"]["endpoints"][0]["credentials"]["headers"] = {
        "OCP-APIM-SUBSCRIPTION-KEY": ["a-rotated-key"]}
    plan, _ = release.build_plan(gateway, manifest)
    actions = actions_by_name(plan)
    assert actions[LOOKUP]["action"] == "noop" and "a-rotated-key" not in json.dumps(plan)
    assert REVIEW not in actions


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
    manifest = copy.deepcopy(manifest)
    manifest["deferred"]["northwind-deferred-example"] = {"reason": "Deliberately not registered."}
    # The mechanism stays covered although the repository manifest has no tombstones any more.
    manifest["stale"]["northwind-retired-example"] = {
        "reason": "Backend lived on a retired Container Apps domain.", "retiredHostFragment": "bluecliff",
        "decision": "Removed from the gateway; listed so the plan flags a re-registration of the retired host."}
    manifest["stale"]["northwind-moved-example"] = {
        "reason": "Backend lived on a retired Container Apps domain.", "retiredHostFragment": "bluecliff",
        "decision": "Removed from the gateway; listed so the plan flags a re-registration of the retired host."}
    gateway = FakeGateway(manifest)
    api = copy.deepcopy(live_item(manifest, LOOKUP))
    api["name"] = "northwind-api"
    api["properties"]["endpoints"] = [{"kind": "openApi", "namespace": "api", "credentials": {"type": "none"},
                                       "openApi": {"specSource": {"type": "inline"}}, "required": True}]
    gateway.servers["northwind-api"] = api
    stale = copy.deepcopy(live_item(manifest, LOOKUP))
    stale["name"] = "northwind-retired-example"
    stale["properties"]["endpoints"][0]["mcp"]["url"] = "https://ca-retired.bluecliff-1976581f.canadacentral.azurecontainerapps.io/mcp/"
    gateway.servers["northwind-retired-example"] = stale
    fixed = copy.deepcopy(stale)
    fixed["name"] = "northwind-moved-example"
    fixed["properties"]["endpoints"][0]["mcp"]["url"] = "https://ca-insurance-review.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/"
    gateway.servers["northwind-moved-example"] = fixed
    deferred = copy.deepcopy(stale)
    deferred["name"] = "northwind-deferred-example"
    gateway.servers["northwind-deferred-example"] = deferred
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
    assert not observed["northwind-claims-knowledge"]["present"]
    stale_rows = {row["name"]: row for row in plan["stale"]}
    assert stale_rows["northwind-retired-example"]["status"] == "needs-human-decision"
    assert stale_rows["northwind-moved-example"]["status"] == "backend-changed-review-manifest"
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
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"SEARCH_QUERY_KEY": SECRET}) == 0
    assert [call["url"].rsplit("/", 1)[-1] for call in gateway.puts()] == [KNOWLEDGE]
    assert gateway.bodies[KNOWLEDGE]["properties"]["endpoints"][0]["credentials"]["headers"] == {"api-key": [SECRET]}
    assert release.validate_refresh(manifest, [LOOKUP, KNOWLEDGE]) == [KNOWLEDGE, LOOKUP]
    keyless = copy.deepcopy(manifest)
    keyless["toolServers"][LOOKUP]["endpoints"][0]["credentials"] = {"type": "none"}
    for name, source in ((LOOKUP, keyless), (REVIEW, manifest), ("not-managed", manifest)):
        with pytest.raises(release.ManifestError, match="not a managed tool server with header credentials"):
            release.validate_refresh(source, [name])


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
               environ=ENVIRON)
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
        "policies": manifest["toolServers"][LOOKUP]["policies"],
        "endpoints": [{"kind": "mcp", "namespace": "lookup", "required": True,
                       "mcp": {"transport": "streamableHttp", "url": "https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup"},
                       "credentials": {"type": "header", "headers": {"Ocp-Apim-Subscription-Key": [APIM_KEY]}}}]}}
    knowledge = gateway.bodies[KNOWLEDGE]["properties"]["endpoints"][0]
    assert knowledge["credentials"] == {"type": "header", "headers": {"api-key": [SECRET]}}
    assert knowledge["mcp"]["url"].endswith("/knowledgebases/northwind-insurance-kb/mcp?api-version=2026-05-01-preview")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["complete"] is True and receipt["kind"] == "ai-gateway-release-receipt"
    assert receipt["planSha256"] == json.loads(plan_path.read_text(encoding="utf-8"))["planSha256"]
    assert [(row["name"], row["outcome"], row["converged"]) for row in receipt["results"]] == [
        (KNOWLEDGE, "applied", True), (LOOKUP, "applied", True)]
    assert gateway.methods() == {"GET", "PUT"}


def test_apply_updates_with_the_etag_and_reconciles_managed_policies(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP, etag="etag-live", displayName="Old name")
    gateway.add(KNOWLEDGE)
    gateway.servers[LOOKUP]["properties"]["policies"][0]["callsPerPeriod"] = 121
    plan_path = make_plan(gateway, tmp_path)
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"INSURANCE_AGENTS_KEY": APIM_KEY}) == 0
    (put,) = gateway.puts()
    assert put["headers"] == {"If-Match": "etag-live"}
    assert gateway.bodies[LOOKUP]["properties"]["policies"] == manifest["toolServers"][LOOKUP]["policies"]
    assert gateway.bodies[LOOKUP]["properties"]["displayName"] == "Northwind insurance lookup"


def test_apply_update_without_an_etag_requires_the_resource_to_exist(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.add(LOOKUP, displayName="Old name")
    gateway.add(KNOWLEDGE)
    del gateway.servers[LOOKUP]["etag"]
    plan_path = make_plan(gateway, tmp_path)
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ={"INSURANCE_AGENTS_KEY": APIM_KEY}) == 0
    assert gateway.puts()[0]["headers"] == {"If-Match": "*"}


def test_apply_refuses_when_the_live_gateway_changed_after_the_plan(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    gateway.add(LOOKUP)
    capsys.readouterr()
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ=ENVIRON) == 1
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
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ=ENVIRON) == 1
    assert not gateway.puts()


def test_apply_with_a_missing_secret_fails_clearly_before_any_write(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path)
    capsys.readouterr()
    cases = [
        ({}, "SEARCH_QUERY_KEY"),
        ({"SEARCH_QUERY_KEY": ""}, "SEARCH_QUERY_KEY"),
        ({"SEARCH_QUERY_KEY": SECRET}, "INSURANCE_AGENTS_KEY"),
        ({"SEARCH_QUERY_KEY": SECRET, "INSURANCE_AGENTS_KEY": ""}, "INSURANCE_AGENTS_KEY"),
    ]
    for environ, name in cases:
        assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ=environ) == 1
        assert name in capsys.readouterr().err
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
               environ=ENVIRON) == 1
    assert receipt_path.read_text(encoding="utf-8") == "keep"
    assert not gateway.calls


def test_apply_leaves_observed_stale_and_unlisted_servers_and_models_untouched(manifest, tmp_path):
    with_stale = copy.deepcopy(manifest)
    with_stale["stale"]["northwind-retired-example"] = {
        "reason": "Backend lived on a retired Container Apps domain.", "retiredHostFragment": "bluecliff",
        "decision": "Removed from the gateway; listed so the plan flags a re-registration of the retired host."}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(with_stale), encoding="utf-8")
    gateway = FakeGateway(manifest)
    for name in ("northwind-api", "northwind-claims-knowledge", "northwind-retired-example", "mystery-server"):
        item = copy.deepcopy(live_item(manifest, LOOKUP))
        item["name"] = name
        gateway.servers[name] = item
    before = copy.deepcopy({name: gateway.servers[name] for name in gateway.servers})
    models_before = copy.deepcopy(gateway.models)
    plan_path = make_plan(gateway, tmp_path, "--manifest", str(manifest_path))
    assert run(gateway, "apply", "--plan", str(plan_path), "--manifest", str(manifest_path), "--yes",
               environ=ENVIRON) == 0
    assert {call["url"].rsplit("/", 1)[-1] for call in gateway.puts()} == {LOOKUP, KNOWLEDGE}
    for name, item in before.items():
        assert gateway.servers[name] == item
    assert gateway.models == models_before
    assert gateway.methods() <= {"GET", "PUT"}


def test_a_changed_service_that_ignores_a_field_is_reported_as_not_converged(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.drop_fields = ("description",)
    plan_path = make_plan(gateway, tmp_path)
    receipt = release.apply_plan(gateway, manifest, json.loads(plan_path.read_text(encoding="utf-8")),
                                 ENVIRON, now=NOW)
    assert receipt["complete"] is True
    assert all(row["converged"] is False and row["remainingDifferences"] == ["description"]
               for row in receipt["results"])


def test_a_service_that_does_not_echo_failure_mode_still_converges(manifest, tmp_path):
    gateway = FakeGateway(manifest)
    gateway.drop_fields = ("failureMode",)
    plan_path = make_plan(gateway, tmp_path)
    receipt = release.apply_plan(gateway, manifest, json.loads(plan_path.read_text(encoding="utf-8")),
                                 ENVIRON, now=NOW)
    assert receipt["complete"] is True
    assert all(row["converged"] is True and row["remainingDifferences"] == [] for row in receipt["results"])
    assert all(body["properties"]["failureMode"] == "failClosed" for body in gateway.bodies.values())


def test_a_failed_write_stops_with_a_partial_receipt_and_scrubs_secrets(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    gateway.put_errors[KNOWLEDGE] = common.RunnerError(f"Azure request failed with {SECRET}", status=400)
    plan_path = make_plan(gateway, tmp_path)
    capsys.readouterr()
    receipt_path = tmp_path / "receipt.json"
    code = run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path),
               environ=ENVIRON)
    captured = capsys.readouterr()
    assert code == 1
    assert SECRET not in captured.out + captured.err + receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["complete"] is False
    assert [(row["name"], row["outcome"]) for row in receipt["results"]] == [(KNOWLEDGE, "failed")]
    assert "***" in receipt["results"][0]["error"]
    assert [call["url"].rsplit("/", 1)[-1] for call in gateway.puts()] == [KNOWLEDGE]
    assert "Nothing was retried or rolled back" in captured.err


def test_a_failed_subscription_key_write_scrubs_the_key_and_keeps_the_earlier_receipt_rows(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    gateway.put_errors[LOOKUP] = common.RunnerError(f"Azure request failed with {APIM_KEY}", status=400)
    plan_path = make_plan(gateway, tmp_path)
    capsys.readouterr()
    receipt_path = tmp_path / "receipt.json"
    code = run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path), environ=ENVIRON)
    captured = capsys.readouterr()
    assert code == 1
    assert APIM_KEY not in captured.out + captured.err + receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert [(row["name"], row["outcome"]) for row in receipt["results"]] == [
        (KNOWLEDGE, "applied"), (LOOKUP, "failed")]
    assert "***" in receipt["results"][1]["error"]
    assert [call["url"].rsplit("/", 1)[-1] for call in gateway.puts()] == [KNOWLEDGE, LOOKUP]


def test_secrets_never_reach_outputs_receipts_plans_or_argv(manifest, tmp_path, capsys):
    gateway = FakeGateway(manifest)
    plan_path = make_plan(gateway, tmp_path, "--refresh-secrets", KNOWLEDGE, LOOKUP)
    receipt_path = tmp_path / "receipt.json"
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", "--receipt", str(receipt_path),
               environ=ENVIRON) == 0
    captured = capsys.readouterr()
    for secret in (SECRET, APIM_KEY):
        for text in (captured.out, captured.err, plan_path.read_text(encoding="utf-8"),
                     receipt_path.read_text(encoding="utf-8")):
            assert secret not in text
        assert secret in json.dumps(gateway.bodies)
        assert secret not in json.dumps([{key: value for key, value in call.items() if key != "body"}
                                         for call in gateway.calls])
    assert APIM_KEY in json.dumps(gateway.bodies[LOOKUP])
    assert SECRET in json.dumps(gateway.bodies[KNOWLEDGE])


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
    assert run(gateway, "apply", "--plan", str(plan_path), "--yes", environ=ENVIRON) == 0
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
    gateway.mcp["northwind-api"] = {"tools": ["api_list_claims", "api_decide_approval", "api_delete_claim"],
                                    "mode": "json", "page_size": 100}
    gateway.mcp["northwind-contact-center-knowledge"] = {"tools": ["knowledge_knowledge_base_retrieve"], "mode": "sse",
                                                         "page_size": 100}
    code, result = verify(gateway, capsys, "--include-observed")
    observed = {row["name"]: row for row in result["observed"]}
    assert code == 0 and result["ok"] is True
    assert observed["northwind-api"]["governanceFindings"] == ["api_decide_approval", "api_delete_claim"]
    assert observed["northwind-api"]["toolCount"] == 3
    assert observed["northwind-contact-center-knowledge"]["reachable"] is True
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
