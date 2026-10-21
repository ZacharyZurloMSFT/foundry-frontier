const API_BASE = (import.meta as any).env.VITE_API_BASE_URL || "http://localhost:8000";

/** Read the currently-active department from the same localStorage key
 *  `CompanyContext` writes. Kept plain-function so non-React modules can
 *  reach it (e.g., the trace panel poller). */
export function currentDepartmentSlug(): string {
  try {
    return localStorage.getItem("northwind.activeDepartment") || "order-concierge";
  } catch {
    return "order-concierge";
  }
}

/** Wraps fetch() with the X-Department header so every request the app
 *  makes carries the active department. Existing routes ignore the
 *  header (Order Concierge default is preserved), but department-aware
 *  routes on the API pick it up. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {});
  if (!headers.has("X-Department")) headers.set("X-Department", currentDepartmentSlug());
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
  // events emitted by run_agent_turn so ChatTab can correlate the manager's
  // hand-offs to the four sub-agents.
  call_id?: string;
  to_agent?: string;
}

export async function* streamChat(sessionId: string | null, message: string): AsyncGenerator<StreamEvent> {
  const resp = await apiFetch(`/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!resp.ok || !resp.body) throw new Error(`chat failed: ${resp.status}`);
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

export async function* streamQA(question: string): AsyncGenerator<StreamEvent> {
  const resp = await apiFetch(`/qa`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!resp.ok || !resp.body) throw new Error(`qa failed: ${resp.status}`);
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

export async function* streamAgentChat(
  slug: string,
  sessionId: string | null,
  message: string,
): AsyncGenerator<StreamEvent> {
  const resp = await apiFetch(`/agent/${slug}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!resp.ok || !resp.body) throw new Error(`agent ${slug} failed: ${resp.status}`);
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

export async function getHitl(): Promise<{ items: any[] }> {
  const r = await apiFetch(`/hitl`);
  if (!r.ok) throw new Error(`Approval queue failed (${r.status}).`);
  return r.json();
}

export async function decideHitl(hitl_id: string, workflow_id: string, decision: "approve" | "reject", note = "") {
  const r = await apiFetch(`/hitl/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hitl_id, workflow_id, decision, note }),
  });
  const result = await r.json();
  if (!r.ok) throw new Error(typeof result.detail === "string" ? result.detail : `Approval failed (${r.status})`);
  return result;
}

export async function getWorkOrders(): Promise<{ items: any[] }> {
  const r = await apiFetch(`/work-orders`);
  if (!r.ok) throw new Error(`Reviewed drafts failed (${r.status}).`);
  return r.json();
}

export async function deleteWorkOrder(hitlId: string, workflowId: string): Promise<{ status: string }> {
  const url = `/work-orders/${encodeURIComponent(hitlId)}?workflow_id=${encodeURIComponent(workflowId)}`;
  const r = await apiFetch(url, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete failed: ${r.status}`);
  return r.json();
}

export async function getBakeoffModels(): Promise<{ models: BakeoffModel[] }> {
  const r = await apiFetch(`/bakeoff/models`);
  if (!r.ok) throw new Error(`Model comparison configuration failed (${r.status}).`);
  return r.json();
}

export interface BakeoffModel {
  deployment: string;
  price_in_per_mtok: number;
  price_out_per_mtok: number;
}

export interface BakeoffEvent {
  kind: "bakeoff_start" | "delta" | "model_done" | "model_error" | "done" | "error";
  model?: string;
  text?: string;
  models?: BakeoffModel[];
  message?: string;
  latency_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  cost_usd?: number;
}

export async function* streamBakeoff(message: string, system?: string): AsyncGenerator<BakeoffEvent> {
  const resp = await apiFetch(`/bakeoff/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, system }),
  });
  if (!resp.ok || !resp.body) throw new Error(`bakeoff failed: ${resp.status}`);
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

export interface SafetyAttack { id: string; label: string; prompt: string }
export interface SafetyVerdict {
  allowed: boolean;
  categories: Record<string, string>;
  shields: Record<string, boolean>;
  protected_material: boolean;
  pii_findings: { kind: string; text: string; start: number }[];
  redacted_text: string;
  engine: string;
  guardrail?: string;
  model_response?: string;
  model_deployment?: string;
  finish_reason?: string;
  blocked_reason?: string;
}

export async function getSafetyAttacks(): Promise<{ attacks: SafetyAttack[] }> {
  const r = await apiFetch(`/safety/attacks`);
  return r.json();
}

export async function checkSafety(text: string): Promise<SafetyVerdict> {
  const r = await apiFetch(`/safety/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return r.json();
}

export interface BuildInfo { git_sha: string; build_env: string; deployed_at: string; repo: string; [k: string]: string }
export interface TraceSpan {
  ts: string; kind: string; name: string; duration_ms: number;
  trace_id: string; parent_span_id: string; span_id: string;
  agent: string; tool: string; model: string;
  prompt_tokens: number; completion_tokens: number; cost_usd: number;
  git_sha: string; prompt_sha: Record<string, string>;
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

export async function mintVoiceSession(channel: "agent" | "customer"): Promise<any> {
  const r = await apiFetch(`/voice/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel }),
  });
  if (!r.ok) throw new Error(`voice mint failed: ${r.status}`);
  return r.json();
}

// ── Claims Document Intelligence (Content Understanding demo) ───────────────
export interface ClaimSample {
  id: string;
  source_file: string;
  source_text: string;
  extraction: Record<string, any>;
  source_mode?: string;
  extraction_mode?: string;
  status?: string;
}

export async function getClaimsSamples(): Promise<{ items: ClaimSample[] }> {
  const r = await apiFetch(`/claims/samples`);
  if (!r.ok) throw new Error(`Document samples failed (${r.status}).`);
  return r.json();
}

export async function extractClaim(filename: string): Promise<{ status: string } & ClaimSample> {
  const r = await apiFetch(`/claims/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`extract failed (${r.status}): ${detail}`);
  }
  return r.json();
}

// ── Cross-project handoff (Contact Center -> Order Concierge) ────────────────
export interface HandoffPayload {
  summary: string;
  caller?: string;
  order_id?: string;
  claim_id?: string;
  policy_id?: string;
  policyholder_id?: string;
  loss_date?: string;
  state?: string;
  customer_hint?: string;
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
