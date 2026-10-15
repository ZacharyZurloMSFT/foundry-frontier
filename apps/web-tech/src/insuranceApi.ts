import { apiFetch, type StreamEvent } from "./api";

export interface Policyholder {
  policyholder_id: string;
  name: string;
  email: string;
  phone: string;
}
export interface Coverage {
  coverage_type: string;
  limit_usd: number;
  deductible_usd: number;
}
export interface InsurancePolicy {
  policy_id: string;
  policyholder_id: string;
  status: string;
  effective_from: string;
  effective_to: string;
  state: string;
  insured_asset: string | Record<string, unknown>;
  coverages: Coverage[];
  endorsements: (string | Record<string, unknown>)[];
  wording_id?: string;
}
export interface InsuranceClaim {
  claim_id: string;
  policy_id: string;
  policyholder_id: string;
  incident_type: string;
  loss_date: string;
  status: string;
  description: string;
  outstanding_evidence: string[];
}
export interface InsuranceFixtures {
  business_domain: "insurance";
  agent_runtime_ready: boolean;
  policyholders: Policyholder[];
  policies: InsurancePolicy[];
  claims: InsuranceClaim[];
  dataset_version?: string;
  policy_wordings?: PolicyWording[];
}
export interface PolicyWording {
  wording_id: string;
  title: string;
  version: string;
  state: string;
  effective_from: string;
  effective_to: string;
  sections: { section_id: string; policy_ref: string; text: string; source_uri: string }[];
}
export const ACTION_LABELS = {
  request_evidence: "Request evidence",
  assign_adjuster: "Assign adjuster",
  customer_update: "Policyholder update",
  escalate_review: "Escalate for review",
  settlement_recommendation: "Settlement recommendation (review only)",
} as const;
export type ActionType = keyof typeof ACTION_LABELS;
export interface InsuranceActionDraft {
  claim_id: string;
  policy_id: string;
  policyholder_id: string;
  action_type: ActionType;
  summary: string;
  policy_refs: string[];
  amount_usd?: number;
}
export interface DraftReceipt {
  status: "pending_approval";
  hitl_id: string;
  workflow_id: string;
}

export function defaultDraftReferences(claim: InsuranceClaim, wording?: PolicyWording): string[] {
  const incidentSection = claim.incident_type === "auto_collision" ? "collision" :
    claim.incident_type === "auto_hail" ? "comprehensive" : undefined;
  return wording?.sections.filter((section) =>
    section.section_id === "duties-after-loss" || section.section_id === incidentSection,
  ).map((section) => section.policy_ref) ?? [];
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === "string");
}
function fields(value: unknown, keys: string[]): value is Record<string, unknown> {
  return object(value) && keys.every((key) => typeof value[key] === "string");
}
function asset(value: unknown): boolean {
  return typeof value === "string" || object(value);
}
function money(value: unknown): boolean {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

export function parseInsuranceFixtures(value: unknown): InsuranceFixtures {
  if (!object(value) || value.business_domain !== "insurance" || typeof value.agent_runtime_ready !== "boolean" ||
      !Array.isArray(value.policyholders) || !value.policyholders.every((v) => fields(v, ["policyholder_id", "name", "email", "phone"])) ||
      !Array.isArray(value.policies) || !value.policies.every((v) =>
        fields(v, ["policy_id", "policyholder_id", "status", "effective_from", "effective_to", "state"]) &&
        asset(v.insured_asset) && Array.isArray(v.endorsements) && v.endorsements.every(asset) &&
        Array.isArray(v.coverages) && v.coverages.every((c) => fields(c, ["coverage_type"]) && money(c.limit_usd) && money(c.deductible_usd))) ||
      !Array.isArray(value.claims) || !value.claims.every((v) =>
        fields(v, ["claim_id", "policy_id", "policyholder_id", "incident_type", "loss_date", "status", "description"]) && strings(v.outstanding_evidence))) {
    throw new Error("Invalid insurance records response. No records or agent readiness can be trusted.");
  }
  const fixtures = value as unknown as InsuranceFixtures;
  if (value.policy_wordings !== undefined && (!Array.isArray(value.policy_wordings) ||
      !value.policy_wordings.every((w) => fields(w, ["wording_id", "title", "version", "state", "effective_from", "effective_to"]) &&
        Array.isArray(w.sections) && w.sections.every((s) => fields(s, ["section_id", "policy_ref", "text", "source_uri"]))))) {
    throw new Error("Invalid versioned insurance wording response.");
  }
  const holderIds = new Set(fixtures.policyholders.map((h) => h.policyholder_id));
  const policies = new Map(fixtures.policies.map((p) => [p.policy_id, p]));
  if (holderIds.size !== fixtures.policyholders.length || policies.size !== fixtures.policies.length ||
      new Set(fixtures.claims.map((c) => c.claim_id)).size !== fixtures.claims.length ||
      fixtures.policies.some((p) => !holderIds.has(p.policyholder_id)) ||
      fixtures.claims.some((c) => !holderIds.has(c.policyholder_id) || policies.get(c.policy_id)?.policyholder_id !== c.policyholder_id)) {
    throw new Error("Insurance records have inconsistent identifiers. Drafting is unavailable.");
  }
  return fixtures;
}

async function ensureOk(response: Response): Promise<void> {
  if (response.ok) return;
  const contentType = response.headers.get("content-type") ?? "";
  const body: unknown = contentType.includes("json") ? await response.json() : await response.text();
  const detail = object(body) && typeof body.detail === "string" ? body.detail :
    object(body) && Array.isArray(body.detail) ? body.detail.filter(object).map((d) => typeof d.msg === "string" ? d.msg : "").filter(Boolean).join("; ") :
    typeof body === "string" ? body : "";
  throw new Error(detail || `Insurance request failed (${response.status}).`);
}

export async function getInsuranceFixtures(signal?: AbortSignal): Promise<InsuranceFixtures> {
  const response = await apiFetch("/insurance/fixtures", { signal });
  await ensureOk(response);
  return parseInsuranceFixtures(await response.json());
}

export function actionDraft(
  claim: InsuranceClaim, action_type: ActionType, summary: string, references: string, amount: string,
): InsuranceActionDraft {
  if (!summary.trim()) throw new Error("Describe the proposed action before submitting.");
  if (summary.trim().length > 2000) throw new Error("Summaries must be 2,000 characters or fewer.");
  if (!Object.hasOwn(ACTION_LABELS, action_type)) throw new Error("Unknown insurance action.");
  const draft: InsuranceActionDraft = {
    claim_id: claim.claim_id, policy_id: claim.policy_id, policyholder_id: claim.policyholder_id,
    action_type, summary: summary.trim(),
    policy_refs: [...new Set(references.split(/[\n,]/).map((r) => r.trim()).filter(Boolean))],
  };
  if (draft.policy_refs.length > 20) throw new Error("Use at most 20 source references.");
  if (amount.trim()) {
    if (action_type !== "settlement_recommendation") throw new Error("Amounts are only allowed on settlement recommendations.");
    const value = Number(amount);
    if (!Number.isFinite(value) || value <= 0 || !/^\d+(\.\d{1,2})?$/.test(amount.trim())) {
      throw new Error("Enter a positive USD amount with at most two decimal places.");
    }
    draft.amount_usd = value;
  }
  if (action_type === "settlement_recommendation" && (!draft.amount_usd || !draft.policy_refs.length)) {
    throw new Error("Settlement recommendations require a positive proposed amount and applicable versioned policy citations.");
  }
  return draft;
}

export async function draftInsuranceAction(draft: InsuranceActionDraft): Promise<DraftReceipt> {
  const response = await apiFetch("/insurance/actions/draft", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft),
  });
  await ensureOk(response);
  const value: unknown = await response.json();
  if (!fields(value, ["status", "hitl_id", "workflow_id"]) || value.status !== "pending_approval") {
    throw new Error("Draft response could not be verified. Check Approvals before retrying; a draft may already be saved.");
  }
  return value as unknown as DraftReceipt;
}

export async function* streamInsuranceChat(sessionId: string | null, message: string): AsyncGenerator<StreamEvent> {
  // Check immediately before sending, not just when the page first loaded.
  const fixtures = await getInsuranceFixtures();
  if (!fixtures.agent_runtime_ready) throw new Error("Insurance agents are not ready. Records and human-reviewed drafts remain available.");
  const response = await apiFetch("/insurance/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, ...(sessionId ? { session_id: sessionId } : {}) }),
  });
  await ensureOk(response);
  if (!response.body) throw new Error("Insurance chat returned no stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      if (done && buffer.trim()) { lines.push(buffer); buffer = ""; }
      for (const line of lines) {
        if (!line.trim()) continue;
        const event: unknown = JSON.parse(line);
        if (!object(event) || typeof event.kind !== "string") throw new Error("Invalid insurance chat event.");
        yield event as unknown as StreamEvent;
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}
