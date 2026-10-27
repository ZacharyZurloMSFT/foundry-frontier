export type Role = "policyholder" | "adjuster" | "presenter";
export type ClaimStatus = "reported" | "evidence_requested" | "evidence_received" | "adjuster_review" | "customer_update_prepared";
export interface Identity {
  run_id: string; role: Role; policyholder_id: string | null; source_version: string;
  synthetic: true; adjusters: string[]; actor_key: string;
}
export interface Source {
  title: string; version: string; state: string; effective_from: string; effective_to: string;
}
export interface Section { section_id: string; policy_ref: string; text: string; source_uri: string }
export interface PolicySnapshot {
  source_version: string;
  policy: {
    policy_id: string; policyholder_id: string; status: string; effective_from: string;
    effective_to: string; state: string; insured_asset: { type: string; description: string; vin: string };
    coverages: { coverage_type: string; limit_usd: number; deductible_usd: number }[];
    endorsements: (Source & { endorsement_id: string })[]; wording_id: string;
  };
  policyholder: { policyholder_id: string; name: string; email: string; phone: string };
  policy_wording: Source & { wording_id: string; sections: Section[] };
  citations: (Source & Section)[];
}
export interface TimelineEvent {
  event_id: string; command_id: string; actor_id: string | null; actor_role: string; occurred_at: string;
  summary: string; before_version: number; after_version: number; internal_note: string | null;
  visibility: "customer" | "internal";
}
export interface EvidenceTask {
  task_id: string; title: string; requested_evidence: string; status: "open" | "completed";
  assigned_to: string | null; created_by: string | null; created_at: string; updated_at: string;
  completed_by: string | null; completed_at: string | null;
}
export interface CaseNote {
  note_id: string; body: string; visibility: "customer" | "internal"; author_id: string | null; created_at: string;
}
export interface ClaimRecord {
  claim_id: string; run_id: string; policy_id: string; policyholder_id: string; incident_type: string;
  loss_date: string; state: string; description: string; status: ClaimStatus; version: number;
  created_at: string; updated_at: string; policy_snapshot: PolicySnapshot; review_flags: string[];
  outstanding_evidence: string[]; timeline: TimelineEvent[]; synthetic: true; tasks: EvidenceTask[];
  notes: CaseNote[]; assigned_adjuster_id: string | null;
}
export interface LossReport {
  policy_id: string; vehicle_id: string; incident_type: "auto_collision" | "auto_hail" | "auto_other";
  loss_date: string; state: string; description: string; synthetic_acknowledged: true;
}
export interface Page<T> { items: T[]; next_offset: number | null }

function invalid(field: string): never { throw new Error(`Invalid business API response: ${field}.`); }
export function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export function object(value: unknown, field = "object"): Record<string, unknown> {
  return isObject(value) ? value : invalid(field);
}
export function text(value: unknown, field: string): string {
  return typeof value === "string" ? value : invalid(field);
}
function nullable(value: unknown, field: string): string | null { return value === null ? null : text(value, field); }
function number(value: unknown, field: string): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : invalid(field);
}
export function integer(value: unknown, field: string): number {
  const n = number(value, field);
  return Number.isSafeInteger(n) ? n : invalid(field);
}
function array<T>(value: unknown, parser: (v: unknown) => T, field: string): T[] {
  return Array.isArray(value) ? value.map(parser) : invalid(field);
}
function strings(value: unknown, field: string): string[] { return array(value, v => text(v, field), field); }
function choice<T extends string>(value: unknown, allowed: readonly T[], field: string): T {
  for (const item of allowed) if (value === item) return item;
  return invalid(field);
}
function synthetic(value: unknown): true { return value === true ? true : invalid("synthetic"); }
function source(value: unknown): Source {
  const v = object(value, "source");
  return { title: text(v.title, "title"), version: text(v.version, "version"), state: text(v.state, "state"),
    effective_from: text(v.effective_from, "effective_from"), effective_to: text(v.effective_to, "effective_to") };
}
function section(value: unknown): Section {
  const v = object(value, "section");
  return { section_id: text(v.section_id, "section_id"), policy_ref: text(v.policy_ref, "policy_ref"),
    text: text(v.text, "section.text"), source_uri: text(v.source_uri, "source_uri") };
}
export function parseIdentity(value: unknown): Identity {
  const v = object(value, "identity");
  const result: Identity = {
    run_id: text(v.run_id, "run_id"), role: choice(v.role, ["policyholder", "adjuster", "presenter"], "role"),
    policyholder_id: nullable(v.policyholder_id, "policyholder_id"), source_version: text(v.source_version, "source_version"),
    synthetic: synthetic(v.synthetic), adjusters: strings(v.adjusters, "adjusters"),
    actor_key: text(v.actor_key, "actor_key"),
  };
  if (result.role === "policyholder" && (!result.policyholder_id || result.adjusters.length > 0)) invalid("customer identity");
  if (!/^[a-f0-9]{64}$/.test(result.actor_key)) invalid("actor_key");
  return result;
}
export function parsePolicy(value: unknown): PolicySnapshot {
  const v = object(value, "policy snapshot"), p = object(v.policy, "policy");
  const a = object(p.insured_asset, "insured_asset"), h = object(v.policyholder, "policyholder");
  const w = object(v.policy_wording, "policy_wording");
  const result: PolicySnapshot = {
    source_version: text(v.source_version, "source_version"),
    policy: {
      policy_id: text(p.policy_id, "policy_id"), policyholder_id: text(p.policyholder_id, "policyholder_id"),
      status: text(p.status, "policy.status"), effective_from: text(p.effective_from, "effective_from"),
      effective_to: text(p.effective_to, "effective_to"), state: text(p.state, "state"), wording_id: text(p.wording_id, "wording_id"),
      insured_asset: { type: text(a.type, "asset.type"), description: text(a.description, "asset.description"), vin: text(a.vin, "asset.vin") },
      coverages: array(p.coverages, entry => {
        const c = object(entry, "coverage");
        return { coverage_type: text(c.coverage_type, "coverage_type"), limit_usd: number(c.limit_usd, "limit_usd"),
          deductible_usd: number(c.deductible_usd, "deductible_usd") };
      }, "coverages"),
      endorsements: array(p.endorsements, entry => ({ ...source(entry), endorsement_id: text(object(entry).endorsement_id, "endorsement_id") }), "endorsements"),
    },
    policyholder: { policyholder_id: text(h.policyholder_id, "policyholder_id"), name: text(h.name, "name"),
      email: text(h.email, "email"), phone: text(h.phone, "phone") },
    policy_wording: { ...source(w), wording_id: text(w.wording_id, "wording_id"), sections: array(w.sections, section, "sections") },
    citations: array(v.citations, entry => ({ ...source(entry), ...section(entry) }), "citations"),
  };
  if (result.policy.policyholder_id !== result.policyholder.policyholder_id || result.policy.wording_id !== result.policy_wording.wording_id) invalid("policy identifiers");
  return result;
}
function parseTask(value: unknown): EvidenceTask {
  const v = object(value, "task");
  return { task_id: text(v.task_id, "task_id"), title: text(v.title, "task.title"),
    requested_evidence: text(v.requested_evidence, "requested_evidence"), status: choice(v.status, ["open", "completed"], "task.status"),
    assigned_to: nullable(v.assigned_to, "assigned_to"), created_by: nullable(v.created_by, "created_by"),
    created_at: text(v.created_at, "created_at"), updated_at: text(v.updated_at, "updated_at"),
    completed_by: nullable(v.completed_by, "completed_by"), completed_at: nullable(v.completed_at, "completed_at") };
}
function parseNote(value: unknown): CaseNote {
  const v = object(value, "note");
  return { note_id: text(v.note_id, "note_id"), body: text(v.body, "note.body"),
    visibility: choice(v.visibility, ["customer", "internal"], "note.visibility"),
    author_id: nullable(v.author_id, "author_id"), created_at: text(v.created_at, "created_at") };
}
function parseEvent(value: unknown): TimelineEvent {
  const v = object(value, "timeline event");
  return { event_id: text(v.event_id, "event_id"), command_id: text(v.command_id, "command_id"),
    actor_id: nullable(v.actor_id, "actor_id"), actor_role: text(v.actor_role, "actor_role"), occurred_at: text(v.occurred_at, "occurred_at"),
    summary: text(v.summary, "summary"), before_version: integer(v.before_version, "before_version"),
    after_version: integer(v.after_version, "after_version"), internal_note: nullable(v.internal_note, "internal_note"),
    visibility: choice(v.visibility, ["customer", "internal"], "event.visibility") };
}
export const claimStatuses: readonly ClaimStatus[] = ["reported", "evidence_requested", "evidence_received", "adjuster_review", "customer_update_prepared"];
export function parseClaim(value: unknown): ClaimRecord {
  const v = object(value, "claim");
  const result: ClaimRecord = {
    claim_id: text(v.claim_id, "claim_id"), run_id: text(v.run_id, "run_id"), policy_id: text(v.policy_id, "policy_id"),
    policyholder_id: text(v.policyholder_id, "policyholder_id"), incident_type: text(v.incident_type, "incident_type"),
    loss_date: text(v.loss_date, "loss_date"), state: text(v.state, "state"), description: text(v.description, "description"),
    status: choice(v.status, claimStatuses, "claim.status"), version: integer(v.version, "version"),
    created_at: text(v.created_at, "created_at"), updated_at: text(v.updated_at, "updated_at"),
    policy_snapshot: parsePolicy(v.policy_snapshot), review_flags: strings(v.review_flags, "review_flags"),
    outstanding_evidence: strings(v.outstanding_evidence, "outstanding_evidence"), timeline: array(v.timeline, parseEvent, "timeline"),
    synthetic: synthetic(v.synthetic), tasks: array(v.tasks, parseTask, "tasks"), notes: array(v.notes, parseNote, "notes"),
    assigned_adjuster_id: nullable(v.assigned_adjuster_id, "assigned_adjuster_id"),
  };
  if (result.version < 1) invalid("claim version");
  if (result.policy_id !== result.policy_snapshot.policy.policy_id || result.policyholder_id !== result.policy_snapshot.policy.policyholder_id) invalid("claim identifiers");
  return result;
}
export function parsePage<T>(value: unknown, parser: (v: unknown) => T): Page<T> {
  const v = object(value, "page");
  return { items: array(v.items, parser, "items"), next_offset: v.next_offset === null ? null : integer(v.next_offset, "next_offset") };
}
export function parseCreated(value: unknown): { claim: ClaimRecord; replayed: boolean } {
  const v = object(value, "created claim");
  if (typeof v.replayed !== "boolean") invalid("replayed");
  return { claim: parseClaim(v.claim), replayed: v.replayed };
}
export function nextStatuses(status: ClaimStatus): ClaimStatus[] {
  switch (status) {
    case "reported": return ["evidence_requested"];
    case "evidence_requested": return ["evidence_received"];
    case "evidence_received": return ["adjuster_review", "evidence_requested"];
    case "adjuster_review": return ["customer_update_prepared", "evidence_requested"];
    case "customer_update_prepared": return ["evidence_requested"];
  }
}
export const statusLabel = (value: string) => value.replaceAll("_", " ");
