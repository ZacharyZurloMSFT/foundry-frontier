import { apiFetch } from "./api";

export type RunState = "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
export type Specialist = "triage" | "order_lookup" | "policy_lookup" | "resolution_draft" | "policy_claim_lookup" | "claim_action_draft";
interface EventBase { run_id: string; seq: number; ts: string }
export type HarnessEvent = EventBase & (
  | { kind: "run_started"; workflow_id: string; model: string; prompt_sha: string }
  | { kind: "assistant_delta" | "assistant_message"; text: string }
  | { kind: "tool_start" | "tool_result" | "tool_error"; tool: Specialist; call_id: string; text: string; duration_ms: number | null }
  | { kind: "usage"; source: string; model: string | null; input_tokens: number | null; output_tokens: number | null }
  | { kind: "approval_pending"; hitl_id: string; workflow_id: string; draft: Record<string, unknown>; action_kind?: "resolution" | "insurance_claim_action"; execution_status?: "not_executed" }
  | { kind: "error"; code: string; message: string }
  | { kind: "done"; status: RunState }
);

export interface HarnessStatus {
  enabled: boolean;
  state: string;
  message: string;
  sdk_version: string | null;
  runtime_version: string | null;
  model: string;
  provider: string;
  max_tool_calls: number;
  timeout_seconds: number;
}
export interface SavedApproval {
  id: string; workflowId: string; status: string; payload: Record<string, unknown>;
  kind?: string; business_domain?: string; synthetic?: boolean;
  execution_status?: "not_executed"; review_scope?: string;
}
export interface HarnessRun {
  run_id: string; workflow_id: string; status: RunState; input: string; output: string;
  error: string | null; model: string; prompt_sha: string; created_at: string;
  events: HarnessEvent[]; approval: SavedApproval | null;
}
export class ExistingRunError extends Error {
  constructor(public runId: string) { super("This request already exists. Loading its saved run."); }
}
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid harness response.");
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid harness text field.");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error("Invalid harness numeric field.");
  return value;
}
function nullableNumber(value: unknown): number | null { return value == null ? null : number(value); }
function nullableText(value: unknown): string | null { return value == null ? null : text(value); }
function state(value: unknown): RunState {
  if (value === "running" || value === "succeeded" || value === "failed" || value === "cancelled" || value === "interrupted") return value;
  throw new Error("Unknown harness run state.");
}
function specialist(value: unknown): Specialist {
  if (value === "triage" || value === "order_lookup" || value === "policy_lookup" || value === "resolution_draft" ||
      value === "policy_claim_lookup" || value === "claim_action_draft") return value;
  throw new Error("Unknown specialist in harness response.");
}
function actionKind(value: unknown): "resolution" | "insurance_claim_action" | undefined {
  if (value === undefined) return undefined;
  if (value === "resolution" || value === "insurance_claim_action") return value;
  throw new Error("Unknown harness approval kind.");
}
function executionStatus(value: unknown): "not_executed" | undefined {
  if (value === undefined) return undefined;
  if (value === "not_executed") return value;
  throw new Error("Unexpected harness execution status.");
}
function optionalBoolean(value: unknown): boolean | undefined {
  if (value === undefined) return undefined;
  if (typeof value === "boolean") return value;
  throw new Error("Invalid harness provenance.");
}
export function parseHarnessEvent(value: unknown): HarnessEvent {
  const v = record(value);
  const base = { run_id: text(v.run_id), seq: number(v.seq), ts: text(v.ts) };
  switch (v.kind) {
    case "run_started": return { ...base, kind: v.kind, workflow_id: text(v.workflow_id), model: text(v.model), prompt_sha: text(v.prompt_sha) };
    case "assistant_delta": case "assistant_message": return { ...base, kind: v.kind, text: text(v.text) };
    case "tool_start": case "tool_result": case "tool_error":
      return { ...base, kind: v.kind, tool: specialist(v.tool), call_id: text(v.call_id), text: text(v.text), duration_ms: nullableNumber(v.duration_ms) };
    case "usage": return { ...base, kind: v.kind, source: text(v.source), model: nullableText(v.model), input_tokens: nullableNumber(v.input_tokens), output_tokens: nullableNumber(v.output_tokens) };
    case "approval_pending": return { ...base, kind: v.kind, hitl_id: text(v.hitl_id), workflow_id: text(v.workflow_id),
      draft: record(v.draft), action_kind: actionKind(v.action_kind), execution_status: executionStatus(v.execution_status) };
    case "error": return { ...base, kind: v.kind, code: text(v.code), message: text(v.message) };
    case "done": return { ...base, kind: v.kind, status: state(v.status) };
    default: throw new Error("Unknown harness stream event.");
  }
}
export async function* readHarnessStream(body: ReadableStream<Uint8Array>): AsyncGenerator<HarnessEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (buffer.length > 1024 * 1024) throw new Error("Harness event exceeds the stream limit.");
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      if (done && buffer.trim()) { lines.push(buffer); buffer = ""; }
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = parseHarnessEvent(JSON.parse(line));
        if (terminal) throw new Error("Harness sent data after completion.");
        if (event.kind === "done") terminal = true;
        yield event;
      }
      if (done) break;
    }
    if (!terminal) throw new Error("Stream interrupted. Reload the saved run before retrying.");
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
async function checked(response: Response): Promise<unknown> {
  const value: unknown = await response.json();
  if (!response.ok) {
    const detail = record(value).detail;
    if (response.status === 409 && detail && typeof detail === "object") {
      const id = record(detail).run_id;
      if (typeof id === "string") throw new ExistingRunError(id);
    }
    throw new Error(typeof detail === "string" ? detail : `Harness request failed (${response.status}).`);
  }
  return value;
}
export async function getHarnessStatus(): Promise<HarnessStatus> {
  const v = record(await checked(await apiFetch("/harness/status")));
  if (typeof v.enabled !== "boolean") throw new Error("Invalid harness availability.");
  return { enabled: v.enabled, state: text(v.state), message: text(v.message),
    sdk_version: nullableText(v.sdk_version), runtime_version: nullableText(v.runtime_version),
    model: text(v.model), provider: text(v.provider), max_tool_calls: number(v.max_tool_calls),
    timeout_seconds: number(v.timeout_seconds) };
}
export async function getHarnessRun(runId: string): Promise<HarnessRun> {
  const v = record(await checked(await apiFetch(`/harness/runs/${encodeURIComponent(runId)}`)));
  if (!Array.isArray(v.events)) throw new Error("Invalid saved event list.");
  const a = v.approval == null ? null : record(v.approval);
  return { run_id: text(v.run_id), workflow_id: text(v.workflow_id), status: state(v.status),
    input: text(v.input), output: text(v.output), error: nullableText(v.error), model: text(v.model),
    prompt_sha: text(v.prompt_sha), created_at: text(v.created_at), events: v.events.map(parseHarnessEvent),
    approval: a ? { id: text(a.id), workflowId: text(a.workflowId), status: text(a.status), payload: record(a.payload),
      kind: a.kind === undefined ? undefined : text(a.kind),
      business_domain: a.business_domain === undefined ? undefined : text(a.business_domain),
      synthetic: optionalBoolean(a.synthetic), execution_status: executionStatus(a.execution_status),
      review_scope: a.review_scope === undefined ? undefined : text(a.review_scope) } : null };
}
export async function* streamHarnessRun(requestId: string, message: string, signal: AbortSignal) {
  const response = await apiFetch("/harness/runs", {
    method: "POST", signal,
    headers: { "Content-Type": "application/json", "X-Department": "order-concierge" },
    body: JSON.stringify({ request_id: requestId, message }),
  });
  if (!response.ok) { await checked(response); return; }
  if (!response.body) throw new Error("The harness returned no stream.");
  yield* readHarnessStream(response.body);
}
