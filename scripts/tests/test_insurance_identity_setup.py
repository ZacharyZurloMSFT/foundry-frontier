import base64
import copy
import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("configure_insurance_identity", ROOT / "scripts" / "configure-insurance-identity.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
DEPLOYMENT = "4a734aae-a0fc-488c-b0b3-3f364a95813d"
PERSON = "dbe3b91b-cb1d-4421-8d7b-f06aad032380"
OTHER_PERSON = "db1ddbbc-f413-4fa7-9d91-69860ab6143e"
REDIRECT = "https://ca-web-tech.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/"
SECRET = "sensitive-fake-credential-never-print"


def new_id(label):
    return str(uuid.uuid5(uuid.UUID(DEPLOYMENT), label))


def opaque_id(label):
    raw = uuid.UUID(new_id(label)).bytes + uuid.UUID(new_id(label + "-opaque")).bytes
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class Credential:
    calls = []

    def __init__(self, **kwargs):
        self.calls.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_token(self, scope):
        self.calls.append(scope)
        return SimpleNamespace(token=SECRET)


class Directory:
    """An in-memory Graph transport; tests never acquire a real token or call Graph."""

    def __init__(self):
        self.apps = {}
        self.sps = {}
        self.grants = {}
        self.requests = []
        self.fail = None
        self.lose_app_response = False
        self.lose_sp_response = False
        self.lose_assignment_response = False
        self.hide_marker = False

    def __call__(self, request):
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        path = request.url.path.removeprefix("/v1.0")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body, dict(request.url.params)))
        if self.fail:
            failure = self.fail(request.method, path, body)
            if failure:
                return httpx.Response(failure, json={"error": {"message": SECRET}})
        if path == "/applications":
            if request.method == "GET":
                expression = request.url.params["$filter"]
                if expression.startswith("appId eq "):
                    values = [app for app in self.apps.values() if app["appId"] == expression.split("'")[1]]
                else:
                    marker = expression.split("'")[1]
                    values = [] if self.hide_marker else [app for app in self.apps.values() if marker in app["tags"]]
                return httpx.Response(200, json={"value": copy.deepcopy(values)})
            assert request.method == "POST"
            index = len(self.apps)
            app = copy.deepcopy(body)
            app.update(id=new_id(f"app-object-{index}"), appId=new_id(f"app-client-{index}"))
            app.setdefault("passwordCredentials", [])
            app.setdefault("keyCredentials", [])
            self.apps[app["id"]] = app
            if self.lose_app_response:
                self.lose_app_response = False
                raise httpx.ReadTimeout(SECRET, request=request)
            return httpx.Response(201, json=copy.deepcopy(app))
        if path.startswith("/applications/"):
            object_id = path.rsplit("/", 1)[1]
            if object_id not in self.apps:
                return httpx.Response(404, json={"error": SECRET})
            if request.method == "GET":
                return httpx.Response(200, json=copy.deepcopy(self.apps[object_id]))
            assert request.method == "PATCH"
            self.apps[object_id].update(copy.deepcopy(body))
            return httpx.Response(204)
        if path == "/servicePrincipals":
            if request.method == "GET":
                client_id = request.url.params["$filter"].split("'")[1]
                return httpx.Response(200, json={"value": [copy.deepcopy(sp) for sp in self.sps.values() if sp["appId"] == client_id]})
            assert request.method == "POST"
            sp = copy.deepcopy(body)
            sp.update(
                id=new_id(f"sp-{len(self.sps)}"), appOwnerOrganizationId=TENANT,
                servicePrincipalType="Application",
            )
            self.sps[sp["id"]] = sp
            self.grants[sp["id"]] = []
            if self.lose_sp_response:
                self.lose_sp_response = False
                raise httpx.ReadTimeout(SECRET, request=request)
            return httpx.Response(201, json=copy.deepcopy(sp))
        if path.startswith("/servicePrincipals/"):
            parts = path.split("/")
            object_id = parts[2]
            if len(parts) == 4:
                assert parts[3] == "appRoleAssignedTo"
                if request.method == "GET":
                    return httpx.Response(200, json={"value": copy.deepcopy(self.grants[object_id])})
                assert request.method == "POST"
                grant = copy.deepcopy(body)
                grant.update(id=opaque_id(f"assignment-{object_id}-{len(self.grants[object_id])}"), principalType="User")
                self.grants[object_id].append(grant)
                if self.lose_assignment_response:
                    self.lose_assignment_response = False
                    raise httpx.ReadTimeout(SECRET, request=request)
                return httpx.Response(201, json=copy.deepcopy(grant))
            if request.method == "PATCH":
                self.sps[object_id].update(body)
                return httpx.Response(204)
            return httpx.Response(200, json=copy.deepcopy(self.sps[object_id]))
        if path.startswith("/users/"):
            assert request.method == "GET"
            return httpx.Response(200, json={"id": path.split("/")[-1], "accountEnabled": True, "userType": "Member"})
        pytest.fail(f"Unexpected Graph path: {request.method} {path}")

    @property
    def writes(self):
        return [request for request in self.requests if request[0] != "GET"]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    # pytest is invoked with --basetemp under the repository, never the OS temp directory.
    monkeypatch.setattr(module, "ROOT", tmp_path / "repository")
    monkeypatch.setattr(module, "AzureCliCredential", Credential)
    Credential.calls = []
    actors = tmp_path / "actors.json"
    actors.write_text(json.dumps({f"{TENANT}:{PERSON}": {"role": "policyholder", "policyholder_id": "PH-1001"}}), encoding="utf-8")
    config = module.Configuration(
        tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT,
        run_id="reviewed-run", actors_json=actors,
    )
    return SimpleNamespace(config=config, actors=actors, path=tmp_path / "journal.json", graph=Directory(), tmp=tmp_path)


def apply_setup(configured, *, assign=False, config=None):
    config = config or configured.config
    journal = module.Journal(configured.path, config)
    result = module.apply(config, journal, assign_participants=assign, transport=httpx.MockTransport(configured.graph))
    return result, json.loads(configured.path.read_text(encoding="utf-8"))


def args(configured):
    return [
        "--tenant-id", TENANT, "--deployment-id", DEPLOYMENT, "--redirect-uri", REDIRECT,
        "--run-id", "reviewed-run", "--actors-json", str(configured.actors), "--journal", str(configured.path),
    ]


@pytest.mark.parametrize("flag", [[], ["--preview"], ["--assign-participants"]])
def test_preview_is_fully_offline_and_does_not_write_or_disclose_people(configured, monkeypatch, capsys, flag):
    monkeypatch.setattr(module, "AzureCliCredential", lambda **kwargs: pytest.fail("Preview authenticated"))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: pytest.fail("Preview made remote calls"))
    assert module.main(args(configured) + flag) == 0
    text = capsys.readouterr().out
    output = json.loads(text)
    assert output["mode"] == "preview"
    assert output["names"]["api"] == f"Northwind Insurance Business API [{DEPLOYMENT}]"
    assert output["redirect_uri"] == REDIRECT
    assert output["participant_mapping_counts"] == {"total": 1, "by_role": {"policyholder": 1}}
    assert not output["ready_for_participants"]
    assert output["configuration_to_review_not_applied"]["frontend"]["VITE_INSURANCE_BUSINESS_ENABLED"] == "false"
    assert output["configuration_to_review_not_applied"]["api"]["NORTHWIND_BUSINESS_API_ENABLED"] == "false"
    permissions = output["operator_permissions_to_review"]
    assert permissions["credential"] == "AzureCliCredential"
    assert permissions["credential_scope"] == "https://graph.microsoft.com/.default"
    assert permissions["registration_delegated_permissions"] == ["Application.ReadWrite.All"]
    assert permissions["assign_participants_additional_delegated_permissions"] == ["AppRoleAssignment.ReadWrite.All", "User.Read.All"]
    assert permissions["permission_or_role_changes_performed"] is False
    assert PERSON not in text and "PH-1001" not in text and SECRET not in text
    assert not configured.path.exists()
    assert not configured.path.with_suffix(".json.lock").exists()


@pytest.mark.parametrize("url", [
    "http://portal.example.com", "https://portal.example.com/callback", "https://portal.example.com/?x=y",
    "https://portal.example.com/#", "https://portal.example.com?", "https://user:password@portal.example.com",
    "https://localhost:3000/", "http://127.0.0.1:3000", "https://portal.example.com\\evil",
    "https://portal.example.com/%2f", "https://portal.example.com:bad", "https://portal.example.com/\n",
    "//portal.example.com", "https://portal..example.com", "https://portal.-example.com",
    "https://192.0.2.1", "https://portal.example-.com",
])
def test_bad_redirects_fail_offline(configured, url):
    with pytest.raises(module.SetupError, match="approved HTTPS origin"):
        module.redirect_uri(url, False)


def test_localhost_requires_explicit_development_and_keeps_exact_origin(configured):
    assert module.redirect_uri("http://localhost:5173/", True) == "http://localhost:5173/"
    assert module.redirect_uri("http://[::1]:5173", True) == "http://[::1]:5173"
    assert module.redirect_uri(REDIRECT, False) == REDIRECT
    with pytest.raises(module.SetupError):
        module.redirect_uri("http://external.example.com", True)


@pytest.mark.parametrize("changes", [
    {"tenant_id": "common"}, {"tenant_id": module.ZERO},
    {"deployment_id": "x' or true"}, {"api_app_id": "not-a-guid"},
    {"api_app_id": PERSON, "spa_app_id": PERSON}, {"run_id": "with.dot"},
    {"run_id": ""}, {"run_id": "a" * 65},
])
def test_invalid_explicit_configuration(configured, changes):
    values = dict(
        tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT,
        run_id="reviewed-run", actors_json=configured.actors,
    )
    values.update(changes)
    with pytest.raises(module.SetupError):
        module.Configuration(**values)
    assert Credential.calls == []


@pytest.mark.parametrize("mapping", [
    {}, {PERSON: {"role": "adjuster"}},
    {f"{TENANT}:{PERSON}": {"role": "admin"}},
    {f"{TENANT}:{PERSON}": {"role": "policyholder"}},
    {f"{TENANT}:{PERSON}": {"role": "policyholder", "policyholder_id": "PH-9999"}},
    {f"{TENANT}:{PERSON}": {"role": "adjuster", "policyholder_id": "PH-1001"}},
    {f"{TENANT}:{PERSON}": {"role": "presenter", "email": "not-allowed"}},
    {f"{TENANT.upper()}:{PERSON}": {"role": "adjuster"}},
    {f"{DEPLOYMENT}:{PERSON}": {"role": "adjuster"}},
    {f"{TENANT}:{uuid.UUID(int=i + 1)}": {"role": "adjuster"} for i in range(101)},
])
def test_invalid_maps_use_existing_seed_contract_and_hide_details(configured, mapping):
    configured.actors.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(module.SetupError, match="Invalid participant map") as error:
        module.Configuration(tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT, run_id="x", actors_json=configured.actors)
    assert PERSON not in str(error.value) and "PH-9999" not in str(error.value)
    assert not configured.path.exists()


def test_duplicate_participant_json_keys_are_rejected(configured):
    key = f"{TENANT}:{PERSON}"
    configured.actors.write_text('{"' + key + '":{"role":"adjuster"},"' + key + '":{"role":"presenter"}}', encoding="utf-8")
    with pytest.raises(module.SetupError, match="Invalid participant map"):
        module.Configuration(tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT, run_id="x", actors_json=configured.actors)


def test_apply_without_external_journal_fails_before_authentication(configured, capsys):
    without_journal = args(configured)[:-2]
    assert module.main(without_journal + ["--apply"]) == 1
    assert "--journal" in capsys.readouterr().out
    assert Credential.calls == []
    with pytest.raises(module.SetupError, match="outside the repository"):
        module.Journal(module.ROOT / "journal.json", configured.config)
    with pytest.raises(module.SetupError, match=".json"):
        module.Journal(configured.tmp / "journal.txt", configured.config)


def test_apply_creates_precise_owned_applications_and_assignment_controls(configured):
    result, journal = apply_setup(configured)
    apps = {("api" if "API [" in app["displayName"] else "spa"): app for app in configured.graph.apps.values()}
    api, spa = apps["api"], apps["spa"]
    assert api["appId"] != spa["appId"]
    assert api["api"]["requestedAccessTokenVersion"] == 2
    assert api["identifierUris"] == [f"api://{api['appId']}"]
    assert api["api"]["oauth2PermissionScopes"] == [module.scope_definition(configured.config)]
    assert api["api"]["preAuthorizedApplications"] == [{"appId": spa["appId"], "delegatedPermissionIds": [configured.config.scope_id]}]
    assert spa["requiredResourceAccess"] == [{"resourceAppId": api["appId"], "resourceAccess": [{"id": configured.config.scope_id, "type": "Scope"}]}]
    assert spa["spa"]["redirectUris"] == [REDIRECT]
    for app in apps.values():
        assert app["signInAudience"] == "AzureADMyOrg"
        assert app["web"] == {"redirectUris": [], "implicitGrantSettings": {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": False}}
        assert app["publicClient"] == {"redirectUris": []}
        assert app["isFallbackPublicClient"] is False
        assert app["passwordCredentials"] == app["keyCredentials"] == app["appRoles"] == []
    assert all(sp["appRoleAssignmentRequired"] for sp in configured.graph.sps.values())
    assert len(journal["resources"]) == 4
    assert journal["source_configuration_hash"] == configured.config.source_hash
    assert journal["scope_id"] == configured.config.scope_id
    assert result["participant_assignments"] == {"state": "pending_operator_assignment", "pending_assignments": 2}
    assert result["status"] == "registrations_verified_access_pending"
    assert Credential.calls[0] == {"tenant_id": TENANT, "process_timeout": 30}
    assert all(call == module.GRAPH_SCOPE for call in Credential.calls[1:])
    written = json.dumps(configured.graph.writes)
    assert "oauth2PermissionGrants" not in written and "passwordCredentials" not in written
    assert PERSON not in json.dumps(result) and SECRET not in json.dumps(journal)


def test_rerun_does_not_mutate_graph_or_duplicate_apps(configured):
    first, _ = apply_setup(configured)
    configured.graph.requests.clear()
    second, _ = apply_setup(configured)
    assert configured.graph.writes == []
    assert first["confirmed_resources"] == second["confirmed_resources"]
    assert len(configured.graph.apps) == len(configured.graph.sps) == 2


def test_every_successful_create_is_checkpointed_before_the_next_write(configured):
    snapshots = []
    def inspect(method, path, body):
        if method != "GET":
            snapshots.append((path, json.loads(configured.path.read_text(encoding="utf-8"))))
    configured.graph.fail = inspect
    apply_setup(configured)
    assert snapshots[0][1]["operations"]["create_api"]["state"] == "in_flight"
    assert snapshots[1][1]["resources"]["api"]["app_id"]
    assert snapshots[1][1]["operations"]["create_api"]["state"] == "confirmed"
    assert snapshots[-1][1]["resources"]["api_sp"]["object_id"]


def test_preauthorization_is_not_patched_before_both_assignment_controls_are_verified(configured):
    def inspect(method, path, body):
        if method == "PATCH" and path.startswith("/applications/"):
            assert len(configured.graph.sps) == 2
            assert all(sp["appRoleAssignmentRequired"] is True for sp in configured.graph.sps.values())
    configured.graph.fail = inspect
    apply_setup(configured)


def test_journal_configuration_conflict_aborts_without_calls(configured):
    apply_setup(configured)
    configured.graph.requests.clear()
    other = module.Configuration(
        tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT,
        run_id="different-run", actors_json=configured.actors,
    )
    with pytest.raises(module.SetupError, match="Journal configuration"):
        module.Journal(configured.path, other)
    assert configured.graph.requests == []


def test_approved_existing_registration_requires_owned_marker_not_just_name(configured):
    app = module.application_body(configured.config, "api")
    app.update(id=new_id("foreign-object"), appId=new_id("foreign-client"), tags=[])
    configured.graph.apps[app["id"]] = app
    config = module.Configuration(
        tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT, run_id="reviewed-run",
        actors_json=configured.actors, api_app_id=app["appId"],
    )
    with pytest.raises(module.SetupError, match="ownership"):
        apply_setup(configured, config=config)
    assert configured.graph.writes == []


def test_same_display_name_without_marker_is_never_adopted(configured):
    app = module.application_body(configured.config, "api")
    app.update(id=new_id("foreign-object"), appId=new_id("foreign-client"), tags=[])
    configured.graph.apps[app["id"]] = app
    result, _ = apply_setup(configured)
    assert result["audience"] != app["appId"]
    assert configured.graph.apps[app["id"]] == app


def test_duplicate_owned_markers_and_explicit_id_conflicts_fail_closed(configured):
    apply_setup(configured)
    owned_api = next(app for app in configured.graph.apps.values() if "API [" in app["displayName"])
    duplicate = copy.deepcopy(owned_api)
    duplicate.update(id=new_id("duplicate-object"), appId=new_id("duplicate-client"))
    configured.graph.apps[duplicate["id"]] = duplicate
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="Multiple applications"):
        apply_setup(configured)
    assert configured.graph.writes == []
    config = module.Configuration(
        tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT, run_id="reviewed-run",
        actors_json=configured.actors, api_app_id=OTHER_PERSON,
    )
    with pytest.raises(module.SetupError, match="conflict"):
        module.Journal(configured.path, config)


@pytest.mark.parametrize("conflict", ["secret", "scope", "redirect", "required_permission", "preauthorized_client"])
def test_owned_application_foreign_settings_are_not_overwritten(configured, conflict):
    apply_setup(configured)
    api = next(app for app in configured.graph.apps.values() if "API [" in app["displayName"])
    if conflict == "secret":
        api["passwordCredentials"] = [{"keyId": new_id("secret")}]
    elif conflict == "scope":
        api["api"]["oauth2PermissionScopes"].append({"id": new_id("foreign-scope"), "value": "unrelated"})
    elif conflict == "redirect":
        api["spa"]["redirectUris"] = ["https://unrelated.example.com/"]
    elif conflict == "required_permission":
        api["requiredResourceAccess"] = [{"resourceAppId": PERSON, "resourceAccess": []}]
    else:
        api["api"]["preAuthorizedApplications"].append({"appId": OTHER_PERSON, "delegatedPermissionIds": []})
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="unrelated settings"):
        apply_setup(configured)
    assert configured.graph.writes == []


@pytest.mark.parametrize("kind", ["app", "sp"])
def test_lost_create_response_query_reconciles_without_duplicate_post(configured, kind):
    setattr(configured.graph, f"lose_{kind}_response", True)
    with pytest.raises(module.GraphError, match="did not confirm") as error:
        apply_setup(configured)
    assert SECRET not in str(error.value)
    journal = json.loads(configured.path.read_text(encoding="utf-8"))
    operation = "create_api" if kind == "app" else "create_api_sp"
    assert journal["operations"][operation]["state"] == "uncertain"
    result, journal = apply_setup(configured)
    assert journal["operations"][operation]["state"] == "confirmed"
    assert len(configured.graph.apps) == len(configured.graph.sps) == 2
    assert result["status"] != "complete"
    assert len([r for r in configured.graph.writes if r[0] == "POST" and r[1] == "/applications"]) == 2
    assert len([r for r in configured.graph.writes if r[0] == "POST" and r[1] == "/servicePrincipals"]) == 2


def test_uncertain_create_with_empty_eventual_query_is_never_resubmitted(configured):
    configured.graph.lose_app_response = True
    with pytest.raises(module.GraphError):
        apply_setup(configured)
    configured.graph.hide_marker = True
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="never delete the journal"):
        apply_setup(configured)
    assert configured.graph.writes == []
    assert len(configured.graph.apps) == 1


def test_explicit_approved_ids_can_reconcile_owned_existing_resources(configured):
    first, _ = apply_setup(configured)
    config = module.Configuration(
        tenant_id=TENANT, deployment_id=DEPLOYMENT, redirect=REDIRECT, run_id="reviewed-run",
        actors_json=configured.actors, api_app_id=first["audience"], spa_app_id=first["allowed_spa_client_id"],
    )
    configured.graph.requests.clear()
    second, _ = apply_setup(configured, config=config)
    assert second["confirmed_resources"] == first["confirmed_resources"]
    assert configured.graph.writes == []


def test_confirmed_ids_are_immutable_even_if_graph_returns_same_marker(configured):
    apply_setup(configured)
    journal = module.Journal(configured.path, configured.config)
    with pytest.raises(module.SetupError, match="conflict"):
        journal.confirm_resource("api", {"id": OTHER_PERSON, "appId": PERSON})
    with pytest.raises(module.SetupError, match="conflict"):
        journal.confirm_resource("api_sp", {
            "id": journal.resources["api"]["object_id"], "appId": journal.resources["api"]["app_id"],
        })


def test_interrupted_before_post_blocks_blind_create_until_query_reconciliation(configured):
    journal = module.Journal(configured.path, configured.config)
    journal.record("create_api", "in_flight")
    with pytest.raises(module.SetupError, match="unresolved"):
        apply_setup(configured)
    assert configured.graph.writes == []


def test_assignment_is_explicit_allowlist_only_not_an_insurance_role_grant(configured):
    result, journal = apply_setup(configured, assign=True)
    assert result["participant_assignments"]["state"] == "verified"
    assert result["ready_for_participants"] is False
    for sp_id, grants in configured.graph.grants.items():
        assert len(grants) == 1
        assert {key: grants[0][key] for key in ("principalId", "resourceId", "appRoleId")} == {
            "principalId": PERSON, "resourceId": sp_id, "appRoleId": module.ZERO,
        }
    confirmed = {operation: state["object_id"] for operation, state in journal["operations"].items() if operation.startswith("assign_")}
    assert set(confirmed.values()) == {grants[0]["id"] for grants in configured.graph.grants.values()}
    assert PERSON not in json.dumps(journal) and PERSON not in json.dumps(result)
    configured.graph.requests.clear()
    _, rerun = apply_setup(configured, assign=True)
    assert configured.graph.writes == []
    assert confirmed == {operation: state["object_id"] for operation, state in rerun["operations"].items() if operation.startswith("assign_")}


def test_lost_assignment_response_is_reconciled_to_actual_id_without_duplicate_grant(configured):
    configured.graph.lose_assignment_response = True
    with pytest.raises(module.GraphError):
        apply_setup(configured, assign=True)
    journal = json.loads(configured.path.read_text(encoding="utf-8"))
    operation = f"assign_api_{module.digest(PERSON)}"
    assert journal["operations"][operation]["state"] == "uncertain"
    result, journal = apply_setup(configured, assign=True)
    assert result["participant_assignments"]["state"] == "verified"
    assert journal["operations"][operation]["state"] == "confirmed"
    api_sp_id = journal["resources"]["api_sp"]["object_id"]
    actual_id = configured.graph.grants[api_sp_id][0]["id"]
    assert journal["operations"][operation]["object_id"] == actual_id
    assert module.opaque_assignment_id(actual_id) == actual_id
    with pytest.raises(module.SetupError):
        module.guid(actual_id)
    assert all(len(grants) == 1 for grants in configured.graph.grants.values())
    configured.graph.requests.clear()
    _, rerun = apply_setup(configured, assign=True)
    assert rerun["operations"][operation]["object_id"] == actual_id
    assert configured.graph.writes == []


@pytest.mark.parametrize("value", [
    "Zjc8B8bkfUuZyKlqTtqiXsDX6sEosL1EqypJZB6-HbQ",
    "_bcD-efg_hIJklmnOP_qrstUvWxyz0123456789-ABCDEFG",
    "Zjc8B8bkfUuZyKlqTtqiXsDX6sEosL1EqypJZB6-HbQ=",
    "YWI=", "YQ==", PERSON, PERSON.upper(), "A", "a" * 512,
])
def test_assignment_identifier_is_opaque_and_preserves_exact_bytes(value):
    assert module.opaque_assignment_id(value) == value


@pytest.mark.parametrize("value", [
    None, 7, [], {}, "", " ", ".", "..", "https://graph.microsoft.com/id",
    "abc/def", "abc\\def", "abc?query", "abc#fragment", "abc%2Fdef",
    "abc\n", "abc\x00", "abc=def", "abc===", "=", "é", "a" * 513,
])
def test_assignment_identifier_rejects_unsafe_or_unbounded_values(configured, value):
    journal = module.Journal(configured.path, configured.config)
    with pytest.raises(module.SetupError, match="bounded opaque identifiers"):
        journal.confirm_assignment("assign_api_test", value)
    assert not configured.path.exists()


def test_existing_opaque_membership_is_reconciled_without_creates_and_journal_ids_stay_exact(configured):
    _, journal = apply_setup(configured)
    expected = {}
    for kind in ("api", "spa"):
        sp_id = journal["resources"][kind + "_sp"]["object_id"]
        assignment_id = opaque_id("existing-" + kind)
        configured.graph.grants[sp_id].append({
            "id": assignment_id, "principalId": PERSON, "principalType": "User",
            "resourceId": sp_id, "appRoleId": module.ZERO,
        })
        expected[f"assign_{kind}_{module.digest(PERSON)}"] = assignment_id
    configured.graph.requests.clear()
    result, reconciled = apply_setup(configured)
    assert result["participant_assignments"] == {"state": "verified", "pending_assignments": 0}
    assert configured.graph.writes == []
    assert expected == {operation: reconciled["operations"][operation]["object_id"] for operation in expected}
    configured.graph.requests.clear()
    _, rerun = apply_setup(configured, assign=True)
    assert configured.graph.writes == []
    assert expected == {operation: rerun["operations"][operation]["object_id"] for operation in expected}


@pytest.mark.parametrize("field", ["principalId", "resourceId", "appRoleId"])
def test_assignment_guid_fields_do_not_accept_opaque_assignment_identifiers(configured, field):
    _, journal = apply_setup(configured)
    sp_id = journal["resources"]["api_sp"]["object_id"]
    row = {
        "id": opaque_id("existing-assignment"), "principalId": PERSON, "principalType": "User",
        "resourceId": sp_id, "appRoleId": module.ZERO,
    }
    row[field] = opaque_id("not-a-guid")
    configured.graph.grants[sp_id].append(row)
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="explicit participant allowlist"):
        apply_setup(configured, assign=True)
    assert configured.graph.writes == []


def test_uncertain_assignment_missing_from_query_is_not_reposted(configured):
    configured.graph.lose_assignment_response = True
    with pytest.raises(module.GraphError):
        apply_setup(configured, assign=True)
    for grants in configured.graph.grants.values():
        grants.clear()
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="prior participant assignment is unresolved"):
        apply_setup(configured, assign=True)
    assert configured.graph.writes == []


def test_assignment_required_is_repaired_never_relaxed(configured):
    apply_setup(configured)
    for sp in configured.graph.sps.values():
        sp["appRoleAssignmentRequired"] = False
    configured.graph.requests.clear()
    apply_setup(configured)
    assert [r[2] for r in configured.graph.writes] == [{"appRoleAssignmentRequired": True}, {"appRoleAssignmentRequired": True}]


def test_foreign_or_group_application_assignment_blocks_new_grants(configured):
    apply_setup(configured)
    sp_id = next(iter(configured.graph.sps))
    configured.graph.grants[sp_id].append({
        "id": new_id("unapproved-assignment"), "principalId": OTHER_PERSON,
        "principalType": "Group", "appRoleId": module.ZERO, "resourceId": sp_id,
    })
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="exceed the explicit participant allowlist"):
        apply_setup(configured, assign=True)
    assert configured.graph.writes == []


def test_assignment_permission_denial_stays_pending_without_disabling_controls(configured):
    configured.graph.fail = lambda method, path, body: 403 if path.endswith("/appRoleAssignedTo") else None
    result, journal = apply_setup(configured, assign=True)
    assert result["participant_assignments"]["state"] == "pending_permissions"
    assert result["ready_for_participants"] is False
    assert journal["operations"]["participant_assignments"]["state"] == "pending_permissions"
    assert all(sp["appRoleAssignmentRequired"] for sp in configured.graph.sps.values())
    assert all(not grants for grants in configured.graph.grants.values())
    assert SECRET not in json.dumps(result)


def test_authorization_failure_preserves_confirmed_ids_and_has_no_false_completion(configured, monkeypatch, capsys):
    configured.graph.fail = lambda method, path, body: 403 if method == "POST" and path == "/servicePrincipals" else None
    real_apply = module.apply
    monkeypatch.setattr(module, "apply", lambda config, journal, **kwargs: real_apply(
        config, journal, transport=httpx.MockTransport(configured.graph), **kwargs,
    ))
    assert module.main(args(configured) + ["--apply"]) == 1
    text = capsys.readouterr().out
    assert json.loads(text)["status"] == "not_confirmed"
    assert "authorization denied" in text and SECRET not in text
    journal = json.loads(configured.path.read_text(encoding="utf-8"))
    assert set(journal["resources"]) == {"api", "spa"}
    assert journal["operations"]["create_api_sp"]["state"] == "denied"
    assert not configured.path.with_suffix(".json.lock").exists()


def test_cli_authentication_failure_has_no_fallback_or_provider_output(configured):
    class FailedCredential:
        def get_token(self, *args):
            raise module.ClientAuthenticationError(message=SECRET)
    with httpx.Client(transport=httpx.MockTransport(configured.graph)) as client:
        with pytest.raises(module.SetupError, match="No fallback or login") as error:
            module.Graph(client, FailedCredential()).request("GET", "/applications")
    assert SECRET not in str(error.value)
    assert configured.graph.requests == []


def test_auth_failure_before_mutation_is_recorded_not_sent_and_can_be_retried(configured):
    class FailedCredential:
        def get_token(self, *args):
            raise module.CredentialUnavailableError(message=SECRET)
    journal = module.Journal(configured.path, configured.config)
    with httpx.Client(transport=httpx.MockTransport(configured.graph)) as client:
        with pytest.raises(module.AuthenticationError):
            module.mutate(module.Graph(client, FailedCredential()), journal, "create_api", "POST", "/applications", {})
    assert json.loads(configured.path.read_text())["operations"]["create_api"]["state"] == "not_sent"
    assert configured.graph.requests == []
    result, _ = apply_setup(configured)
    assert result["status"] == "registrations_verified_access_pending"


def test_server_error_is_uncertain_and_not_reposted_on_empty_query(configured):
    configured.graph.fail = lambda method, path, body: 503 if method == "POST" else None
    with pytest.raises(module.GraphError):
        apply_setup(configured)
    journal = json.loads(configured.path.read_text(encoding="utf-8"))
    assert journal["operations"]["create_api"]["state"] == "uncertain"
    configured.graph.fail = None
    configured.graph.requests.clear()
    with pytest.raises(module.SetupError, match="unresolved"):
        apply_setup(configured)
    assert configured.graph.writes == []


def test_marker_filter_is_escaped_and_never_display_name_based(configured):
    assert module.quoted("owner's-marker") == "'owner''s-marker'"
    apply_setup(configured)
    queries = [r[3]["$filter"] for r in configured.graph.requests if r[0] == "GET" and r[1] == "/applications"]
    assert all("tags/any" in query and "displayName" not in query for query in queries)
    assert all(DEPLOYMENT in query for query in queries)


def test_graph_pagination_cannot_follow_off_host_or_unbounded_resource(configured):
    def reply(request):
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example.com/v1.0/applications?token=" + SECRET})
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(module.SetupError, match="bounded resource path") as error:
            module.Graph(client, Credential()).collection("/applications")
    assert SECRET not in str(error.value)


def test_lock_prevents_parallel_mutations_and_is_not_removed_by_another_operator(configured):
    lock = configured.path.with_suffix(".json.lock")
    lock.write_text("", encoding="utf-8")
    with pytest.raises(module.SetupError, match="locked"):
        apply_setup(configured)
    assert configured.graph.requests == []
    assert lock.exists()
