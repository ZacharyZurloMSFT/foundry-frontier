"""Offline identity preview; only --apply reconciles owned Entra API/SPA registrations.

An existing Azure CLI sign-in in the explicit tenant is required for apply. No login,
admin consent, invitation, storage seed, deployment, or feature enablement is performed.
Keep the external JSON journal: uncertain creates are reconciled, never blindly retried.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential, CredentialUnavailableError

ROOT = Path(__file__).resolve().parents[1]
SEED_SCRIPT = ROOT / "scripts" / "seed-insurance-run.py"
REPOSITORY = "https://github.com/ZacharyZurloMSFT/foundry-frontier"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
SCOPE = "access_as_user"
ZERO = "00000000-0000-0000-0000-000000000000"
APP_SELECT = (
    "id,appId,displayName,tags,notes,signInAudience,identifierUris,api,spa,web,"
    "publicClient,isFallbackPublicClient,requiredResourceAccess,passwordCredentials,keyCredentials,appRoles"
)
SP_SELECT = "id,appId,appOwnerOrganizationId,servicePrincipalType,appRoleAssignmentRequired"


class SetupError(ValueError):
    """Only operator-safe text belongs in this exception."""


class AuthenticationError(SetupError):
    """Authentication failed before the Graph request was sent."""


class GraphError(SetupError):
    def __init__(self, status: int | None = None, *, uncertain: bool = False):
        self.status = status
        self.uncertain = uncertain
        if status in (401, 403):
            message = (
                "Graph authorization denied. Review the signed-in tenant and delegated Graph permissions "
                "with an administrator; no login, consent, or permission escalation was attempted."
            )
        elif uncertain:
            message = (
                "Graph did not confirm the operation. Preserve the journal and rerun to query-reconcile; "
                "an uncertain create will not be resubmitted."
            )
        else:
            message = "Graph did not confirm the request. Review configuration and permissions; provider details are withheld."
        super().__init__(message)


def guid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
        if parsed.int == 0:
            raise ValueError
        return str(parsed)
    except (ValueError, AttributeError, TypeError):
        raise SetupError("Supply nonzero UUIDs for tenant, deployment, and application IDs.") from None


def opaque_assignment_id(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise SetupError("Graph assignment IDs must be bounded opaque identifiers without URL, path, or control characters.")
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("Duplicate JSON keys are not allowed.")
        result[key] = value
    return result


@lru_cache(maxsize=1)
def seed_helper():
    spec = importlib.util.spec_from_file_location("_insurance_identity_seed_contract", SEED_SCRIPT)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def redirect_uri(value: str, development: bool) -> str:
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        local = host in ("localhost", "127.0.0.1", "::1")
        numeric_host = False
        if host:
            try:
                ipaddress.ip_address(host)
                numeric_host = True
            except ValueError:
                pass
        if (
            not isinstance(value, str) or not value.isascii()
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(c in value for c in ("\\", "%", "?", "#"))
            or not host or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment or not (port is None or 1 <= port <= 65535)
            or (local and not development)
            or (parts.scheme != "https" and not (development and local and parts.scheme == "http"))
            or (not local and (numeric_host or "." not in host or len(host) > 253))
            or (not local and any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")))
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SetupError("Use an approved HTTPS origin with no path, credentials, query, or fragment; localhost requires --development.") from None
    return value


def journal_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(ROOT.resolve()) or resolved.suffix.lower() != ".json":
        raise SetupError("The journal must be a .json file outside the repository; never commit it.")
    if not resolved.parent.is_dir() or (resolved.exists() and not resolved.is_file()):
        raise SetupError("The journal requires an existing external directory and a regular JSON file.")
    return resolved


class Configuration:
    def __init__(
        self, *, tenant_id: str, deployment_id: str, redirect: str, run_id: str,
        actors_json: Path, development: bool = False, api_app_id: str | None = None,
        spa_app_id: str | None = None,
    ):
        self.tenant_id = guid(tenant_id)
        self.deployment_id = guid(deployment_id)
        self.redirect = redirect_uri(redirect, development)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise SetupError("The explicit run ID must contain 1-64 letters, digits, underscores, or hyphens.")
        self.run_id = run_id
        self.development = development
        self.approved_ids = {"api": guid(api_app_id) if api_app_id else None, "spa": guid(spa_app_id) if spa_app_id else None}
        if self.approved_ids["api"] and self.approved_ids["api"] == self.approved_ids["spa"]:
            raise SetupError("The API and SPA must be separate application registrations.")
        try:
            if actors_json.stat().st_size > 128 * 1024:
                raise ValueError
            json.loads(actors_json.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            helper = seed_helper()
            self.actors = helper.actor_mappings(actors_json)
            from app.insurance_domain import fixtures

            customers = {p["policyholder_id"] for p in fixtures()["policies"]}
            if not self.actors or any(
                subject.split(":")[0] != self.tenant_id
                or guid(subject.split(":")[1]) != subject.split(":")[1]
                or (actor.role == "policyholder" and actor.policyholder_id not in customers)
                for subject, actor in self.actors.items()
            ):
                raise ValueError
        except (ValueError, OSError, KeyError, IndexError):
            raise SetupError(
                "Invalid participant map. Supply 1-100 canonical target-tenant:object-UUID subjects with "
                "policyholder (existing synthetic policyholder_id), adjuster, or presenter mappings only."
            ) from None
        self.counts = dict(sorted(Counter(actor.role for actor in self.actors.values()).items()))
        self.scope_id = str(uuid.uuid5(uuid.UUID(self.deployment_id), "northwind-insurance/access_as_user"))
        self.source = {
            "tenant_id": self.tenant_id, "deployment_id": self.deployment_id, "redirect_uri": self.redirect,
            "run_id": self.run_id, "development": self.development, "repository": REPOSITORY,
            "participant_mapping_hash": digest({
                subject: actor.model_dump(mode="json") for subject, actor in self.actors.items()
            }),
            "participant_counts": self.counts,
        }
        self.source_hash = digest(self.source)

    def marker(self, kind: str) -> str:
        return f"northwind-insurance-identity:v1:{self.deployment_id}:{kind}"

    def name(self, kind: str) -> str:
        name = "API" if kind == "api" else "Portal"
        return f"Northwind Insurance Business {name} [{self.deployment_id}]"

    def notes(self, kind: str) -> str:
        return f"Owned by {REPOSITORY}; {self.marker(kind)}; synthetic insurance only."


def scope_definition(config: Configuration) -> dict:
    return {
        "id": config.scope_id, "value": SCOPE, "type": "User", "isEnabled": True,
        "adminConsentDisplayName": "Access the synthetic Northwind insurance demo",
        "adminConsentDescription": "Access only the signed-in participant's operator-approved synthetic insurance demo run.",
        "userConsentDisplayName": "Access your synthetic Northwind insurance demo",
        "userConsentDescription": "Access your operator-approved synthetic insurance demo run. No real insurance records.",
    }


def application_body(config: Configuration, kind: str, *, api_id: str | None = None, spa_id: str | None = None) -> dict:
    api = kind == "api"
    return {
        "displayName": config.name(kind), "tags": [config.marker(kind)], "notes": config.notes(kind),
        "signInAudience": "AzureADMyOrg", "isFallbackPublicClient": False,
        "identifierUris": [f"api://{api_id}"] if api and api_id else [],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [scope_definition(config)] if api else [],
            "preAuthorizedApplications": [{"appId": spa_id, "delegatedPermissionIds": [config.scope_id]}] if api and spa_id else [],
        },
        "spa": {"redirectUris": [] if api else [config.redirect]},
        "web": {"redirectUris": [], "implicitGrantSettings": {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": False}},
        "publicClient": {"redirectUris": []},
        "requiredResourceAccess": [] if api else [
            {"resourceAppId": api_id, "resourceAccess": [{"id": config.scope_id, "type": "Scope"}]},
        ],
        "appRoles": [],
    }


class Journal:
    def __init__(self, path: Path | None, config: Configuration):
        self.path = journal_path(path) if path else None
        self.config = config
        self.data = {
            "version": 1, "source_configuration_hash": config.source_hash,
            "configuration": config.source, "scope_id": config.scope_id, "resources": {}, "operations": {},
        }
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
                if (
                    self.data["version"] != 1 or self.data["source_configuration_hash"] != config.source_hash
                    or self.data["configuration"] != config.source or self.data["scope_id"] != config.scope_id
                    or set(self.data["resources"]) - {"api", "spa", "api_sp", "spa_sp"}
                    or not isinstance(self.data["operations"], dict)
                ):
                    raise ValueError
                object_ids, app_ids = set(), {}
                for kind, resource in self.data["resources"].items():
                    object_id, app_id = guid(resource["object_id"]), guid(resource["app_id"])
                    if object_id in object_ids or resource != {"object_id": object_id, "app_id": app_id}:
                        raise ValueError
                    object_ids.add(object_id)
                    app_kind = kind.removesuffix("_sp")
                    if app_kind in app_ids and app_ids[app_kind] != app_id:
                        raise ValueError
                    app_ids[app_kind] = app_id
                if app_ids.get("api") and app_ids.get("api") == app_ids.get("spa"):
                    raise ValueError
            except (ValueError, KeyError, TypeError, OSError):
                raise SetupError("Journal configuration or resource IDs conflict; preserve it and reconcile explicitly.") from None
        for kind, approved in config.approved_ids.items():
            if approved and self.resources.get(kind, {}).get("app_id", approved) != approved:
                raise SetupError("Approved application IDs conflict with the journal.")

    @property
    def resources(self):
        return self.data["resources"]

    def save(self):
        if not self.path:
            raise SetupError("--apply requires an external --journal JSON path.")
        staging = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.writing")
        try:
            descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, self.path)
        finally:
            staging.unlink(missing_ok=True)

    def state(self, operation: str) -> str | None:
        return self.data["operations"].get(operation, {}).get("state")

    def record(self, operation: str, state: str):
        self.data["operations"][operation] = {"state": state}
        self.save()

    def confirm_resource(self, kind: str, resource: dict):
        confirmed = {"object_id": guid(resource.get("id")), "app_id": guid(resource.get("appId"))}
        if kind in self.resources and self.resources[kind] != confirmed:
            raise SetupError("Graph resource IDs conflict with confirmed journal IDs.")
        for existing_kind, existing in self.resources.items():
            if existing_kind == kind:
                continue
            same_application = existing_kind.removesuffix("_sp") == kind.removesuffix("_sp")
            if existing["object_id"] == confirmed["object_id"] or (
                (existing["app_id"] == confirmed["app_id"]) != same_application
            ):
                raise SetupError("Graph returned conflicting application or object IDs across resources.")
        self.resources[kind] = confirmed
        self.record(f"create_{kind}", "confirmed")

    def confirm_assignment(self, operation: str, object_id: str):
        object_id = opaque_assignment_id(object_id)
        previous = self.data["operations"].get(operation, {}).get("object_id")
        if previous and previous != object_id:
            raise SetupError("Participant assignment ID conflicts with the confirmed journal; reconcile separately.")
        self.data["operations"][operation] = {"state": "confirmed", "object_id": object_id}
        self.save()

    @contextmanager
    def locked(self):
        if not self.path:
            raise SetupError("--apply requires an external --journal JSON path.")
        lock = self.path.with_suffix(".json.lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise SetupError("Journal is locked. Reconcile any active/interrupted operator before removing the lock.") from None
        try:
            os.close(descriptor)
            # Reload under the lock; another process may have advanced a journal since preview.
            self.data = Journal(self.path, self.config).data
            self.data["status"] = "reconciling"
            self.data.pop("participant_assignments", None)
            self.save()
            yield
        finally:
            lock.unlink(missing_ok=True)


class Graph:
    def __init__(self, client: httpx.Client, credential):
        self.client = client
        self.credential = credential

    def request(self, method: str, path: str, **kwargs):
        try:
            token = self.credential.get_token(GRAPH_SCOPE).token
        except (ClientAuthenticationError, CredentialUnavailableError):
            raise AuthenticationError("Azure CLI could not authenticate in the explicit tenant; sign in separately. No fallback or login was attempted.") from None
        if not isinstance(token, str) or not token:
            raise AuthenticationError("Azure CLI returned no Graph token; no fallback or login was attempted.")
        try:
            response = self.client.request(
                method, GRAPH + path, headers={"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"},
                follow_redirects=False, **kwargs,
            )
        except httpx.HTTPError:
            raise GraphError(uncertain=method != "GET") from None
        if response.status_code >= 300:
            raise GraphError(response.status_code, uncertain=method != "GET" and (
                response.status_code >= 500 or response.status_code == 408
            ))
        if response.status_code == 204:
            return {}
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ValueError:
            raise GraphError(uncertain=method != "GET") from None

    def collection(self, path: str, *, params: dict | None = None, limit: int = 2) -> list[dict]:
        result = []
        next_path = path
        for _ in range(4):
            page = self.request("GET", next_path, params=params)
            values = page.get("value")
            if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
                raise SetupError("Graph returned an invalid bounded collection; no adoption was performed.")
            result.extend(values)
            if len(result) > limit:
                raise SetupError("Graph results exceed the exact bounded allowlist; reconcile conflicting resources or assignments.")
            continuation = page.get("@odata.nextLink")
            if not continuation:
                return result
            parts = urlsplit(continuation)
            if (
                parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
                or parts.path != "/v1.0" + path or parts.fragment
            ):
                raise SetupError("Graph pagination left the bounded resource path.")
            next_path = continuation.removeprefix(GRAPH)
            params = None
        raise SetupError("Graph pagination exceeded the bounded reconciliation limit.")


def mutate(graph: Graph, journal: Journal, operation: str, method: str, path: str, body: dict, *, resource_kind: str | None = None):
    journal.record(operation, "in_flight")
    try:
        result = graph.request(method, path, json=body)
    except AuthenticationError:
        journal.record(operation, "not_sent")
        raise
    except GraphError as error:
        journal.record(operation, "uncertain" if error.uncertain else "denied" if error.status in (401, 403) else "rejected")
        raise
    if resource_kind:
        try:
            guid(result["id"])
            guid(result["appId"])
        except (KeyError, SetupError):
            journal.record(operation, "uncertain")
            raise GraphError(uncertain=True) from None
        journal.confirm_resource(resource_kind, result)
    else:
        if method == "POST":
            try:
                assignment_id = opaque_assignment_id(result["id"])
            except (KeyError, SetupError):
                journal.record(operation, "uncertain")
                raise GraphError(uncertain=True) from None
            journal.confirm_assignment(operation, assignment_id)
        else:
            journal.record(operation, "confirmed")
    return result


def quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def owned_application(config: Configuration, kind: str, app: dict):
    try:
        guid(app["id"])
        guid(app["appId"])
        if (
            config.marker(kind) not in app["tags"] or app["notes"] != config.notes(kind)
            or app["displayName"] != config.name(kind) or app["signInAudience"] != "AzureADMyOrg"
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SetupError("Application ownership, single-tenant audience, or deterministic name conflicts; no adoption is allowed.") from None


def ensure_application(graph: Graph, journal: Journal, kind: str, *, api_id: str | None = None) -> dict:
    config = journal.config
    matches = graph.collection("/applications", params={
        "$filter": f"tags/any(t:t eq {quoted(config.marker(kind))})",
        "$select": APP_SELECT, "$top": "2", "$count": "true",
    })
    if len(matches) > 1:
        raise SetupError("Multiple applications have the exact ownership marker; reconcile before retrying.")
    known = journal.resources.get(kind)
    approved = config.approved_ids[kind]
    if known:
        app = graph.request("GET", f"/applications/{known['object_id']}", params={"$select": APP_SELECT})
    elif approved:
        approved_matches = graph.collection("/applications", params={
            "$filter": f"appId eq {quoted(approved)}", "$select": APP_SELECT, "$top": "2",
        })
        if len(approved_matches) != 1:
            raise SetupError("The explicit approved application ID was not uniquely found; it will not be created or substituted.")
        app = approved_matches[0]
    elif matches:
        app = matches[0]
    else:
        if journal.state(f"create_{kind}") in ("in_flight", "uncertain", "confirmed"):
            raise SetupError("A previous application create is unresolved. Exact-marker query found none; wait/reconcile, never delete the journal or blindly recreate.")
        app = mutate(
            graph, journal, f"create_{kind}", "POST", "/applications",
            application_body(config, kind, api_id=api_id), resource_kind=kind,
        )
        app = graph.request("GET", f"/applications/{guid(app['id'])}", params={"$select": APP_SELECT})
    owned_application(config, kind, app)
    if matches and (matches[0]["id"] != app["id"] or matches[0]["appId"] != app["appId"]):
        raise SetupError("Explicit/journal IDs conflict with the owned-marker query; no resource was substituted.")
    if approved and app["appId"] != approved:
        raise SetupError("The owned resource differs from the approved application ID.")
    journal.confirm_resource(kind, app)
    return app


def application_patch(config: Configuration, kind: str, app: dict, *, api_id: str, spa_id: str) -> dict:
    desired = application_body(config, kind, api_id=api_id, spa_id=spa_id)
    api = app.get("api") or {}
    expected_scope = [scope_definition(config)] if kind == "api" else []
    scopes = api.get("oauth2PermissionScopes") or []
    expected_preauth = desired["api"]["preAuthorizedApplications"]
    if (
        app.get("passwordCredentials") or app.get("keyCredentials") or app.get("appRoles")
        or api.get("knownClientApplications") or api.get("acceptMappedClaims")
        or (scopes and scopes != expected_scope)
        or (api.get("preAuthorizedApplications") and api["preAuthorizedApplications"] != expected_preauth)
        or (app.get("requiredResourceAccess") and app["requiredResourceAccess"] != desired["requiredResourceAccess"])
        or (app.get("identifierUris") and app["identifierUris"] != desired["identifierUris"])
        or any((app.get(key) or {}).get("redirectUris") not in (None, [], desired[key]["redirectUris"]) for key in ("spa", "web", "publicClient"))
        or (app.get("web") or {}).get("homePageUrl") or (app.get("web") or {}).get("logoutUrl")
    ):
        raise SetupError("Owned application has conflicting scopes, clients, redirects, credentials, or permissions; unrelated settings will not be overwritten.")
    patch = {}
    for key in ("identifierUris", "isFallbackPublicClient", "requiredResourceAccess"):
        if app.get(key) != desired[key]:
            patch[key] = desired[key]
    if any(api.get(key) != value for key, value in desired["api"].items()):
        patch["api"] = desired["api"]
    for key in ("spa", "publicClient"):
        if (app.get(key) or {}).get("redirectUris") != desired[key]["redirectUris"]:
            patch[key] = desired[key]
    web = app.get("web") or {}
    if web.get("redirectUris") != [] or web.get("implicitGrantSettings") != desired["web"]["implicitGrantSettings"]:
        patch["web"] = desired["web"]
    return patch


def configure_application(graph: Graph, journal: Journal, kind: str, app: dict, *, api_id: str, spa_id: str):
    patch = application_patch(journal.config, kind, app, api_id=api_id, spa_id=spa_id)
    if patch:
        path = f"/applications/{guid(app['id'])}"
        mutate(graph, journal, f"configure_{kind}", "PATCH", path, patch)
        app = graph.request("GET", path, params={"$select": APP_SELECT})
        owned_application(journal.config, kind, app)
        if application_patch(journal.config, kind, app, api_id=api_id, spa_id=spa_id):
            raise SetupError("Graph has not verified the exact application settings. Keep flags disabled and reconcile.")
    if journal.state(f"configure_{kind}") in ("in_flight", "uncertain"):
        journal.record(f"configure_{kind}", "confirmed")
    journal.record(f"verify_{kind}", "confirmed")


def ensure_sp(graph: Graph, journal: Journal, kind: str, app_id: str) -> dict:
    resource_kind = kind + "_sp"
    matches = graph.collection("/servicePrincipals", params={
        "$filter": f"appId eq {quoted(guid(app_id))}", "$select": SP_SELECT, "$top": "2",
    })
    if len(matches) > 1:
        raise SetupError("Multiple service principals match the exact application ID.")
    if not matches:
        if resource_kind in journal.resources or journal.state(f"create_{resource_kind}") in ("in_flight", "uncertain", "confirmed"):
            raise SetupError("A service principal create is unresolved; reconcile its exact application ID before retrying.")
        sp = mutate(graph, journal, f"create_{resource_kind}", "POST", "/servicePrincipals", {
            "appId": app_id, "appRoleAssignmentRequired": True,
            "tags": [journal.config.marker(kind)], "notes": journal.config.notes(kind),
        }, resource_kind=resource_kind)
        sp = graph.request("GET", f"/servicePrincipals/{guid(sp['id'])}", params={"$select": SP_SELECT})
    else:
        sp = matches[0]
    if (
        guid(sp.get("appId")) != app_id or sp.get("servicePrincipalType") != "Application"
        or sp.get("appOwnerOrganizationId") != journal.config.tenant_id
    ):
        raise SetupError("Service principal does not belong to the exact owned tenant application.")
    journal.confirm_resource(resource_kind, sp)
    if sp.get("appRoleAssignmentRequired") is not True:
        path = f"/servicePrincipals/{guid(sp['id'])}"
        mutate(graph, journal, f"require_assignment_{kind}", "PATCH", path, {"appRoleAssignmentRequired": True})
        sp = graph.request("GET", path, params={"$select": SP_SELECT})
        if sp.get("appRoleAssignmentRequired") is not True:
            raise SetupError("Participant-assignment enforcement was not verified; it will not be disabled as a fallback.")
    if journal.state(f"require_assignment_{kind}") in ("in_flight", "uncertain"):
        journal.record(f"require_assignment_{kind}", "confirmed")
    journal.record(f"verify_{resource_kind}", "confirmed")
    return sp


def assignments(graph: Graph, journal: Journal, principals: dict, *, assign: bool) -> dict:
    allowed = {subject.split(":")[1] for subject in journal.config.actors}
    existing = {}
    try:
        for kind, sp in principals.items():
            rows = graph.collection(f"/servicePrincipals/{guid(sp['id'])}/appRoleAssignedTo", params={
                "$select": "id,principalId,principalType,appRoleId,resourceId", "$top": "100",
            }, limit=100)
            if any(
                row.get("principalId") not in allowed or row.get("principalType") != "User"
                or row.get("appRoleId") != ZERO or row.get("resourceId") != sp["id"]
                for row in rows
            ):
                raise SetupError("Existing application assignments exceed the explicit participant allowlist. Reconcile them separately; no grants were removed.")
            if len({row["principalId"] for row in rows}) != len(rows):
                raise SetupError("Duplicate participant assignments require separate reconciliation.")
            for row in rows:
                operation = f"assign_{kind}_{digest(row['principalId'])}"
                journal.confirm_assignment(operation, row.get("id"))
            existing[kind] = {row["principalId"] for row in rows}
        pending = sum(len(allowed - existing[kind]) for kind in principals)
        if not assign:
            return {"state": "pending_operator_assignment" if pending else "verified", "pending_assignments": pending}
        # Validate every explicit object before granting anyone. Never look up a display name or invite.
        for object_id in sorted(allowed):
            user = graph.request("GET", f"/users/{guid(object_id)}", params={"$select": "id,accountEnabled,userType"})
            if user.get("id") != object_id or user.get("accountEnabled") is not True or user.get("userType") not in ("Member", "Guest"):
                raise SetupError("An allowlisted participant is not an existing enabled tenant user; no invitation or substitute is allowed.")
        for kind, sp in principals.items():
            for object_id in sorted(allowed - existing[kind]):
                operation = f"assign_{kind}_{digest(object_id)}"
                if journal.state(operation) in ("in_flight", "uncertain", "confirmed"):
                    raise SetupError("A prior participant assignment is unresolved; wait/reconcile its exact resource and principal before retrying.")
                mutate(graph, journal, operation, "POST", f"/servicePrincipals/{guid(sp['id'])}/appRoleAssignedTo", {
                    "principalId": object_id, "resourceId": sp["id"], "appRoleId": ZERO,
                })
        # Confirm membership by reading Graph, not by assuming that POST success means usable access.
        return assignments(graph, journal, principals, assign=False)
    except GraphError as error:
        if error.status not in (401, 403):
            raise
        journal.record("participant_assignments", "pending_permissions")
        return {"state": "pending_permissions", "pending_assignments": "unverified", "message": str(error)}


def summary(
    config: Configuration, journal: Journal, *, mode: str,
    assignment_status: dict | None = None, assign_participants: bool = False,
) -> dict:
    api_id = journal.resources.get("api", {}).get("app_id") or config.approved_ids["api"] or "<API_APP_ID>"
    spa_id = journal.resources.get("spa", {}).get("app_id") or config.approved_ids["spa"] or "<SPA_APP_ID>"
    return {
        "mode": mode, "status": "offline_preview" if mode == "preview" else "registrations_verified_access_pending",
        "tenant_id": config.tenant_id, "source_configuration_hash": config.source_hash,
        "names": {kind: config.name(kind) for kind in ("api", "spa")},
        "ownership_markers": {kind: config.marker(kind) for kind in ("api", "spa")},
        "redirect_uri": config.redirect, "redirect_origin": f"{urlsplit(config.redirect).scheme}://{urlsplit(config.redirect).netloc}",
        "participant_mapping_counts": {"total": len(config.actors), "by_role": config.counts},
        "audience": api_id, "delegated_scope": f"api://{api_id}/{SCOPE}", "scope_id": config.scope_id,
        "allowed_spa_client_id": spa_id, "requested_access_token_version": 2,
        "sign_in_audience": "AzureADMyOrg", "authorization_code_pkce": True, "implicit_flows": False,
        "application_assignment_required": {"api": True, "spa": True},
        "participant_assignment_requested": assign_participants,
        "participant_assignments": assignment_status or {"state": "not_checked_offline"},
        "confirmed_resources": journal.resources, "ready_for_participants": False,
        "operator_permissions_to_review": {
            "credential": "AzureCliCredential",
            "credential_scope": GRAPH_SCOPE,
            "interactive_fallback": False,
            "registration_delegated_permissions": ["Application.ReadWrite.All"],
            "assign_participants_additional_delegated_permissions": ["AppRoleAssignment.ReadWrite.All", "User.Read.All"],
            "operator_authorization": (
                "Use an already authorized registration owner with Application Developer for owned single-tenant "
                "management, or an existing Cloud Application Administrator/Application Administrator (or supported "
                "custom role). Participant assignment additionally needs an already authorized supported administrator, "
                "such as Cloud Application Administrator/Application Administrator. Tenant policies still apply."
            ),
            "permission_or_role_changes_performed": False,
        },
        "proposed_lifecycle": [
            "Find exact owned markers; reject conflicting IDs, ownership, or unrelated settings.",
            "Create missing single-tenant API and SPA applications; checkpoint every confirmed ID.",
            "Create missing tenant service principals; verify required user assignment on both before preauthorization.",
            "Expose only access_as_user; API v2 GUID audience; preauthorize only the exact SPA and scope ID.",
            "Configure only the exact SPA origin, delegated API permission, and disabled implicit flows; no secrets.",
            "Audit exact participant allowlist; grant default application access only with --assign-participants.",
            "Query-reconcile uncertain creates without resubmission; retain the external journal.",
        ],
        "configuration_to_review_not_applied": {
            "api": {
                "INSURANCE_AUTH_TENANT_ID": config.tenant_id, "INSURANCE_AUTH_AUDIENCE": api_id,
                "INSURANCE_AUTH_REQUIRED_SCOPE": SCOPE, "INSURANCE_AUTH_CLIENT_IDS": spa_id,
                "NORTHWIND_BUSINESS_API_ENABLED": "false",
            },
            "frontend": {
                "VITE_INSURANCE_AUTH_TENANT_ID": config.tenant_id, "VITE_INSURANCE_AUTH_CLIENT_ID": spa_id,
                "VITE_INSURANCE_AUTH_SCOPE": f"api://{api_id}/{SCOPE}", "VITE_INSURANCE_RUN_ID": config.run_id,
                "VITE_INSURANCE_BUSINESS_ENABLED": "false",
            },
        },
        "next_steps": [
            "Operator review: approve tenant, exact redirect, API GUID audience, SPA client binding, and participant map.",
            "Review delegated Graph Application.ReadWrite.All. --assign-participants additionally needs "
            "AppRoleAssignment.ReadWrite.All and User.Read.All for existing-user validation, plus an already authorized "
            "operator role. No permissions or roles are granted by this tool; do not relax assignment controls.",
            "An administrator must review tenant consent policies and any required app-specific consent separately. "
            "No oauth2PermissionGrants, directory-wide consent, or admin roles are changed here.",
            "Separately authenticate and run scripts\\seed-insurance-run.py with --run-id and the same --actors-json; "
            "review its preview before its distinct --apply. Only the persisted run map grants insurance roles.",
            "Verify delegated v2 RS256 token claims, participant assignment, and negative-access tests before a separately approved deployment/enablement.",
        ],
        "not_performed": [
            "login", "client secrets", "admin consent", "oauth2PermissionGrants", "directory role changes",
            "participant invitations", "Cosmos seeding", "GitHub writes", "deployment", "feature enablement",
        ],
    }


def apply(config: Configuration, journal: Journal, *, assign_participants: bool = False, transport=None) -> dict:
    with journal.locked():
        with AzureCliCredential(tenant_id=config.tenant_id, process_timeout=30) as credential:
            with httpx.Client(timeout=30, transport=transport, trust_env=False) as client:
                graph = Graph(client, credential)
                api = ensure_application(graph, journal, "api")
                spa = ensure_application(graph, journal, "spa", api_id=api["appId"])
                if api["appId"] == spa["appId"]:
                    raise SetupError("API and SPA IDs must be distinct.")
                for kind, app in (("api", api), ("spa", spa)):
                    application_patch(config, kind, app, api_id=api["appId"], spa_id=spa["appId"])
                principals = {
                    kind: ensure_sp(graph, journal, kind, app["appId"])
                    for kind, app in (("api", api), ("spa", spa))
                }
                for kind, app in (("api", api), ("spa", spa)):
                    configure_application(graph, journal, kind, app, api_id=api["appId"], spa_id=spa["appId"])
                status = assignments(graph, journal, principals, assign=assign_participants)
                journal.data["participant_assignments"] = status
                journal.data["status"] = "registrations_verified_access_pending"
                journal.save()
                return summary(config, journal, mode="apply", assignment_status=status, assign_participants=assign_participants)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Validate fully offline (the default); no journal writes or authentication.")
    mode.add_argument("--apply", action="store_true", help="Explicitly approve reconciliation of this owned identity deployment only.")
    parser.add_argument("--tenant-id", required=True, help="Explicit target tenant UUID; no tenant default.")
    parser.add_argument("--deployment-id", required=True, help="Stable operator-chosen UUID used in exact ownership markers.")
    parser.add_argument("--redirect-uri", required=True, help="Operator-approved exact SPA HTTPS origin; never inferred.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actors-json", required=True, type=Path, help="Same explicit participant map accepted by seed-insurance-run.py.")
    parser.add_argument("--journal", type=Path, help="Persistent external .json path, required for apply; never commit or delete after uncertainty.")
    parser.add_argument("--api-app-id", help="Optional approved existing API application/client UUID, not its object ID; exact ownership required.")
    parser.add_argument("--spa-app-id", help="Optional approved existing SPA application/client UUID; exact ownership required.")
    parser.add_argument("--development", action="store_true", help="Explicit development deployment; permits localhost origins, including HTTP.")
    parser.add_argument("--assign-participants", action="store_true", help="With --apply only, grant default app access solely to mapped existing users on both SPs; no insurance role grant.")
    args = parser.parse_args(argv)
    try:
        config = Configuration(
            tenant_id=args.tenant_id, deployment_id=args.deployment_id, redirect=args.redirect_uri,
            run_id=args.run_id, actors_json=args.actors_json, development=args.development,
            api_app_id=args.api_app_id, spa_app_id=args.spa_app_id,
        )
        journal = Journal(args.journal, config)
        result = apply(config, journal, assign_participants=args.assign_participants) if args.apply else summary(
            config, journal, mode="preview", assign_participants=args.assign_participants,
        )
    except SetupError as error:
        print(json.dumps({"status": "not_confirmed", "error": str(error), "ready_for_participants": False}))
        return 1
    except OSError:
        print(json.dumps({
            "status": "not_confirmed", "error": "Local configuration/journal I/O failed; preserve the journal and reconcile before retrying.",
            "ready_for_participants": False,
        }))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
