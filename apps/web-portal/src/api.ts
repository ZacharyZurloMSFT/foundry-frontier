import { canonicalDepartmentSlug, DEFAULT_DEPARTMENT_SLUG } from "./departments";
import type { DraftRecord } from "./insuranceApi";

const API_BASE = (import.meta as any).env.VITE_API_BASE_URL || "http://localhost:8000";

/** Read the currently-active department from the same localStorage key
 *  `CompanyContext` writes. Kept plain-function so non-React modules can
 *  reach it (e.g., the trace panel poller). Always canonical: a deprecated
 *  slug persisted by an older build is mapped, never sent. */
export function currentDepartmentSlug(): string {
  try {
    return canonicalDepartmentSlug(localStorage.getItem("northwind.activeDepartment")) || DEFAULT_DEPARTMENT_SLUG;
  } catch {
    return DEFAULT_DEPARTMENT_SLUG;
  }
}

/** Wraps fetch() with the X-Department header so every request the app
 *  makes carries the active department (canonical slug only). Routes that
 *  are not department-aware ignore the header (Claims Concierge is the
 *  default), department-aware routes on the API pick it up. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {});
  headers.set("X-Department", canonicalDepartmentSlug(headers.get("X-Department")) || currentDepartmentSlug());
  if (tokenProvider && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${await tokenProvider()}`);
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

/** Set by the signed-in insurance portal so staff workspaces send the Entra access token. */
let tokenProvider: (() => Promise<string>) | null = null;
export function setApiTokenProvider(provider: (() => Promise<string>) | null) { tokenProvider = provider; }

export interface StreamEvent {
  kind: string;
  agent?: string;
  text?: string;
  tool?: string;
  arguments?: any;
  result?: string;
  session_id?: string;
  workflow_id?: string;
  message?: string;
  to?: string;
  // Trace-rail additions — set on tool_call / tool_args_done / tool_result
  // events emitted by the orchestrator so ChatTab can correlate its
  // hand-offs to the specialist agents.
  call_id?: string;
  to_agent?: string;
}

export async function getHitl(): Promise<{ items: DraftRecord[] }> {
  const r = await apiFetch(`/hitl`);
  if (!r.ok) throw new Error(`Approval queue failed (${r.status}).`);
  return r.json();
}

export type HitlDecisionResult = DraftRecord & {
  chat_message: string | null;
  session_id: string | null;
};

export async function decideHitl(
  hitl_id: string, workflow_id: string, decision: "approve" | "reject", note = "",
): Promise<HitlDecisionResult> {
  const r = await apiFetch(`/hitl/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hitl_id, workflow_id, decision, note }),
  });
  const result = await r.json();
  if (!r.ok) throw new Error(typeof result.detail === "string" ? result.detail : `Approval failed (${r.status})`);
  return result;
}

export async function getReviewedDrafts(): Promise<{ items: DraftRecord[] }> {
  const r = await apiFetch(`/hitl/resolved`);
  if (!r.ok) throw new Error(`Reviewed drafts failed (${r.status}).`);
  return r.json();
}

export async function deleteReviewedDraft(hitlId: string, workflowId: string): Promise<{ status: string }> {
  const url = `/hitl/${encodeURIComponent(hitlId)}?workflow_id=${encodeURIComponent(workflowId)}`;
  const r = await apiFetch(url, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete failed: ${r.status}`);
  return r.json();
}

export interface BuildInfo { git_sha: string; build_env: string; deployed_at: string; repo: string; [k: string]: string }
export interface TraceSpan {
  ts: string; kind: string; name: string; duration_ms: number;
  trace_id: string; parent_span_id: string; span_id: string;
  agent: string; tool: string; model: string;
  prompt_tokens: number; completion_tokens: number; cost_usd: number;
  git_sha: string; prompt_sha: Record<string, string>;
  gateway?: string; gateway_model?: string;
}
export interface TraceReport {
  session_id: string;
  build: BuildInfo;
  spans: TraceSpan[];
  totals: {
    latency_ms: number; cost_usd: number;
    prompt_tokens: number; completion_tokens: number;
    tool_calls: number; agent_hops: number;
  };
  handoff_mermaid: string;
  note?: string;
}

export async function getBuild(): Promise<{ build: BuildInfo }> {
  const r = await apiFetch(`/build`);
  return r.json();
}

export async function getTrace(sessionId: string, minutes = 15): Promise<TraceReport> {
  const r = await apiFetch(`/traces/${encodeURIComponent(sessionId)}?minutes=${minutes}`);
  return r.json();
}

// ── Cross-department handoff (Contact Center / Document Intelligence -> Claims Concierge) ───
export interface HandoffPayload {
  summary: string;
  caller?: string;
  claim_id?: string;
  policy_id?: string;
  policyholder_id?: string;
  loss_date?: string;
  state?: string;
  intent?: string;
  urgency?: string;
}

export async function* streamHandoff(payload: HandoffPayload): AsyncGenerator<StreamEvent> {
  const resp = await apiFetch(`/handoff`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok || !resp.body) throw new Error(`handoff failed: ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (line) yield JSON.parse(line);
    }
  }
}
