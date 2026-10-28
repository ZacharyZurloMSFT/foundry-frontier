"""Short-lived draft capabilities; packaged verbatim for the hosted signer and MCP verifier."""
from __future__ import annotations

import hashlib
import json
import re
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

AUDIENCE = "northwind-insurance-claim-review"
ISSUER = "northwind-insurance-claim-action-draft"
MAX_AGE_SECONDS = 120
MAX_PROOF_LENGTH = 8192


def _digest(workflow_id: str, draft: dict) -> str:
    if not re.fullmatch(r"wf-[A-Za-z0-9][A-Za-z0-9_-]{0,123}", workflow_id):
        raise ValueError("Invalid application-owned workflow ID")
    encoded = json.dumps(draft, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def signing_key(pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise ValueError("Claim workflow signing key must be a private RSA key of at least 2048 bits") from None


def sign_workflow(workflow_id: str, draft: dict, key: rsa.RSAPrivateKey) -> str:
    now = int(time.time())
    return jwt.encode({
        "iss": ISSUER, "aud": AUDIENCE, "sub": workflow_id, "draft_sha256": _digest(workflow_id, draft),
        "iat": now, "nbf": now, "exp": now + MAX_AGE_SECONDS,
    }, key, algorithm="RS256")


def verify_workflow(proof: str | None, workflow_id: str, draft: dict, public_pem: str) -> None:
    try:
        key = serialization.load_pem_public_key(public_pem.encode())
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Claim workflow public key must be a public RSA key of at least 2048 bits") from None
    try:
        if not isinstance(proof, str) or not 0 < len(proof) <= MAX_PROOF_LENGTH:
            raise ValueError()
        claims = jwt.decode(proof, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER,
                            options={"require": ["iss", "aud", "sub", "draft_sha256", "iat", "nbf", "exp"],
                                     "strict_aud": True})
        if (any(type(claims[name]) is not int for name in ("iat", "nbf", "exp"))
                or not 0 < claims["exp"] - claims["iat"] <= MAX_AGE_SECONDS
                or claims["nbf"] != claims["iat"]
                or claims["sub"] != workflow_id
                or claims["draft_sha256"] != _digest(workflow_id, draft)):
            raise ValueError()
    except (jwt.PyJWTError, ValueError, TypeError):
        raise ValueError("Invalid or expired application-bound workflow proof") from None
