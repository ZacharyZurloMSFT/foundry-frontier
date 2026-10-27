"""Per-turn authority: who is asking, which of their records are in scope, and a read-only snapshot.

Identity comes only from the verified token via `BusinessContext`; the role and customer mapping come
from the run. Each business-service call below re-applies the same visibility rules as the portal.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..business.models import BusinessError, ClaimRecord, DemoRun, PageRequest, PolicySnapshot
from ..business.routes import BusinessContext
from ..business.service import InsuranceBusinessService
from ..business.store import BusinessRepository, StoredRun
from .models import ChatContext, ScopeSummary
from .profiles import AssistantProfile

LIST_LIMIT = 20


class TurnSnapshot:
    """Reads each run once per turn and refuses every write: one consistent, immutable view per answer."""

    def __init__(self, repository: BusinessRepository):
        self._repository = repository
        self._runs: dict[str, StoredRun] = {}

    async def get(self, run_id: str) -> StoredRun:
        if run_id not in self._runs:
            self._runs[run_id] = await self._repository.get(run_id)
        return self._runs[run_id]

    async def create(self, state: DemoRun) -> StoredRun:
        raise RuntimeError("Assistant snapshots are read-only.")

    async def replace(self, state: DemoRun, etag: str) -> StoredRun:
        raise RuntimeError("Assistant snapshots are read-only.")


@dataclass(frozen=True)
class AssistantScope:
    run_id: str
    subject_id: str
    actor_key: str
    role: str
    surface: str
    claim_id: str | None = None
    policy_id: str | None = None

    def summary(self) -> ScopeSummary:
        return ScopeSummary(claim_id=self.claim_id, policy_id=self.policy_id)


class ScopedReader:
    """Read-only access to exactly the records the caller may see."""

    def __init__(self, scope: AssistantScope, service: InsuranceBusinessService):
        self.scope = scope
        self._service = service

    async def policies(self) -> tuple[list[PolicySnapshot], bool]:
        page = await self._service.list_policies(
            self.scope.run_id, self.scope.subject_id, PageRequest(offset=0, limit=LIST_LIMIT))
        return page.items, page.next_offset is not None

    async def policy(self, policy_id: str) -> PolicySnapshot:
        return await self._service.get_policy(self.scope.run_id, self.scope.subject_id, policy_id)

    async def claims(self) -> tuple[list[ClaimRecord], bool]:
        page = await self._service.list_claims(
            self.scope.run_id, self.scope.subject_id, PageRequest(offset=0, limit=LIST_LIMIT))
        return page.items, page.next_offset is not None

    async def claim(self, claim_id: str) -> ClaimRecord:
        return await self._service.get_claim(self.scope.run_id, self.scope.subject_id, claim_id)


@dataclass(frozen=True)
class Caller:
    """The authorised identity of a request, for surfaces that read no personal records."""

    run_id: str
    actor_key: str
    role: str


async def authorise(ctx: BusinessContext, profile: AssistantProfile) -> tuple[InsuranceBusinessService, Caller]:
    service = InsuranceBusinessService(TurnSnapshot(ctx.service.repository))
    account = await service.account_context(ctx.run_id, ctx.subject_id)
    if account.role not in profile.roles:
        raise BusinessError("assistant_forbidden", "This assistant is not available for your account.", 403)
    return service, Caller(run_id=ctx.run_id, actor_key=account.actor_key, role=account.role)


async def open_caller(ctx: BusinessContext, profile: AssistantProfile) -> Caller:
    """Role-gate a general-help surface. The business service is dropped, so no record can be read through it."""
    _, caller = await authorise(ctx, profile)
    return caller


async def open_scope(ctx: BusinessContext, profile: AssistantProfile, context: ChatContext) -> ScopedReader:
    """Authorise the caller for a surface and verify any record hints before a model is involved."""
    service, caller = await authorise(ctx, profile)
    claim_id, policy_id = context.claim_id, context.policy_id
    if claim_id is not None:
        claim = await service.get_claim(ctx.run_id, ctx.subject_id, claim_id)
        if policy_id is not None and claim.policy_id != policy_id:
            raise BusinessError("context_mismatch", "The claim and policy do not belong together.", 422)
        policy_id = claim.policy_id
    elif policy_id is not None:
        await service.get_policy(ctx.run_id, ctx.subject_id, policy_id)
    scope = AssistantScope(
        run_id=ctx.run_id, subject_id=ctx.subject_id, actor_key=caller.actor_key, role=caller.role,
        surface=profile.surface, claim_id=claim_id, policy_id=policy_id,
    )
    return ScopedReader(scope, service)
