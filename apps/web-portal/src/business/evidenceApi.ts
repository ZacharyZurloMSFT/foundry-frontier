import { BusinessApi, BusinessError } from "./api";
import { parsePage } from "./models";
import { EvidenceDocument, FieldReview, parseEvidenceDocument, parseEvidenceJob, parseEvidenceReceipt, parseEvidenceStatus, parseSamples } from "./evidenceModels";
import { prepareHashedCommand, RetryStorage } from "./reportRetry";

export class EvidenceApi {
  constructor(private readonly api: BusinessApi) {}
  status() { return this.api.request("/evidence/status", parseEvidenceStatus); }
  samples() { return this.api.request("/evidence/samples", parseSamples); }
  documents(claimId: string, offset = 0) {
    return this.api.request(`/claims/${encodeURIComponent(claimId)}/documents?offset=${offset}&limit=20`, v => parsePage(v, entry => {
      const doc = parseEvidenceDocument(entry);
      if (doc.claim_id !== claimId) throw new Error("Evidence belongs to another claim.");
      return doc;
    }));
  }
  document(id: string, claimId: string) {
    return this.api.request(`/documents/${encodeURIComponent(id)}`, v => this.documentFor(v, id, claimId));
  }
  private documentFor(value: unknown, id: string, claimId: string): EvidenceDocument {
    const doc = parseEvidenceDocument(value);
    if (doc.document_id !== id || doc.claim_id !== claimId) throw new Error("Evidence context mismatch.");
    return doc;
  }
  sample(claimId: string, filename: string, expectedVersion: number, key: string) {
    return this.api.request(`/claims/${encodeURIComponent(claimId)}/documents/samples`, v => {
      const receipt = parseEvidenceReceipt(v);
      if (receipt.document.claim_id !== claimId) throw new Error("Evidence context mismatch.");
      return receipt;
    }, "POST", { filename, expected_version: expectedVersion, synthetic_acknowledged: true }, key);
  }
  async upload(claimId: string, file: File, expectedVersion: number, key: string) {
    const path = `/claims/${encodeURIComponent(claimId)}/documents?filename=${encodeURIComponent(file.name)}&expected_version=${expectedVersion}`;
    const response = await this.api.exchange(path, "POST", file, {
      "Content-Type": evidenceMediaType(file.name), "Idempotency-Key": key, "X-Synthetic-Data": "true",
    });
    return this.api.readJson(response, v => {
      const receipt = parseEvidenceReceipt(v);
      if (receipt.document.claim_id !== claimId) throw new Error("Evidence context mismatch.");
      return receipt;
    }, true);
  }
  job(id: string, claimId: string, documentId: string) {
    return this.api.request(`/jobs/${encodeURIComponent(id)}`, v => {
      const job = parseEvidenceJob(v);
      if (job.job_id !== id || job.claim_id !== claimId || job.document_id !== documentId) throw new Error("Job context mismatch.");
      return job;
    });
  }
  jobCommand(action: "retry" | "cancel", id: string, claimId: string, documentId: string, expectedVersion: number) {
    return this.api.request(`/jobs/${encodeURIComponent(id)}/${action}`, v => {
      const job = parseEvidenceJob(v);
      if (job.job_id !== id || job.claim_id !== claimId || job.document_id !== documentId) throw new Error("Job context mismatch.");
      return job;
    }, "POST", { expected_version: expectedVersion });
  }
  review(document: EvidenceDocument, fields: FieldReview[]) {
    return this.api.request(`/documents/${encodeURIComponent(document.document_id)}/reviews`,
      v => this.documentFor(v, document.document_id, document.claim_id), "POST", {
        expected_review_version: document.review_version, expected_document_version: document.version,
        fields, synthetic_acknowledged: true,
      });
  }
  async source(document: EvidenceDocument): Promise<Blob> {
    const response = await this.api.exchange(`/documents/${encodeURIComponent(document.document_id)}/content`);
    if (!response.ok) return this.api.readJson(response, () => { throw new Error("Unexpected source error response."); });
    if (response.headers.get("content-type")?.split(";")[0].trim() !== document.media_type || !response.body) {
      throw new BusinessError("The original source response has an invalid type.", response.status, "invalid_source", response.headers.get("X-Correlation-ID"));
    }
    const reader = response.body.getReader(), chunks: ArrayBuffer[] = [];
    let size = 0;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > document.size_bytes || size > 10485760) throw new Error("Source exceeded its recorded size.");
        const copy = new ArrayBuffer(value.length);
        new Uint8Array(copy).set(value);
        chunks.push(copy);
      }
    } finally { await reader.cancel(); }
    const bytes = new Blob(chunks, { type: document.media_type });
    if (size !== document.size_bytes || await sha256(await bytes.arrayBuffer()) !== document.sha256) throw new Error("Source integrity could not be verified. No document was opened.");
    return bytes;
  }
}
export async function sha256(bytes: ArrayBuffer): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), value => value.toString(16).padStart(2, "0")).join("");
}
export function evidenceMediaType(filename: string): string {
  const extension = filename.toLowerCase().split(".").pop();
  if (extension === "pdf") return "application/pdf";
  if (extension === "png") return "image/png";
  if (extension === "jpg" || extension === "jpeg") return "image/jpeg";
  throw new Error("Use a PDF, JPEG or PNG document.");
}
export function evidenceStorageSlot(runId: string, actorKey: string, claimId: string): string {
  return `northwind.evidence-command.${encodeURIComponent(runId)}.${encodeURIComponent(actorKey)}.${encodeURIComponent(claimId)}`;
}
export async function evidenceCommand(storage: RetryStorage, slot: string, kind: "sample" | "upload", filename: string, file?: File) {
  if (file && (file.size < 1 || file.size > 10485760)) throw new Error("Evidence must be nonempty and no larger than 10 MiB.");
  const fingerprint = JSON.stringify({ kind, filename, ...(file ? { sha256: await sha256(await file.arrayBuffer()), media_type: evidenceMediaType(filename) } : {}) });
  const hash = await sha256(new TextEncoder().encode(fingerprint).buffer);
  return prepareHashedCommand(hash, storage, slot, "document");
}
