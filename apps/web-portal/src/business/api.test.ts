import { afterEach, describe, expect, it, vi } from "vitest";
import { BusinessApi, BusinessError } from "./api";
import { claimExample, configExample, identityExample, policyExample, reportExample } from "./testData";
import { claimStatuses, nextStatuses, parseClaim, parseIdentity, parsePolicy } from "./models";
import { businessTheme, readBusinessConfig } from "./config";

const json = (body: unknown, status = 200, headers?: HeadersInit) => new Response(JSON.stringify(body), { status, headers });
const makeApi = () => new BusinessApi(configExample, vi.fn().mockResolvedValue("test-access-token"));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("connected business API", () => {
  it("sends real token and configured run headers only to versioned protected paths", async () => {
    const fetch = vi.fn().mockResolvedValue(json(identityExample)); vi.stubGlobal("fetch", fetch);
    expect(await makeApi().me()).toEqual(identityExample);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/v1/me");
    expect(url).not.toContain("token");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer test-access-token");
    expect(new Headers(init.headers).get("X-Demo-Run")).toBe("test-run");
    expect(init.credentials).toBe("omit"); expect(init.redirect).toBe("error");
  });
  it("supports explicit next pages for both full policy snapshots and claims", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ items: [policyExample], next_offset: 20 }))
      .mockResolvedValueOnce(json({ items: [claimExample], next_offset: null }));
    vi.stubGlobal("fetch", fetch); const api = makeApi();
    expect((await api.policies()).next_offset).toBe(20); expect((await api.claims(20)).items[0]).toEqual(claimExample);
    expect(fetch.mock.calls.map(c => c[0])).toEqual(["http://localhost:8000/api/v1/me/policies?offset=0&limit=20", "http://localhost:8000/api/v1/me/claims?offset=20&limit=20"]);
  });
  it("posts the exact loss contract with a stable explicit idempotency header", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ claim: claimExample, replayed: false }, 201, { ETag: '"1"' }));
    vi.stubGlobal("fetch", fetch); const api = makeApi();
    expect((await api.create(reportExample, "command-key")).claim.claim_id).toBe("CLM-TEST");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/api\/v1\/claims$/); expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("command-key");
    expect(JSON.parse(init.body)).toEqual(reportExample);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("rejects a mutation response for a different case in the same run", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ ...claimExample, claim_id: "CLM-OTHER" })));
    await expect(makeApi().note("CLM-TEST", 1, "A note", "internal")).rejects.toMatchObject({
      code: "invalid_response", uncertain: true,
    });
  });
  it("uses only explicit versioned manual case commands", async () => {
    const fetch = vi.fn().mockImplementation(async () => json({ ...claimExample, version: 2 }, 200, { ETag: '"2"' }));
    vi.stubGlobal("fetch", fetch); const api = makeApi();
    await api.transition("CLM-TEST", 1, "evidence_requested", "Need estimate");
    await api.task("CLM-TEST", 2, "Estimate", "repair_estimate", "adjuster-a");
    await api.complete("CLM-TEST", "task-a", 3);
    await api.note("CLM-TEST", 4, "Synthetic review note", "customer");
    await api.assign("CLM-TEST", 5, "adjuster-a");
    expect(fetch.mock.calls.map(c => [c[0].split("/api/v1")[1], c[1].method, JSON.parse(c[1].body)])).toEqual([
      ["/claims/CLM-TEST/transitions", "POST", { target: "evidence_requested", expected_version: 1, reason: "Need estimate" }],
      ["/claims/CLM-TEST/tasks", "POST", { expected_version: 2, title: "Estimate", requested_evidence: "repair_estimate", assigned_to: "adjuster-a" }],
      ["/claims/CLM-TEST/tasks/task-a", "PATCH", { expected_version: 3, status: "completed" }],
      ["/claims/CLM-TEST/notes", "POST", { expected_version: 4, body: "Synthetic review note", visibility: "customer" }],
      ["/claims/CLM-TEST/assignment", "POST", { expected_version: 5, adjuster_id: "adjuster-a" }],
    ]);
    expect(fetch.mock.calls.every(c => !/fixture|agent|draft|approve|pay|settle/.test(c[0]))).toBe(true);
  });
  it.each([401, 403, 409, 503])("preserves HTTP %i and support references without automatic retries", async status => {
    const fetch = vi.fn().mockResolvedValue(json({ error: { code: "service_code", message: "Connected service message", correlation_id: "body-ref" } }, status, { "X-Correlation-ID": "header-ref" }));
    vi.stubGlobal("fetch", fetch);
    await expect(makeApi().note("CLM-TEST", 1, "note", "internal")).rejects.toMatchObject({
      status, code: "service_code", message: "Connected service message", correlationId: "header-ref", uncertain: status >= 500,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("fails closed for malformed successes/errors, wrong runs, identifiers and ETags", async () => {
    const responses = [
      json({ ...identityExample, run_id: "other-run" }), json({ items: [policyExample], next_offset: "20" }),
      json({ ...claimExample, synthetic: false }), json({ ...claimExample, claim_id: "other-claim" }),
      json(claimExample, 200, { ETag: '"999"' }), json({ claim: claimExample, replayed: false }, 201, { ETag: '"999"' }),
      json({ error: { code: 42, message: "wrong", correlation_id: 1 } }, 403),
    ];
    const fetch = vi.fn(); responses.forEach(r => fetch.mockResolvedValueOnce(r)); vi.stubGlobal("fetch", fetch);
    const api = makeApi();
    for (const action of [() => api.me(), () => api.policies(), () => api.claim("CLM-TEST"), () => api.claim("CLM-TEST"), () => api.claim("CLM-TEST"), () => api.create(reportExample, "key"), () => api.me()]) {
      await expect(action()).rejects.toBeInstanceOf(BusinessError);
    }
  });
  it("never posts if token acquisition needs interaction, and marks network-write outcomes uncertain", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const api = new BusinessApi(configExample, async () => { throw new Error("Interaction required"); });
    await expect(api.create(reportExample, "key")).rejects.toThrow("Interaction required");
    expect(fetch).not.toHaveBeenCalled();
    fetch.mockRejectedValue(new TypeError("connection ended"));
    await expect(makeApi().create(reportExample, "key")).rejects.toMatchObject({ uncertain: true, status: 0 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
describe("business contract validation", () => {
  it("validates all nested records instead of casting server JSON", () => {
    expect(parseClaim(claimExample)).toEqual(claimExample); expect(parsePolicy(policyExample)).toEqual(policyExample);
    expect(parseIdentity(identityExample)).toEqual(identityExample);
    for (const value of [
      { ...claimExample, tasks: [{}] }, { ...claimExample, notes: [{ body: 3 }] },
      { ...claimExample, timeline: [{ ...claimExample.timeline[0], visibility: "all" }] },
      { ...claimExample, status: "approved" }, { ...claimExample, version: 1.5 }, { ...claimExample, version: 0 },
      { ...claimExample, policyholder_id: "other-person" }, { ...claimExample, outstanding_evidence: [false] },
    ]) expect(() => parseClaim(value)).toThrow("Invalid business API response");
    expect(() => parsePolicy({ ...policyExample, policy: { ...policyExample.policy, coverages: [{ coverage_type: "collision", limit_usd: -1, deductible_usd: 0 }] } })).toThrow();
    expect(() => parseIdentity({ ...identityExample, role: "admin" })).toThrow();
    expect(() => parseIdentity({ ...identityExample, adjusters: ["private-employee"] })).toThrow();
  });
  it("offers only the bounded manual workflow, including returns for more evidence", () => {
    expect(claimStatuses).toEqual(["reported", "evidence_requested", "evidence_received", "adjuster_review", "customer_update_prepared"]);
    expect(nextStatuses("reported")).toEqual(["evidence_requested"]);
    expect(nextStatuses("evidence_requested")).toEqual(["evidence_received"]);
    expect(nextStatuses("evidence_received")).toEqual(["adjuster_review", "evidence_requested"]);
    expect(nextStatuses("adjuster_review")).toEqual(["customer_update_prepared", "evidence_requested"]);
    expect(nextStatuses("customer_update_prepared")).toEqual(["evidence_requested"]);
  });
  it("rejects missing/invalid config without constructing a fake identity", () => {
    expect(readBusinessConfig({}).ok).toBe(false);
    const env = { VITE_INSURANCE_AUTH_TENANT_ID: configExample.tenantId, VITE_INSURANCE_AUTH_CLIENT_ID: configExample.clientId,
      VITE_INSURANCE_AUTH_SCOPE: configExample.scope, VITE_INSURANCE_RUN_ID: configExample.runId };
    expect(readBusinessConfig(env)).toEqual({ ok: true, value: configExample });
    for (const extra of [
      { VITE_INSURANCE_AUTH_TENANT_ID: "common" }, { VITE_INSURANCE_AUTH_SCOPE: "access_as_user" },
      { VITE_INSURANCE_RUN_ID: "" }, { VITE_API_BASE_URL: "https://user:secret@api.example.test" },
      { VITE_API_BASE_URL: "https://api.example.test?token=secret" }, { VITE_API_BASE_URL: "http://api.example.test" },
    ]) expect(readBusinessConfig({ ...env, ...extra }).ok).toBe(false);
    expect(readBusinessConfig({ ...env, VITE_INSURANCE_RUN_ID: "not.allowed" }).ok).toBe(false);
    expect(readBusinessConfig({ ...env, VITE_INSURANCE_RUN_ID: "a".repeat(65) }).ok).toBe(false);
    expect(readBusinessConfig({ ...env, VITE_INSURANCE_RUN_ID: "_run-a" }).ok).toBe(true);
  });
  it("honours explicit light/dark themes instead of treating either string as dark", () => {
    expect(businessTheme("light", true)).toBe("light");
    expect(businessTheme("dark", false)).toBe("dark");
    expect(businessTheme(null, false)).toBe("light");
    expect(businessTheme("invalid", true)).toBe("dark");
  });
});
