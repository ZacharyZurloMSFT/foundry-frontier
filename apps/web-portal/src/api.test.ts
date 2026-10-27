import { afterEach, describe, expect, it, vi } from "vitest";
import { decideHitl, deleteReviewedDraft, getHitl, getReviewedDrafts } from "./api";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const draft = {
  id: "hitl-1", workflowId: "wf-1", status: "approved",
  payload: { claim_id: "CLM-2001", policy_id: "POL-1001", policyholder_id: "PH-1001", action_type: "request_evidence", summary: "Ask for an estimate", policy_refs: [] },
};
afterEach(() => vi.unstubAllGlobals());

describe("human-review queue routes", () => {
  it("lists pending approvals from /hitl", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ items: [draft] }));
    vi.stubGlobal("fetch", fetch);
    expect((await getHitl()).items[0].payload.claim_id).toBe("CLM-2001");
    expect(fetch.mock.calls[0][0]).toMatch(/\/hitl$/);
  });

  it("lists reviewed drafts from the canonical /hitl/resolved route", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ items: [draft] }));
    vi.stubGlobal("fetch", fetch);
    expect((await getReviewedDrafts()).items).toEqual([draft]);
    expect(fetch.mock.calls[0][0]).toMatch(/\/hitl\/resolved$/);
  });

  it("surfaces a failed reviewed-drafts request", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 503)));
    await expect(getReviewedDrafts()).rejects.toThrow("Reviewed drafts failed (503)");
  });

  it("deletes a reviewed draft through DELETE /hitl/{id} with the workflow partition key", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ status: "ok" }));
    vi.stubGlobal("fetch", fetch);
    await deleteReviewedDraft("hitl/1", "wf 1");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/hitl\/hitl%2F1\?workflow_id=wf%201$/);
    expect(init.method).toBe("DELETE");
  });

  it("posts the decision to /hitl/decide and keeps the backend error detail", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ ...draft, chat_message: null, session_id: null }))
      .mockResolvedValueOnce(json({ detail: "This draft has already been decided." }, 409));
    vi.stubGlobal("fetch", fetch);
    expect((await decideHitl("hitl-1", "wf-1", "approve")).status).toBe("approved");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/hitl\/decide$/);
    expect(JSON.parse(init.body)).toEqual({ hitl_id: "hitl-1", workflow_id: "wf-1", decision: "approve", note: "" });
    await expect(decideHitl("hitl-1", "wf-1", "reject")).rejects.toThrow("already been decided");
  });
});
