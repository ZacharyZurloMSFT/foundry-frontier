import { describe, expect, it } from "vitest";
import { parseHarnessEvent, readHarnessStream } from "./harnessApi";

const base = { run_id: "harness-test", seq: 1, ts: "2026-01-01T00:00:00Z" };
function bytes(...chunks: Uint8Array[]) {
  return new ReadableStream<Uint8Array>({
    start(controller) { for (const chunk of chunks) controller.enqueue(chunk); controller.close(); },
  });
}
async function collect(stream: ReadableStream<Uint8Array>) {
  const events = [];
  for await (const event of readHarnessStream(stream)) events.push(event);
  return events;
}
describe("harness stream contract", () => {
  it("decodes split unicode and a trailing line without a newline", async () => {
    const encoded = new TextEncoder().encode(
      JSON.stringify({ ...base, kind: "assistant_delta", text: "caf\u00e9" }) + "\n" +
      JSON.stringify({ ...base, seq: 2, kind: "done", status: "succeeded" }),
    );
    const result = await collect(bytes(...Array.from(encoded, (n) => new Uint8Array([n]))));
    expect(result[0]).toMatchObject({ text: "caf\u00e9" });
    expect(result[1].kind).toBe("done");
  });
  it("does not turn a truncated stream into success", async () => {
    const encoded = new TextEncoder().encode(JSON.stringify({ ...base, kind: "assistant_delta", text: "partial" }));
    await expect(collect(bytes(encoded))).rejects.toThrow("Stream interrupted");
  });
  it("rejects unknown tools and malformed events", () => {
    expect(() => parseHarnessEvent({ ...base, kind: "tool_start", tool: "shell", call_id: "x", text: "" })).toThrow("Unknown specialist");
    expect(() => parseHarnessEvent({ kind: "done", status: "succeeded" })).toThrow();
    expect(() => parseHarnessEvent({ ...base, kind: "done", status: "maybe" })).toThrow("Unknown harness");
  });
  it("preserves insurance tool and pending-only approval metadata", () => {
    expect(parseHarnessEvent({ ...base, kind: "tool_start", tool: "policy_claim_lookup",
      call_id: "lookup", text: "", duration_ms: null })).toMatchObject({ tool: "policy_claim_lookup" });
    expect(parseHarnessEvent({ ...base, kind: "tool_result", tool: "claim_action_draft",
      call_id: "draft", text: "pending", duration_ms: 1 })).toMatchObject({ tool: "claim_action_draft" });
    const approval = { ...base, kind: "approval_pending", hitl_id: "hitl-test", workflow_id: "wf-test",
      draft: { claim_id: "CLM-2001" }, action_kind: "insurance_claim_action", execution_status: "not_executed" };
    expect(parseHarnessEvent(approval)).toMatchObject({
      action_kind: "insurance_claim_action", execution_status: "not_executed",
    });
    expect(() => parseHarnessEvent({ ...approval, execution_status: "paid" })).toThrow("execution status");
    expect(() => parseHarnessEvent({ ...approval, action_kind: "approve_claim" })).toThrow("approval kind");
  });
  it("rejects events after a terminal event", async () => {
    const event = JSON.stringify({ ...base, kind: "done", status: "failed" });
    await expect(collect(bytes(new TextEncoder().encode(`${event}\n${event}\n`)))).rejects.toThrow("after completion");
  });
});
