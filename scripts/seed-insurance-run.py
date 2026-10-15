"""Preview a create-only synthetic run; --apply explicitly writes existing Cosmos."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from azure.core.exceptions import AzureError
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.business.auth import subject_id  # noqa: E402
from app.business.models import ActorMapping, BusinessError  # noqa: E402
from app.business.service import seed_run  # noqa: E402
from app.business.store import CosmosBusinessRepository  # noqa: E402
from app.settings import get_settings  # noqa: E402


def actor_mappings(path: Path) -> dict[str, ActorMapping]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mappings = TypeAdapter(dict[str, ActorMapping]).validate_python(data)
    if len(mappings) > 100:
        raise ValueError("A bounded demo run allows at most 100 identity mappings.")
    for subject in mappings:
        parts = subject.split(":")
        if len(parts) != 2 or subject_id(*parts) != subject:
            raise ValueError("Mapping keys must be canonical tenant-UUID:object-UUID subjects.")
    return mappings


async def seed(run_id: str, path: Path, *, apply: bool = False) -> dict:
    mappings = actor_mappings(path)
    state = seed_run(run_id, mappings)
    summary = {
        "mode": "apply" if apply else "preview", "run_id": state.run_id,
        "source_version": state.source_version, "policies": len(state.policies),
        "claims": len(state.claims), "identity_mappings": len(state.actors),
        "synthetic": True, "existing_runs_overwritten": False,
    }
    if not apply:
        return summary
    settings = get_settings()
    if settings.business_domain != "insurance" or not settings.cosmos_endpoint:
        raise BusinessError("not_configured", "Explicit insurance domain and Cosmos configuration are required.")
    tenant = str(uuid.UUID(settings.business_auth_tenant_id))
    if any(subject.split(":")[0] != tenant for subject in mappings):
        raise BusinessError("tenant_mismatch", "All mapped identities must belong to the configured insurance tenant.")
    from app.azure_clients import get_cosmos_client
    client = get_cosmos_client()
    try:
        await CosmosBusinessRepository().create(state)
    finally:
        await client.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actors-json", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Create the run; never overwrite or reset one.")
    args = parser.parse_args()
    try:
        summary = asyncio.run(seed(args.run_id, args.actors_json, apply=args.apply))
    except (ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, BusinessError) else "invalid_configuration"
        raise SystemExit(f"Insurance run was not confirmed ({code}); check the mapping and configuration.") from None
    except AzureError:
        raise SystemExit("Storage did not confirm run creation. Reconcile the run before retrying; no reset was attempted.") from None
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
