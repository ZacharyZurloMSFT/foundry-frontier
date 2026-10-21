import { integer, isObject, object, text } from "./models";

export const evidenceFieldNames = ["claim_id", "policy_id", "policyholder_id", "loss_date", "estimate_total_usd", "invoice_total_usd"] as const;
export type EvidenceFieldName = typeof evidenceFieldNames[number];
export type FieldValue = string | number | null;
export type FieldDecision = "accept" | "correct" | "clear";
export interface ExtractedField { type: string; value: FieldValue; confidence: number | null; source_spans: unknown[]; missing: boolean }
export interface ConfirmedField { value: FieldValue; decision: FieldDecision; reviewed_by: string | null; reviewed_at: string; extraction_sha256: string | null }
export interface EvidenceDocument {
  document_id: string; claim_id: string; filename: string; media_type: string; size_bytes: number;
  sha256: string; version: number; created_at: string; source_mode: "synthetic_sample" | "synthetic_upload";
  uploaded_by: string | null; job_id: string; review_version: number;
  fields: Partial<Record<EvidenceFieldName, ExtractedField>>;
  confirmed_fields: Partial<Record<EvidenceFieldName, ConfirmedField>>; notice: string;
}
export interface EvidenceJob {
  job_id: string; document_id: string; claim_id: string; status: "queued" | "running" | "completed" | "failed" | "cancelled";
  version: number; attempts: number; max_attempts: number; error_code: string | null; error_message: string | null;
  created_at: string; updated_at: string; notice: string;
}
export interface EvidenceStatus { enabled: boolean; uploads_enabled: boolean; extraction_enabled: boolean; worker_enabled: boolean; max_upload_bytes: number; notice: string }
export interface EvidenceSample { filename: string; title: string; media_type: string }
export interface EvidenceReceipt { document: EvidenceDocument; job: EvidenceJob; replayed: boolean }
export interface FieldReview { name: EvidenceFieldName; decision: FieldDecision; value?: FieldValue }

function invalid(field: string): never { throw new Error(`Invalid evidence response: ${field}.`); }
function boolean(value: unknown, field: string): boolean { return typeof value === "boolean" ? value : invalid(field); }
function nullable(value: unknown, field: string): string | null { return value === null ? null : text(value, field); }
function oneOf<T extends string>(value: unknown, allowed: readonly T[], field: string): T {
  for (const item of allowed) if (item === value) return item;
  return invalid(field);
}
function hash(value: unknown): string { return typeof value === "string" && /^[a-f0-9]{64}$/.test(value) ? value : invalid("sha256"); }
function version(value: unknown): number { const result = integer(value, "version"); return result > 0 ? result : invalid("version"); }
function fieldValue(value: unknown): FieldValue {
  return value === null || typeof value === "string" || typeof value === "number" && Number.isFinite(value) ? value : invalid("field value");
}
function extracted(value: unknown): ExtractedField {
  const v = object(value);
  if (v.confidence !== null && (typeof v.confidence !== "number" || !Number.isFinite(v.confidence) || v.confidence < 0 || v.confidence > 1)) invalid("confidence");
  if (!Array.isArray(v.source_spans)) invalid("source spans");
  return { type: text(v.type, "field type"), value: fieldValue(v.value), confidence: v.confidence,
    source_spans: v.source_spans, missing: boolean(v.missing, "missing") };
}
function confirmed(value: unknown): ConfirmedField {
  const v = object(value);
  return { value: fieldValue(v.value), decision: oneOf(v.decision, ["accept", "correct", "clear"], "decision"),
    reviewed_by: nullable(v.reviewed_by, "reviewed_by"), reviewed_at: text(v.reviewed_at, "reviewed_at"),
    extraction_sha256: v.extraction_sha256 === null ? null : hash(v.extraction_sha256) };
}
function fields<T>(value: unknown, parser: (entry: unknown) => T): Partial<Record<EvidenceFieldName, T>> {
  const v = object(value), result: Partial<Record<EvidenceFieldName, T>> = {};
  if (Object.keys(v).some(name => !evidenceFieldNames.some(known => name === known))) invalid("unknown fields");
  for (const name of evidenceFieldNames) if (Object.hasOwn(v, name)) result[name] = parser(v[name]);
  return result;
}
export function parseEvidenceDocument(value: unknown): EvidenceDocument {
  const v = object(value);
  const result: EvidenceDocument = {
    document_id: text(v.document_id, "document_id"), claim_id: text(v.claim_id, "claim_id"),
    filename: text(v.filename, "filename"), media_type: oneOf(v.media_type, ["application/pdf", "image/png", "image/jpeg"], "media_type"),
    size_bytes: version(v.size_bytes), sha256: hash(v.sha256), version: version(v.version),
    created_at: text(v.created_at, "created_at"), source_mode: oneOf(v.source_mode, ["synthetic_sample", "synthetic_upload"], "source mode"),
    uploaded_by: nullable(v.uploaded_by, "uploaded_by"), job_id: text(v.job_id, "job_id"),
    review_version: integer(v.review_version, "review_version"), fields: fields(v.fields, extracted),
    confirmed_fields: fields(v.confirmed_fields, confirmed), notice: text(v.notice, "notice"),
  };
  if (result.size_bytes > 10485760 || !/^doc-[a-f0-9-]{36}$/.test(result.document_id) || !/^job-[a-f0-9-]{36}$/.test(result.job_id)) invalid("document metadata");
  return result;
}
export function parseEvidenceJob(value: unknown): EvidenceJob {
  const v = object(value);
  const result: EvidenceJob = {
    job_id: text(v.job_id, "job_id"), document_id: text(v.document_id, "document_id"), claim_id: text(v.claim_id, "claim_id"),
    status: oneOf(v.status, ["queued", "running", "completed", "failed", "cancelled"], "job status"),
    version: version(v.version), attempts: integer(v.attempts, "attempts"), max_attempts: version(v.max_attempts),
    error_code: nullable(v.error_code, "error_code"), error_message: nullable(v.error_message, "error_message"),
    created_at: text(v.created_at, "created_at"), updated_at: text(v.updated_at, "updated_at"), notice: text(v.notice, "notice"),
  };
  if (result.attempts > result.max_attempts || !/^job-[a-f0-9-]{36}$/.test(result.job_id) || !/^doc-[a-f0-9-]{36}$/.test(result.document_id)) invalid("job metadata");
  return result;
}
export function parseEvidenceStatus(value: unknown): EvidenceStatus {
  const v = object(value);
  const max = integer(v.max_upload_bytes, "max_upload_bytes");
  if (!max || max > 10485760) invalid("upload bound");
  return { enabled: boolean(v.enabled, "enabled"), uploads_enabled: boolean(v.uploads_enabled, "uploads"),
    extraction_enabled: boolean(v.extraction_enabled, "extraction"), worker_enabled: boolean(v.worker_enabled, "worker"),
    max_upload_bytes: max, notice: text(v.notice, "notice") };
}
export function parseEvidenceReceipt(value: unknown): EvidenceReceipt {
  const v = object(value), document = parseEvidenceDocument(v.document), job = parseEvidenceJob(v.job);
  if (document.document_id !== job.document_id || document.job_id !== job.job_id || document.claim_id !== job.claim_id) invalid("receipt relationships");
  return { document, job, replayed: boolean(v.replayed, "replayed") };
}
export function parseSamples(value: unknown): EvidenceSample[] {
  const v = object(value);
  if (!Array.isArray(v.items)) invalid("samples");
  return v.items.map(item => {
    if (!isObject(item)) invalid("sample");
    return { filename: text(item.filename, "filename"), title: text(item.title, "title"), media_type: text(item.media_type, "media_type") };
  });
}
