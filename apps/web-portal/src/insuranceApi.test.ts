import { afterEach, describe, expect, it, vi } from "vitest";
import { actionDraft, defaultDraftReferences, draftInsuranceAction, getInsuranceFixtures, parseInsuranceFixtures, streamInsuranceChat } from "./insuranceApi";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import InsuranceEndorsement from "./components/InsuranceEndorsement";
import canonicalFixture from "../../../data/northwind/insurance-fixtures.json";

const claim = {
  claim_id: "CLM-2001", policy_id: "POL-1001", policyholder_id: "PH-1001",
  incident_type: "auto_collision", loss_date: "2026-09-18", status: "awaiting_evidence",
  description: "Synthetic collision report", outstanding_evidence: ["Repair estimate"],
};
const fixture = {
  business_domain: "insurance", agent_runtime_ready: false,
  policyholders: [{ policyholder_id: "PH-1001", name: "Demo Person", email: "demo@example.test", phone: "+1-312-555-0100" }],
  policies: [{
    policy_id: "POL-1001", policyholder_id: "PH-1001", status: "active",
    effective_from: "2026-01-01", effective_to: "2026-12-31", state: "WA",
    insured_asset: { year: 2022, make: "Demo", model: "Sedan" },
    coverages: [{ coverage_type: "collision", limit_usd: 30000, deductible_usd: 500 }], endorsements: ["Synthetic rental endorsement"],
  }],
  claims: [claim],
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
afterEach(() => vi.unstubAllGlobals());

describe("insurance records and draft contract", () => {
  it("accepts the canonical backend fixture and preserves versioned section citations", () => {
    const data = parseInsuranceFixtures({ ...canonicalFixture, agent_runtime_ready: false });
    expect(data.claims[0].claim_id).toBe("CLM-2001");
    expect(data.policy_wordings?.[0].sections[0].policy_ref).toBe("NW-AUTO-WA-2026-v1#collision");
    expect(data.policies[0].wording_id).toBe("NW-AUTO-WA-2026-v1");
  });
  it("keeps endorsement limitations and citations visible without treating them as coverage decisions", () => {
    const data = parseInsuranceFixtures({ ...canonicalFixture, agent_runtime_ready: false });
    const rental = renderToStaticMarkup(createElement(InsuranceEndorsement, { endorsement: data.policies[0].endorsements[0] }));
    expect(rental).toContain("NW-AUTO-WA-2026-v1#rental-reimbursement");
    expect(rental).toContain("$30 per day");
    expect(rental).toContain("Version 1");
    expect(rental).toContain("does not establish applicability or approve a claim");
    const glass = renderToStaticMarkup(createElement(InsuranceEndorsement, { endorsement: data.policies[1].endorsements[0] }));
    expect(glass).toContain("NW-AUTO-CO-2026-v1#glass");
    expect(glass).toContain("$100 glass-only deductible");
    expect(glass).toContain("must not be applied to body-panel hail damage");
    expect(renderToStaticMarkup(createElement(InsuranceEndorsement, { endorsement: "Legacy synthetic endorsement" }))).toBe("Legacy synthetic endorsement");
  });
  it("does not preselect rental or glass endorsements for the collision and body-hail claims", () => {
    const data = parseInsuranceFixtures({ ...canonicalFixture, agent_runtime_ready: false });
    expect(defaultDraftReferences(data.claims[0], data.policy_wordings?.[0])).toEqual([
      "NW-AUTO-WA-2026-v1#collision", "NW-AUTO-WA-2026-v1#duties-after-loss",
    ]);
    expect(defaultDraftReferences(data.claims[1], data.policy_wordings?.[1])).toEqual([
      "NW-AUTO-CO-2026-v1#comprehensive", "NW-AUTO-CO-2026-v1#duties-after-loss",
    ]);
    expect(defaultDraftReferences(claim)).toEqual([]);
  });
  it("validates insurance records without trusting a foreign domain or malformed readiness", () => {
    expect(parseInsuranceFixtures(fixture).claims[0].claim_id).toBe("CLM-2001");
    expect(() => parseInsuranceFixtures({ ...fixture, business_domain: "other" })).toThrow("Invalid insurance");
    expect(() => parseInsuranceFixtures({ ...fixture, agent_runtime_ready: "true" })).toThrow("Invalid insurance");
    expect(() => parseInsuranceFixtures({ ...fixture, claims: [{ ...claim, policy_id: "missing" }] })).toThrow("inconsistent identifiers");
    expect(() => parseInsuranceFixtures({ ...fixture, policies: [{ ...fixture.policies[0], coverages: [{ coverage_type: "collision", limit_usd: -1, deductible_usd: 0 }] }] })).toThrow("Invalid insurance");
  });
  it("surfaces fixture errors without fabricated fallback records", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ detail: "Fixtures unavailable" }, 503)));
    await expect(getInsuranceFixtures()).rejects.toThrow("Fixtures unavailable");
  });
  it("builds only claim-linked review drafts and normalizes citations", () => {
    expect(actionDraft(claim, "request_evidence", "  Ask for an estimate. ", "NW-AUTO-WA-2026-v1#collision, NW-AUTO-WA-2026-v1#duties-after-loss\nNW-AUTO-WA-2026-v1#collision", "")).toEqual({
      claim_id: "CLM-2001", policy_id: "POL-1001", policyholder_id: "PH-1001",
      action_type: "request_evidence", summary: "Ask for an estimate.",
      policy_refs: ["NW-AUTO-WA-2026-v1#collision", "NW-AUTO-WA-2026-v1#duties-after-loss"],
    });
    expect(actionDraft(claim, "settlement_recommendation", "Review only", "NW-AUTO-WA-2026-v1#collision", "1250.50").amount_usd).toBe(1250.5);
    expect(() => actionDraft(claim, "customer_update", "Draft", "", "100")).toThrow("only allowed");
    for (const amount of ["-1", "NaN", "Infinity", "0", "1.234"]) {
      expect(() => actionDraft(claim, "settlement_recommendation", "Draft", "", amount)).toThrow("positive USD");
    }
    expect(() => actionDraft(claim, "request_evidence", " ", "", "")).toThrow("Describe");
  });
  it("posts the exact draft contract without calling approve or pay routes", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ status: "pending_approval", hitl_id: "draft-1", workflow_id: "workflow-1" }));
    vi.stubGlobal("fetch", fetch);
    const draft = actionDraft(claim, "assign_adjuster", "Propose an adjuster handoff", "NW-AUTO-WA-2026-v1#collision", "");
    expect((await draftInsuranceAction(draft)).hitl_id).toBe("draft-1");
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/insurance\/actions\/draft$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual(draft);
  });
  it("warns when an unverified draft response may already have saved", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ status: "ok" })));
    await expect(draftInsuranceAction(actionDraft(claim, "request_evidence", "Draft", "", ""))).rejects.toThrow("Check Approvals before retrying");
  });
  it("requires source citations and an amount on recommendations", () => {
    expect(() => actionDraft(claim, "settlement_recommendation", "Review only", "", "100")).toThrow("versioned policy citations");
    expect(() => actionDraft(claim, "settlement_recommendation", "Review only", "source#collision", "")).toThrow("positive proposed amount");
  });
});

describe("insurance runtime isolation", () => {
  async function collect() {
    const events = [];
    for await (const event of streamInsuranceChat(null, "Review CLM-2001")) events.push(event);
    return events;
  }
  it("never sends an insurance prompt while the agent runtime is not ready", async () => {
    const fetch = vi.fn().mockResolvedValue(json(fixture));
    vi.stubGlobal("fetch", fetch);
    await expect(collect()).rejects.toThrow("Insurance agents are not ready");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/insurance\/fixtures$/);
  });
  it("only uses insurance chat when ready and retains backend 503 explanations", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ ...fixture, agent_runtime_ready: true }))
      .mockResolvedValueOnce(json({ detail: "Agent deployment is not ready" }, 503));
    vi.stubGlobal("fetch", fetch);
    await expect(collect()).rejects.toThrow("Agent deployment is not ready");
    expect(fetch.mock.calls[1][0]).toMatch(/\/insurance\/chat$/);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ message: "Review CLM-2001" });
  });
  it("decodes NDJSON including a trailing event without a newline", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ ...fixture, agent_runtime_ready: true }))
      .mockResolvedValueOnce(new Response('{"kind":"session","session_id":"insurance-1"}\n{"kind":"assistant_message","text":"Review required"}'));
    vi.stubGlobal("fetch", fetch);
    expect(await collect()).toEqual([{ kind: "session", session_id: "insurance-1" }, { kind: "assistant_message", text: "Review required" }]);
  });
});
