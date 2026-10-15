import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentHarnessTab, { insuranceReview } from "./AgentHarnessTab";

afterEach(() => vi.unstubAllGlobals());
describe("insurance SDK harness workspace", () => {
  it("shows grounded insurance boundaries, not a disabled retail wrapper", () => {
    vi.stubGlobal("localStorage", { getItem: () => null });
    const markup = renderToStaticMarkup(createElement(AgentHarnessTab, {
      active: true, onBack: () => {}, onApprovals: () => {},
    }));
    expect(markup).toContain("GitHub Copilot SDK orchestrates grounded insurance lookups");
    expect(markup).toContain("CLM-2001");
    expect(markup).toContain("No simulated output");
    expect(markup).not.toContain("four Foundry specialists");
    expect(markup).toContain("Opening a run never replays tools");
  });
  it("accepts only the application-bound, non-executed insurance review contract", () => {
    const saved = { id: "h", workflowId: "wf-harness-unit", kind: "insurance_claim_action",
      execution_status: "not_executed", status: "pending", payload: { claim_id: "CLM-2001" } };
    expect(insuranceReview(saved, "wf-harness-unit")?.status).toBe("pending");
    expect(insuranceReview({ ...saved, status: "approved" })?.status).toBe("approved");
    expect(insuranceReview({ ...saved, kind: "resolution" })).toBeNull();
    expect(insuranceReview({ ...saved, execution_status: "paid" })).toBeNull();
    expect(insuranceReview(saved, "wf-harness-other")).toBeNull();
    expect(insuranceReview({ ...saved, kind: undefined })).toBeNull();
    expect(insuranceReview({ kind: "approval_pending", action_kind: "insurance_claim_action",
      execution_status: "not_executed", workflow_id: "wf-harness-unit", draft: saved.payload })?.payload).toEqual(saved.payload);
  });
});
