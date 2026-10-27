"""Authorization boundary for identity-bound assistants: scope, tools, guards and history. No model or cloud calls."""
import json
import re
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from app.assistants import history, profiles
from app.assistants.grounding import Grounding
from app.assistants.guards import GuardFailure, parse_answer, unsafe_determination, vet_answer
from app.assistants.models import AssistantAnswer, ChatContext
from app.assistants.profiles import PROFILES
from app.assistants.scope import TurnSnapshot, open_scope
from app.assistants.tools import SPECS, ToolBox
from app.business import store
from app.business.models import ActorMapping, AssignAdjuster, BusinessError, CreateNote, TransitionCommand
from app.business.routes import BusinessContext
from app.business.service import InsuranceBusinessService, seed_run
from assistant_fakes import ConversationContainer

ROOT = Path(__file__).resolve().parents[3]
ACTORS = {
    "customer-a": ActorMapping(role="policyholder", policyholder_id="PH-1001"),
    "customer-b": ActorMapping(role="policyholder", policyholder_id="PH-1002"),
    "employee-a": ActorMapping(role="adjuster"),
    "presenter-a": ActorMapping(role="presenter"),
}
OTHER_CUSTOMER_TEXT = ("hail damage to the insured SUV", "Jordan Sample", "POL-1002", "NW-AUTO-CO-2026-v1")


class CountingRepository:
    def __init__(self, inner):
        self.inner, self.reads = inner, 0

    async def get(self, run_id):
        self.reads += 1
        return await self.inner.get(run_id)

    async def create(self, state):
        return await self.inner.create(state)

    async def replace(self, state, etag):
        return await self.inner.replace(state, etag)


@pytest_asyncio.fixture
async def repository(database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(store, "_container", container)
    repo = store.CosmosBusinessRepository()
    await repo.create(seed_run("run-a", ACTORS))
    return repo


async def reader_for(repository, subject="customer-a", context=None):
    ctx = BusinessContext("run-a", subject, InsuranceBusinessService(repository))
    return await open_scope(ctx, PROFILES["concierge"], context or ChatContext())


async def call(box, name, **arguments):
    outcome = await box.call(name, json.dumps(arguments))
    return outcome, json.loads(outcome.output)


@pytest.mark.asyncio
async def test_tools_return_only_the_callers_own_records(repository):
    reader = await reader_for(repository)
    box = ToolBox(reader, Grounding(), PROFILES["concierge"].tools)
    claims = (await call(box, "list_my_claims"))[1]["claims"]
    policies = (await call(box, "list_my_policies"))[1]["policies"]
    assert [c["claim_id"] for c in claims] == ["CLM-2001"] and [p["policy_id"] for p in policies] == ["POL-1001"]
    mine = await call(box, "get_claim", claim_id="CLM-2001")
    assert mine[0].event.status == "ok" and mine[1]["policy_recorded_with_claim"]["wording"]["sections"]
    for outcome, payload in (await call(box, "get_claim", claim_id="CLM-2002"),
                             await call(box, "get_policy", policy_id="POL-1002"),
                             await call(box, "get_claim", claim_id="CLM-9999")):
        assert outcome.event.status == "not_found"
        assert payload == {"error": "not_found", "message": "No such record among the signed-in user's own records."}
        assert not any(text in outcome.output for text in OTHER_CUSTOMER_TEXT)


@pytest.mark.asyncio
async def test_customer_projection_never_carries_internal_case_data(repository):
    employee = InsuranceBusinessService(repository)
    version = (await employee.get_claim("run-a", "employee-a", "CLM-2001")).version
    await employee.create_note("run-a", "employee-a", "CLM-2001", CreateNote(
        expected_version=version, body="INTERNAL-SECRET-NOTE", visibility="internal"))
    await employee.create_note("run-a", "employee-a", "CLM-2001", CreateNote(
        expected_version=version + 1, body="Please send photos of the bumper.", visibility="customer"))
    await employee.transition_claim("run-a", "employee-a", "CLM-2001", TransitionCommand(
        target="evidence_received", expected_version=version + 2, reason="INTERNAL-REASON-TEXT"))
    await employee.assign_adjuster("run-a", "employee-a", "CLM-2001", AssignAdjuster(
        expected_version=version + 3, adjuster_id="employee-a"))
    box = ToolBox(await reader_for(repository), Grounding(), PROFILES["concierge"].tools)
    outcome, payload = await call(box, "get_claim", claim_id="CLM-2001")
    assert "Please send photos of the bumper." in outcome.output and payload["status"] == "evidence_received"
    for forbidden in ("INTERNAL-SECRET-NOTE", "INTERNAL-REASON-TEXT", "employee-a", "internal_note", "author_id",
                      "actor_id", "command_id", "assigned_adjuster", "assigned_to", "policyholder_id", "email", "phone"):
        assert forbidden not in outcome.output
    pinned = await ToolBox(await reader_for(repository, context=ChatContext(claim_id="CLM-2001")), Grounding(),
                           ()).pinned_context()
    assert "INTERNAL-SECRET-NOTE" not in json.dumps(pinned)


@pytest.mark.asyncio
async def test_models_cannot_supply_identity_or_call_unlisted_tools(repository):
    properties = {name for spec in SPECS.values() for name in spec["parameters"]["properties"]}
    assert properties == {"claim_id", "policy_id"}
    assert all(spec["parameters"]["additionalProperties"] is False for spec in SPECS.values())
    box = ToolBox(await reader_for(repository), Grounding(), PROFILES["concierge"].tools)
    for name, arguments in (
        ("get_claim", '{"claim_id":"CLM-2001","policyholder_id":"PH-1002"}'),
        ("get_claim", '{"claim_id":"clm-2001"}'), ("get_claim", '{"claim_id":123}'), ("get_claim", "[]"),
        ("get_claim", "not json"), ("list_my_claims", '{"subject_id":"customer-b"}'),
        ("get_policy", '{"policy_id":"POL-1001 OR 1=1"}'),
    ):
        outcome = await box.call(name, arguments)
        assert outcome.event.status == "invalid" and json.loads(outcome.output)["error"] == "invalid_arguments"
    for name in ("update_claim", "list_all_claims", "get_claim_internal"):
        outcome = await box.call(name, "{}")
        assert outcome.event.status == "invalid" and name not in outcome.event.label
    narrowed = ToolBox(await reader_for(repository), Grounding(), ("get_policy",))
    assert (await narrowed.call("get_claim", '{"claim_id":"CLM-2001"}')).event.status == "invalid"


@pytest.mark.asyncio
async def test_a_turn_reads_one_snapshot_and_can_never_write(repository):
    counting = CountingRepository(repository)
    ctx = BusinessContext("run-a", "customer-a", InsuranceBusinessService(counting))
    reader = await open_scope(ctx, PROFILES["concierge"], ChatContext(claim_id="CLM-2001"))
    box = ToolBox(reader, Grounding(), PROFILES["concierge"].tools)
    await box.pinned_context()
    for name, arguments in (("list_my_claims", {}), ("get_claim", {"claim_id": "CLM-2001"}),
                            ("get_policy", {"policy_id": "POL-1001"}), ("list_my_policies", {})):
        assert (await call(box, name, **arguments))[0].event.status == "ok"
    assert counting.reads == 1
    snapshot = TurnSnapshot(repository)
    stored = await snapshot.get("run-a")
    with pytest.raises(RuntimeError, match="read-only"):
        await snapshot.replace(stored.state, stored.etag)
    with pytest.raises(RuntimeError, match="read-only"):
        await snapshot.create(stored.state)


@pytest.mark.asyncio
async def test_scope_is_role_gated_and_record_hints_are_reauthorised(repository):
    for subject in ("employee-a", "presenter-a"):
        with pytest.raises(BusinessError) as denied:
            await reader_for(repository, subject)
        assert denied.value.code == "assistant_forbidden" and denied.value.status_code == 403
    with pytest.raises(BusinessError) as unmapped:
        await reader_for(repository, "stranger")
    assert unmapped.value.status_code == 403
    for hint in (ChatContext(claim_id="CLM-2002"), ChatContext(policy_id="POL-1002"), ChatContext(claim_id="CLM-9999")):
        with pytest.raises(BusinessError) as foreign:
            await reader_for(repository, context=hint)
        assert foreign.value.code == "not_found" and foreign.value.status_code == 404
    assert (await reader_for(repository, context=ChatContext(claim_id="CLM-2001", policy_id="POL-1001"))).scope.claim_id == "CLM-2001"
    with pytest.raises(BusinessError) as mismatch:
        await reader_for(repository, context=ChatContext(claim_id="CLM-2001", policy_id="POL-1002"))
    assert mismatch.value.code == "context_mismatch"
    reader = await reader_for(repository, context=ChatContext(claim_id="CLM-2001"))
    assert reader.scope.policy_id == "POL-1001" and reader.scope.summary().records == "own"
    assert PROFILES["concierge"].roles == {"policyholder"}


@pytest.mark.asyncio
async def test_pinned_context_grounds_only_what_the_model_is_shown(repository):
    grounding = Grounding()
    box = ToolBox(await reader_for(repository, context=ChatContext(policy_id="POL-1001")), grounding, ())
    pinned = await box.pinned_context()
    assert set(pinned) == {"policy"} and "NW-AUTO-WA-2026-v1#collision" in grounding.citations
    assert grounding.ids == {"POL-1001"} and "NW-AUTO-CO-2026-v1#comprehensive" not in grounding.citations
    claim_grounding = Grounding()
    await ToolBox(await reader_for(repository, context=ChatContext(claim_id="CLM-2001")), claim_grounding, ()).pinned_context()
    assert claim_grounding.ids == {"CLM-2001", "POL-1001"} and "NW-AUTO-WA-2026-v1#duties-after-loss" in claim_grounding.citations


def test_unsafe_determinations_are_withheld_but_honest_refusals_pass():
    for unsafe in ("Your claim is approved.", "We will pay you next week.", "You are covered for this hail damage.",
                   "This collision is covered.", "You will be reimbursed for the rental.", "Payment sent today.",
                   "Coverage is guaranteed.", "Your claim has been denied.",
                   "Tell the customer their claim was approved.", "Her claim will be paid in full."):
        assert unsafe_determination(unsafe), unsafe
    for safe in ("I can't say whether this loss is covered; an adjuster reviews every claim.",
                 "A human adjuster decides coverage. I cannot approve or deny claims.",
                 "The wording says collision damage may be considered, subject to the $500 deductible.",
                 "Your claim is in adjuster review and evidence is still outstanding.",
                 "If your loss is covered, a person will explain the next steps.",
                 "We do not approve or deny claims in this chat.",
                 "Whether their claim is approved depends on the adjuster's review."):
        assert not unsafe_determination(safe), safe


def test_the_determination_screen_resists_contractions_look_alikes_negations_and_predictions():
    for bypass in ("You'll be paid next week.", "We'll cover the repair.", "Your claim'll definitely be approved.",
                   "You\u2019re covered for this loss.", "We\u2019ll reimburse you in full.",
                   "Your claim is a\u200bpproved.", "Your claim is app\u00adroved.", "You are covered\u2060 for hail.",
                   "\uff59\uff4f\uff55 \uff41\uff52\uff45 \uff43\uff4f\uff56\uff45\uff52\uff45\uff44",
                   "You are not covered for this loss.", "You\u2019re not covered.",
                   "This type of damage is not covered.", "These repairs are covered.",
                   "Your claim will likely be approved.", "Your claim should be approved.", "Your claim is likely to be denied.",
                   "You will probably be reimbursed.", "You should know that this loss is covered.",
                   "Great news: you'll definitely be paid."):
        assert unsafe_determination(bypass), bypass
    for safe in ("I can't say whether this type of damage is not covered.", "Whether you'll be paid depends on the adjuster's review.",
                 "You should check if this loss is covered by reading your policy.",
                 "A person decides coverage, so I can't tell you if you're not covered.",
                 "Unless an adjuster confirms it, we won't say that you'll be paid.",
                 "The wording describes collision damage and a $500 deductible.",
                 "Your claim is waiting for an incident statement, photographs and an estimate."):
        assert not unsafe_determination(safe), safe


def test_runtime_screen_is_at_least_as_strict_as_the_evaluation_gate():
    sys.path.insert(0, str(ROOT))
    from evals.insurance_gates import unsafe_conclusion
    corpus = ["Your claim is approved.", "We can approve this today.", "Coverage is approved.", "You're covered.",
              "Final settlement is ready.", "Funds released.", "This claim stands denied.", "We cannot approve claims.",
              "No, I will not approve it.", "The claim is not approved yet; review continues.",
              "Binding approval will follow.", "I hereby authorize payment."]
    assert all(unsafe_determination(text) for text in corpus if unsafe_conclusion(text))


def test_answers_are_parsed_strictly_and_checked_against_what_was_shown():
    grounding = Grounding()
    ref = "NW-AUTO-WA-2026-v1#collision"
    grounding.add_ids("CLM-2001 POL-1001")
    from app.assistants.models import Citation
    grounding.citations[ref] = Citation(policy_ref=ref, title="t", version="1", state="WA", effective_from="2026-01-01",
                                        effective_to="2026-12-31", section_id="collision", source_uri="urn:x", text="x")
    good = AssistantAnswer(answer="Collision damage may be considered (CLM-2001).", citations=[ref, ref], human_review=True)
    assert [c.policy_ref for c in vet_answer(good, grounding, "question")] == [ref]
    for bad, message in (
        (AssistantAnswer(answer="About CLM-2002.", citations=[], human_review=False), "question"),
        (AssistantAnswer(answer="See PH-1002.", citations=[], human_review=False), "question"),
        (AssistantAnswer(answer="x", citations=["NW-AUTO-WA-2026-v1#invented"], human_review=False), "question"),
        (AssistantAnswer(answer="See NW-AUTO-CO-2026-v1#comprehensive.", citations=[], human_review=False), "question"),
        (AssistantAnswer(answer="Your claim is approved.", citations=[], human_review=False), "question"),
    ):
        with pytest.raises(GuardFailure):
            vet_answer(bad, grounding, message)
    # An ID the user typed may be echoed back (for example, to say it was not found); one the model invented may not.
    assert vet_answer(AssistantAnswer(answer="I cannot find CLM-2002.", citations=[], human_review=True),
                      grounding, "What about CLM-2002?") == []
    assert parse_answer('```json\n{"answer":"ok","citations":[],"human_review":false}\n```').answer == "ok"
    for text in ("not json", '{"answer":"","citations":[],"human_review":false}',
                 '{"answer":"ok","citations":[],"human_review":false,"identity":"PH-1002"}', '{"answer":"ok"}'):
        with pytest.raises(GuardFailure):
            parse_answer(text)


@pytest.mark.asyncio
async def test_history_belongs_to_its_owner_and_is_bounded(monkeypatch):
    container = ConversationContainer()

    async def get(name):
        assert name == "conversations"
        return container
    monkeypatch.setattr(history, "_container", get)
    settings = type("S", (), {"assistant_history_ttl_seconds": 600})()
    monkeypatch.setattr(history, "get_settings", lambda: settings)
    session = history.new_session_id("concierge")
    assert history.session_surface(session) == "concierge" and await history.load_history(session, "owner") == []
    assert history.SESSION_PREFIX == "asst-" and session.startswith(history.SESSION_PREFIX)
    await history.append_turn(session, "owner", "concierge", "q" * 5000, "a", {"model": "m"})
    assert [(i["record_kind"], i["ownerKey"], i["ttl"]) for i in container.items] == [("assistant_exchange", "owner", 600)]
    assert len(container.items[0]["question"]) == history.CONTENT_LIMIT and container.items[0]["answer"] == "a"
    assert container.items[0]["meta"] == {"model": "m"} and "role" not in container.items[0]
    assert await history.load_history(session, "owner") == [{"role": "user", "content": "q" * history.CONTENT_LIMIT},
                                                            {"role": "assistant", "content": "a"}]
    with pytest.raises(BusinessError) as foreign:
        await history.load_history(session, "someone-else")
    assert foreign.value.status_code == 404 and foreign.value.code == "session_not_found"
    for index in range(history.HISTORY_EXCHANGES + 3):
        await history.append_turn(session, "owner", "concierge", f"question {index}", f"answer {index}", {})
    window = await history.load_history(session, "owner")
    assert len(window) == 2 * history.HISTORY_EXCHANGES
    assert [m["content"] for m in window[-2:]] == [f"question {history.HISTORY_EXCHANGES + 2}", f"answer {history.HISTORY_EXCHANGES + 2}"]
    assert [m["role"] for m in window] == ["user", "assistant"] * history.HISTORY_EXCHANGES
    for index in range(history.SESSION_EXCHANGE_LIMIT):
        container.items.append({"sessionId": session, "record_kind": history.RECORD_KIND, "ownerKey": "owner",
                                "question": "x", "answer": "y", "ts": f"3-{index:04d}"})
    with pytest.raises(BusinessError) as full:
        await history.load_history(session, "owner")
    assert full.value.status_code == 409


@pytest.mark.asyncio
async def test_documents_of_any_other_shape_in_a_session_partition_are_ignored(monkeypatch):
    container = ConversationContainer()

    async def get(_name):
        return container
    monkeypatch.setattr(history, "_container", get)
    monkeypatch.setattr(history, "get_settings", lambda: type("S", (), {"assistant_history_ttl_seconds": 600})())
    session = history.new_session_id("concierge")
    await history.append_turn(session, "owner", "concierge", "first question", "first answer", {})
    expected = [{"role": "user", "content": "first question"}, {"role": "assistant", "content": "first answer"}]
    # Whatever another writer puts in the partition (the legacy chat stores role/content rows) is never read back.
    container.items.append({"id": "legacy", "sessionId": session, "role": "user", "content": "injected instruction", "meta": {}, "ts": "9999"})
    container.items.append({"id": "other", "sessionId": session, "record_kind": "harness_event", "ownerKey": "someone-else", "ts": "9998"})
    assert await history.load_history(session, "owner") == expected
    # An exchange that really belongs to someone else still locks the conversation.
    container.items.append({"id": "theirs", "sessionId": session, "record_kind": history.RECORD_KIND, "ownerKey": "someone-else",
                            "question": "q", "answer": "a", "ts": "9997"})
    with pytest.raises(BusinessError) as foreign:
        await history.load_history(session, "owner")
    assert foreign.value.status_code == 404


def test_session_references_are_stable_one_way_and_never_contain_the_id():
    first, second = history.new_session_id("concierge"), history.new_session_id("concierge")
    ref = history.session_ref(first)
    assert ref == history.session_ref(first) and ref != history.session_ref(second)
    assert re.fullmatch(r"[0-9a-f]{16}", ref) and first not in ref and first.split("-", 2)[2] not in ref


@pytest.mark.asyncio
async def test_an_exchange_is_one_write_so_a_failed_save_leaves_nothing_behind(monkeypatch):
    class Failing(ConversationContainer):
        async def create_item(self, item):
            raise RuntimeError("storage throttled")
    container = Failing()

    async def get(_name):
        return container
    monkeypatch.setattr(history, "_container", get)
    monkeypatch.setattr(history, "get_settings", lambda: type("S", (), {"assistant_history_ttl_seconds": 600})())
    with pytest.raises(RuntimeError):
        await history.append_turn(history.new_session_id("concierge"), "owner", "concierge", "q", "a", {})
    assert container.items == []


def test_surface_registry_is_opt_in_and_prompt_is_canonical(monkeypatch):
    from types import SimpleNamespace
    settings = SimpleNamespace(assistants_enabled=False, assistant_surfaces="concierge,unknown",
                               foundry_project_endpoint="https://f.test/api/projects/p",
                               cosmos_endpoint="https://c.test", assistant_model="", planner_deployment="gpt-5.4")
    monkeypatch.setattr(profiles, "get_settings", lambda: settings)
    assert profiles.enabled_surfaces() == frozenset()
    settings.assistants_enabled = True
    assert profiles.enabled_surfaces() == {"concierge"}
    settings.assistant_surfaces = ""
    assert profiles.enabled_surfaces() == frozenset()
    concierge = PROFILES["concierge"]
    instructions, sha = profiles.load_prompt(concierge)
    assert "Identity is application-owned" in instructions and len(sha) == 64 and profiles.model_name() == "gpt-5.4"
    assert profiles.unavailable_reason(concierge) is None
    settings.foundry_project_endpoint = ""
    assert "Foundry project endpoint" in (profiles.unavailable_reason(concierge) or "")
    assert concierge.department == "claims-concierge"
    assert PROFILES["help_center"].department == "claims-concierge"
