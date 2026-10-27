export const API_BASE = ((import.meta as any).env?.VITE_API_BASE_URL ||
  "https://ca-insurance-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io").replace(/\/$/, "");

export interface InsuranceRecords {
  business_domain: "insurance";
  agent_runtime_ready: boolean;
  policyholders: { policyholder_id: string; name: string }[];
  policies: { policy_id: string; policyholder_id: string; state: string; insured_asset: Record<string, unknown> }[];
  claims: { claim_id: string; policy_id: string; policyholder_id: string; loss_date: string; description: string }[];
}
export interface VoiceSession {
  transport: "websocket-proxy";
  connect_ticket: string;
  websocket_path: "/voice/realtime";
  model: string;
  sample_rate: 24000;
  max_session_seconds: number;
}
export interface HandoffPayload {
  summary: string; caller?: string; intent?: string; urgency?: string;
  claim_id?: string; policy_id?: string; policyholder_id?: string;
  loss_date?: string; state?: string;
}

async function responseError(response: Response): Promise<Error> {
  const data = await response.json().catch(() => ({}));
  return new Error(typeof data.detail === "string" ? data.detail :
    `Request failed (${response.status}). Check identifiers, loss date and state; no automatic retry was made.`);
}
export async function getInsuranceRecords(signal?: AbortSignal): Promise<InsuranceRecords> {
  const response = await fetch(`${API_BASE}/insurance/fixtures`, { signal });
  if (!response.ok) throw await responseError(response);
  const data = await response.json();
  if (data?.business_domain !== "insurance" || typeof data.agent_runtime_ready !== "boolean" ||
      !Array.isArray(data.claims) || !Array.isArray(data.policies) || !Array.isArray(data.policyholders)) {
    throw new Error("Insurance records and readiness could not be verified.");
  }
  return data;
}
export async function getInsuranceReadiness(signal?: AbortSignal): Promise<boolean> {
  return (await getInsuranceRecords(signal)).agent_runtime_ready;
}
export async function getVoiceReadiness(signal?: AbortSignal): Promise<void> {
  const response = await fetch(`${API_BASE}/voice/status`, { signal, headers: { "X-Department": "contact-center" } });
  if (!response.ok) throw await responseError(response);
  const data = await response.json();
  if (data?.ready !== true || data.transport !== "websocket-proxy") throw new Error("Voice Live transport is not ready.");
}
export async function mintVoiceSession(signal?: AbortSignal): Promise<VoiceSession> {
  if (!(await getInsuranceReadiness(signal))) throw new Error("Insurance agent runtime is not ready.");
  const response = await fetch(`${API_BASE}/voice/session`, {
    signal, method: "POST",
    headers: { "Content-Type": "application/json", "X-Department": "contact-center" },
    body: JSON.stringify({ channel: "customer" }),
  });
  if (!response.ok) throw await responseError(response);
  const data = await response.json();
  if (data?.transport !== "websocket-proxy" || data.websocket_path !== "/voice/realtime" ||
      typeof data.connect_ticket !== "string" || data.connect_ticket.length < 32 || data.connect_ticket.length > 128 ||
      data.sample_rate !== 24000 || "client_secret" in data || "api_key" in data ||
      "access_token" in data || "realtime_url" in data ||
      !Number.isInteger(data.max_session_seconds) || data.max_session_seconds < 1 || data.max_session_seconds > 600) {
    throw new Error("Unsafe or unsupported Voice Live configuration.");
  }
  return data;
}
export function voiceProxyUrlFor(apiBase: string): string {
  const url = new URL(apiBase);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname))) {
    throw new Error("Voice requires HTTPS (or local development).");
  }
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  // A gateway base keeps its path prefix (for example /insurance), so the proxy path is appended rather than replacing it.
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/voice/realtime`;
  url.search = ""; url.hash = "";
  return url.toString();
}
export function voiceProxyUrl(): string {
  return voiceProxyUrlFor(API_BASE);
}
export function confirmedHandoff(payload: HandoffPayload, confirmed: boolean, asset: string): HandoffPayload {
  if (!confirmed) throw new Error("Confirm the caller-provided identifiers, date, state and asset before handoff.");
  if (!payload.summary.trim() || payload.summary.length > 12000) throw new Error("Enter a summary of up to 12,000 characters.");
  if (!payload.policy_id?.trim() || !payload.policyholder_id?.trim() || !payload.loss_date || !payload.state || !asset.trim()) {
    throw new Error("Policy, policyholder, loss date, state and insured asset are required; never guess missing values.");
  }
  const loss = new Date(`${payload.loss_date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(payload.loss_date) || !Number.isFinite(loss.getTime()) ||
      loss.toISOString().slice(0, 10) !== payload.loss_date) {
    throw new Error("Enter a valid loss date.");
  }
  if (!/^[A-Z]{2}$/.test(payload.state)) throw new Error("Enter a two-letter state.");
  const result: HandoffPayload = { summary: `${payload.summary.trim()}\nOperator-confirmed insured asset: ${asset.trim()}. Human-reviewed next step only; no coverage decision or payment.` };
  for (const key of ["caller", "intent", "urgency", "claim_id", "policy_id", "policyholder_id", "loss_date", "state"] as const) {
    const value = payload[key]?.trim();
    if (value) result[key] = value;
  }
  return result;
}
export async function* streamHandoff(payload: HandoffPayload, signal?: AbortSignal): AsyncGenerator<Record<string, any>> {
  const response = await fetch(`${API_BASE}/handoff`, {
    method: "POST", signal,
    headers: { "Content-Type": "application/json", "X-Department": "contact-center" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("Handoff stream is unavailable. Check the session before retrying.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      if (pending.length > 1024 * 1024) throw new Error("Handoff stream exceeded its buffer limit.");
      let index: number;
      while ((index = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, index).trim();
        pending = pending.slice(index + 1);
        if (line) yield JSON.parse(line);
      }
      if (done) { if (pending.trim()) yield JSON.parse(pending); break; }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
