# 0004 — OIDC trust on the public repository is an explicit opt-in

> Status: Accepted · Recorded: 2026-10-23 (the owner approved it on 2026-10-22) · Sources:
> [api-management.md](../../api-management.md#repository-visibility),
> [`scripts/configure-github-oidc.py`](../../../scripts/configure-github-oidc.py), PR #68

## Context

The pipeline identities for the API Management workflow are created by `configure-github-oidc.py`, which was
written for a private repository and refused a public one. This repository is public. Why it is public is not
recorded here.

## Decision

- The owner accepted OIDC trust on the public repository on 2026-10-22, as an explicit opt-in rather than a
  removed check: `validate_repository` still refuses a public repository unless `--allow-public-repository` is
  passed.
- The opt-in relaxes only the visibility check. Still enforced: the `main` default branch; the immutable
  repository and owner IDs and the immutable OIDC subject; environments pinned to `main` before any Azure trust
  exists; apply identities that cannot read secrets (`listSecrets`) and are limited to an allow-list of resource
  types in one resource group.

## Consequences

- Keep the strict default. Do not drop the flag to make the refusal go away.
- Workflow logs, plan artifacts and step summaries are world-readable. The safety argument rests on them carrying
  only resource names and plan hashes, with no secret passed through the workflows, so never print a key, token or
  property value there.
- Pull requests, including fork pull requests, cannot obtain these tokens because the environments are pinned to
  `main`; dispatching the workflows needs write access.
- If the repository becomes private, run the script without the flag; no code change is needed.
- Changing the allow-list or the roles is a deliberate boundary change made together with its tests
  ([api-management.md](../../api-management.md#changing-the-api)).
