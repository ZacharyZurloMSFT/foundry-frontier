"""Signed workflow capabilities never replace gateway identity or human review."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from app.workflow_binding import (
    AUDIENCE,
    ISSUER,
    sign_workflow,
    signing_key,
    verify_workflow,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_insurance import CLAIM_REVIEW, DRAFT, load_mcp


@pytest.fixture
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def test_proof_binds_exact_workflow_and_draft(keys):
    private, public = keys
    proof = sign_workflow("wf-owned", DRAFT, private)
    verify_workflow(proof, "wf-owned", dict(reversed(list(DRAFT.items()))), public)
    for workflow, draft in (("wf-other", DRAFT), ("wf-owned", {**DRAFT, "summary": "Changed"})):
        with pytest.raises(ValueError, match="workflow proof"):
            verify_workflow(proof, workflow, draft, public)
    with pytest.raises(ValueError, match="workflow proof"):
        verify_workflow(proof, "wf-owned", DRAFT, rsa.generate_private_key(
            public_exponent=65537, key_size=2048).public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())


@pytest.mark.parametrize("changes", [
    {"exp": 0}, {"iat": int(time.time()) + 600}, {"exp": int(time.time()) + 600},
    {"aud": "other"}, {"iss": "other"}, {"sub": "wf-other"}, {"iat": "1"},
    {"draft_sha256": "wrong"}, {"nbf": 0},
])
def test_invalid_claims_rejected(keys, changes):
    private, public = keys
    claims = jwt.decode(sign_workflow("wf-owned", DRAFT, private), options={"verify_signature": False})
    proof = jwt.encode({**claims, **changes}, private, algorithm="RS256")
    with pytest.raises(ValueError, match="workflow proof"):
        verify_workflow(proof, "wf-owned", DRAFT, public)


@pytest.mark.parametrize("proof", [None, "", "invalid", "x" * 8193])
def test_missing_or_malformed_proof_rejected(keys, proof):
    with pytest.raises(ValueError, match="workflow proof"):
        verify_workflow(proof, "wf-owned", DRAFT, keys[1])


def test_signing_key_rejects_public_or_invalid_material(keys):
    for value in ("", "not a key", keys[1]):
        with pytest.raises(ValueError, match="private RSA"):
            signing_key(value)


@pytest.mark.asyncio
async def test_signed_mode_requires_proof_even_with_legacy_headers(keys, monkeypatch):
    private, public = keys
    monkeypatch.setenv("CLAIM_WORKFLOW_BINDING", "signed")
    monkeypatch.setenv("CLAIM_WORKFLOW_PUBLIC_KEY", public)
    module = load_mcp(CLAIM_REVIEW)
    queue = SimpleNamespace(create_item=AsyncMock())
    monkeypatch.setattr(module, "_cosmos_container", lambda: queue)
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={"x-workflow-id": "wf-owned"})))
    draft = module.ClaimActionDraft.model_validate(DRAFT)
    for proof in (None, sign_workflow("wf-other", DRAFT, private),
                  sign_workflow("wf-owned", {**DRAFT, "summary": "changed"}, private)):
        with pytest.raises(ValueError, match="workflow proof"):
            await module.draft_claim_action("wf-owned", draft, ctx, proof)
    queue.create_item.assert_not_awaited()
    result = json.loads(await module.draft_claim_action(
        "wf-owned", draft, ctx, sign_workflow("wf-owned", module.action_payload(draft), private)))
    assert result["status"] == "pending_approval" and result["execution_status"] == "not_executed"
    queue.create_item.assert_awaited_once()


def test_algorithm_confusion_rejected(keys):
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "wf-owned"}
    proof = jwt.encode(claims, "not-an-rsa-key-" * 3, algorithm="HS256")
    with pytest.raises(ValueError, match="workflow proof"):
        verify_workflow(proof, "wf-owned", DRAFT, keys[1])
