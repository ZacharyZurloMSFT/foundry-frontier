import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { BusinessApi } from "./api";
import { EvidenceApi, evidenceCommand, evidenceMediaType, evidenceStorageSlot } from "./evidenceApi";
import { EvidenceDocument, EvidenceFieldName, EvidenceJob, EvidenceSample, EvidenceStatus, FieldDecision, FieldReview, evidenceFieldNames } from "./evidenceModels";
import { ClaimRecord, Identity, Page, statusLabel } from "./models";
import { clearConfirmedCommand, readCommand } from "./reportRetry";
import { DateText, ErrorNotice, Loading, Pager } from "./shared";

export function fieldReview(name: EvidenceFieldName, decision: FieldDecision, input: string): FieldReview {
  if (decision !== "correct") return { name, decision };
  if (!input.trim()) throw new Error("Enter a corrected value, or explicitly clear this field.");
  if (name.endsWith("_usd")) {
    if (!/^\d+(?:\.\d{1,2})?$/.test(input.trim()) || !Number.isFinite(Number(input))) throw new Error("Enter a nonnegative amount with at most two decimal places.");
    return { name, decision, value: Number(input) };
  }
  return { name, decision, value: input.trim() };
}

export function FieldReviewForm({ document, name, busy, locked, review }: {
  document: EvidenceDocument; name: EvidenceFieldName; busy: boolean; locked: boolean;
  review: (field: FieldReview) => Promise<void>;
}) {
  const raw = document.fields[name], confirmed = document.confirmed_fields[name];
  const [decision, setDecision] = useState<FieldDecision>("correct");
  const [input, setInput] = useState(confirmed?.value === null || confirmed?.value === undefined ? "" : String(confirmed.value));
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || locked || !acknowledged) return;
    try { const command = fieldReview(name, decision, input); setError(null); await review(command); }
    catch (e) { setError(e); }
  }
  return <article className="business-panel business-field-review">
    <h4>{statusLabel(name)}</h4>
    <dl className="business-facts">
      <div><dt>Untrusted extracted suggestion</dt><dd>{!raw || raw.missing ? "Missing — manual entry is available" : raw.value === null ? "No value" : String(raw.value)}</dd></div>
      <div><dt>Available confidence</dt><dd>{raw?.confidence === null || raw?.confidence === undefined ? "Not supplied" : `${Math.round(raw.confidence * 100)}% — human review required`}</dd></div>
      <div><dt>Human-confirmed value</dt><dd>{!confirmed ? "Not reviewed" : confirmed.decision === "clear" ? "Explicitly cleared" : confirmed.value === null ? "No value" : String(confirmed.value)}</dd></div>
    </dl>
    {confirmed && <p className="business-muted">{confirmed.decision} · <DateText value={confirmed.reviewed_at} /></p>}
    {!!raw?.source_spans.length && <details><summary>Inspect source spans</summary><pre className="business-evidence-spans">{JSON.stringify(raw.source_spans, null, 2)}</pre></details>}
    <ErrorNotice error={error} />
    <form onSubmit={event => void submit(event)}>
      <fieldset disabled={busy || locked}>
        <legend>Review {statusLabel(name)}</legend>
        <label htmlFor={`review-decision-${name}`}>Decision</label>
        <select id={`review-decision-${name}`} value={decision} onChange={event => {
          const value = event.target.value;
          if (value === "accept" || value === "correct" || value === "clear") setDecision(value);
        }}>
          <option value="correct">Enter a manually confirmed value</option>
          <option value="accept" disabled={!raw || raw.missing || raw.value === null}>Accept the extracted suggestion</option>
          <option value="clear">Explicitly clear this field</option>
        </select>
        {decision === "correct" && <><label htmlFor={`review-value-${name}`}>Confirmed value</label><input id={`review-value-${name}`} required maxLength={2000}
          type={name === "loss_date" ? "date" : "text"} inputMode={name.endsWith("_usd") ? "decimal" : "text"} value={input} onChange={event => setInput(event.target.value)} /></>}
        <label className="business-check"><input type="checkbox" required checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />I inspected the original source and confirm this review contains synthetic information only.</label>
        <button type="submit" disabled={!acknowledged}>Save {statusLabel(name)} review at version {document.review_version}</button>
      </fieldset>
    </form>
  </article>;
}

export default function DocumentsPanel({ claim, identity, api, caseBusy, caseLocked, onCaseChanged, onError }: {
  claim: ClaimRecord; identity: Identity; api: BusinessApi; caseBusy: boolean; caseLocked: boolean;
  onCaseChanged: () => void; onError: (error: unknown) => void;
}) {
  const evidence = useMemo(() => new EvidenceApi(api), [api]);
  const [status, setStatus] = useState<EvidenceStatus | null>(null), [samples, setSamples] = useState<EvidenceSample[]>([]);
  const [page, setPage] = useState<Page<EvidenceDocument> | null>(null), [offset, setOffset] = useState(0), [history, setHistory] = useState<number[]>([]);
  const [selected, setSelected] = useState<EvidenceDocument | null>(null), [job, setJob] = useState<EvidenceJob | null>(null);
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [locked, setLocked] = useState(false);
  const [error, setError] = useState<unknown>(null), [notice, setNotice] = useState(""), [reload, setReload] = useState(0);
  const [source, setSource] = useState<"sample" | "upload">("sample"), [sample, setSample] = useState(""), [file, setFile] = useState<File | null>(null);
  const [ack, setAck] = useState(false), [pending, setPending] = useState(false), [discard, setDiscard] = useState(false);
  const working = useRef(false);
  const mutable = identity.role !== "presenter", reviewer = identity.role === "adjuster";
  const slot = evidenceStorageSlot(identity.run_id, identity.actor_key, claim.claim_id);
  useEffect(() => {
    try { setPending(readCommand(window.sessionStorage, slot) !== null); }
    catch (e) { setError(e); setPending(true); setLocked(true); }
  }, [slot]);
  useEffect(() => {
    let current = true; setLoading(true); setError(null);
    evidence.status().then(async info => {
      if (!current) return;
      setStatus(info);
      const documents = await evidence.documents(claim.claim_id, offset);
      if (!current) return;
      setPage(documents);
      if (info.enabled) {
        const catalog = await evidence.samples();
        if (current) setSamples(catalog);
      }
    }).catch(e => { if (current) { setError(e); onError(e); } }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [evidence, claim.claim_id, offset, reload, onError]);
  useEffect(() => {
    if (!selected || !status?.worker_enabled || !status.extraction_enabled) return;
    let current = true, polls = 0;
    let timer: ReturnType<typeof setTimeout>;
    const documentId = selected.document_id, jobId = selected.job_id;
    async function poll() {
      try {
        const result = await evidence.job(jobId, claim.claim_id, documentId);
        if (!current) return;
        setJob(result);
        if (result.status === "completed") {
          const document = await evidence.document(documentId, claim.claim_id);
          if (current) setSelected(document);
        } else if (["queued", "running"].includes(result.status) && ++polls < 120) timer = setTimeout(() => void poll(), 5000);
        else if (polls >= 120) setNotice("Automatic polling paused after ten minutes. Read current evidence to continue; the durable job does not depend on this page.");
      } catch (e) { if (current) { setError(e); onError(e); setNotice("Automatic polling paused after an error. No processing command was repeated."); } }
    }
    void poll();
    return () => { current = false; clearTimeout(timer); };
  }, [evidence, selected?.document_id, status?.worker_enabled, status?.extraction_enabled, claim.claim_id, onError]);
  async function read(document: EvidenceDocument) {
    if (working.current) return;
    working.current = true; setBusy(true); setError(null);
    try {
      const updated = await evidence.document(document.document_id, claim.claim_id);
      const updatedJob = await evidence.job(updated.job_id, claim.claim_id, updated.document_id);
      setSelected(updated); setJob(updatedJob); setLocked(false);
    } catch (e) { setError(e); onError(e); setLocked(true); }
    finally { working.current = false; setBusy(false); }
  }
  async function attach(event: FormEvent) {
    event.preventDefault();
    if (working.current || busy || caseBusy || caseLocked || locked || !ack || !mutable || !status?.enabled) return;
    if (source === "sample" && !sample || source === "upload" && !file) { setError(new Error("Choose a synthetic source document.")); return; }
    working.current = true; setBusy(true); setError(null); setNotice("");
    try {
      const filename = source === "sample" ? sample : file?.name || "";
      if (source === "upload") evidenceMediaType(filename);
      const command = await evidenceCommand(window.sessionStorage, slot, source, filename, source === "upload" && file ? file : undefined);
      setPending(true);
      const result = source === "sample" ? await evidence.sample(claim.claim_id, sample, claim.version, command.key) :
        file ? await evidence.upload(claim.claim_id, file, claim.version, command.key) : null;
      if (!result) throw new Error("No evidence submission was made.");
      const warning = clearConfirmedCommand(window.sessionStorage, slot, "document");
      setSelected(result.document); setJob(result.job); setPending(!!warning); setAck(false);
      setNotice(`${result.replayed ? "Original evidence submission recovered." : "Document stored and durable processing job queued."} No extracted facts have been confirmed.${warning ? ` ${warning}` : ""}`);
      setReload(value => value + 1); onCaseChanged();
    } catch (e) { setError(e); onError(e); setPending(true); setLocked(true); }
    finally { working.current = false; setBusy(false); }
  }
  async function command(action: () => Promise<void>) {
    if (working.current || busy || locked) return;
    working.current = true; setBusy(true); setError(null); setNotice("");
    try { await action(); onCaseChanged(); }
    catch (e) { setError(e); onError(e); setLocked(true); }
    finally { working.current = false; setBusy(false); }
  }
  async function download(document: EvidenceDocument) {
    try {
      setError(null);
      const bytes = await evidence.source(document), url = URL.createObjectURL(bytes);
      const link = window.document.createElement("a");
      link.href = url; link.download = document.filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (e) { setError(e); onError(e); }
  }
  function discardCommand() {
    if (!discard || busy) return;
    try { window.sessionStorage.removeItem(slot); setPending(false); setDiscard(false); setLocked(false); setError(null); setFile(null); setSample(""); }
    catch { setError(new Error("The retry key could not be cleared. No document was submitted.")); }
  }
  return <section className="business-stack" aria-labelledby="documents-title">
    <div className="business-heading"><h3 id="documents-title">Documents and evidence review</h3><button disabled={loading || busy} onClick={() => { setReload(value => value + 1); if (selected) void read(selected); else { setLocked(false); onCaseChanged(); } }}>Read current evidence</button></div>
    <ErrorNotice error={error} />{loading && <Loading>Reading authorised document records…</Loading>}
    {notice && <p role="status" className="business-callout">{notice}</p>}
    {status && <p className="business-callout">{status.notice} {!status.worker_enabled || !status.extraction_enabled ? "Automatic extraction is paused. Stored sources and manual field review do not require a model." : "Processing is asynchronous. Closing this page does not cancel a durable job."}</p>}
    {pending && mutable && <aside className="business-panel"><h4>An evidence retry key is saved</h4><p>Only its key and content fingerprint are stored in this tab. After reading current evidence, explicitly retry the exact source; do not select a different file under the same key.</p>
      <label className="business-check"><input type="checkbox" checked={discard} disabled={busy} onChange={event => setDiscard(event.target.checked)} />I inspected this case's documents and want to discard the retry key. A separate submission may duplicate an earlier stored document.</label>
      <button disabled={!discard || busy} onClick={discardCommand}>Discard key for a separate submission</button></aside>}
    {status?.enabled && mutable && <form className="business-panel business-stack" onSubmit={event => void attach(event)}>
      <h4>Attach synthetic evidence</h4><fieldset disabled={busy || caseBusy || locked || caseLocked}>
        <legend>Source document</legend><label htmlFor="evidence-source">Source</label><select id="evidence-source" value={source} onChange={event => { if (event.target.value === "sample" || event.target.value === "upload") setSource(event.target.value); }}>
          <option value="sample">Allowlisted synthetic sample</option><option value="upload" disabled={!status.uploads_enabled}>Synthetic upload {status.uploads_enabled ? "(operator enabled)" : "(not enabled)"}</option></select>
        {source === "sample" ? <><label htmlFor="evidence-sample">Sample</label><select required id="evidence-sample" value={sample} onChange={event => setSample(event.target.value)}><option value="">Select a source</option>{samples.map(item => <option key={item.filename} value={item.filename}>{item.title} · {item.filename}</option>)}</select></> :
          <><label htmlFor="evidence-upload">PDF, JPEG or PNG — maximum 10 MiB</label><input id="evidence-upload" type="file" required accept=".pdf,.jpg,.jpeg,.png" onChange={event => {
            const selectedFile = event.target.files?.[0] || null;
            if (selectedFile && (selectedFile.size < 1 || selectedFile.size > status.max_upload_bytes)) { setError(new Error("Choose a nonempty document no larger than 10 MiB.")); setFile(null); event.target.value = ""; }
            else setFile(selectedFile);
          }} /></>}
        <label className="business-check"><input type="checkbox" required checked={ack} onChange={event => setAck(event.target.checked)} />I confirm this document contains synthetic information only, not a real claim or personal data.</label>
        <button type="submit" disabled={!ack}>{pending ? "Explicitly retry the same document" : "Store document and queue processing"}</button>
      </fieldset></form>}
    {page && <><ul className="business-list">{page.items.map(document => <li key={document.document_id}><strong>{document.filename}</strong><p className="business-muted">{document.source_mode} · {document.size_bytes.toLocaleString()} bytes · <DateText value={document.created_at} /></p><button disabled={busy} onClick={() => void read(document)}>Inspect {document.filename}</button></li>)}</ul>
      {!page.items.length && <p>No durable documents are attached to this case.</p>}<Pager offset={offset} next={page.next_offset} history={history} busy={loading || busy} onPage={(next, backwards) => { setHistory(value => backwards ? value.slice(0, -1) : [...value, offset]); setOffset(next); }} /></>}
    {selected && <section className="business-panel business-stack" aria-label={`Evidence ${selected.filename}`}>
      <h4>{selected.filename}</h4><p>Source SHA-256: <code>{selected.sha256}</code></p><p>{selected.notice}</p>
      <button disabled={busy} onClick={() => void download(selected)}>Download verified original</button>
      {job && <aside><h4>Processing: {job.status}</h4><p>Attempt {job.attempts}/{job.max_attempts} · {job.notice}</p>
        {job.error_message && <p role="status">{job.error_message} · {job.error_code}</p>}
        {reviewer && <div className="business-actions">
          {["queued", "running"].includes(job.status) && <button disabled={busy || locked} onClick={() => void command(async () => { setJob(await evidence.jobCommand("cancel", job.job_id, claim.claim_id, selected.document_id, job.version)); setNotice("Cancellation recorded. Provider compute already submitted may still run."); })}>Cancel further processing</button>}
          {job.status === "failed" && job.attempts < job.max_attempts && job.error_code !== "cu_submission_uncertain" && <button disabled={busy || locked} onClick={() => void command(async () => { setJob(await evidence.jobCommand("retry", job.job_id, claim.claim_id, selected.document_id, job.version)); setNotice("Explicit retry queued. No facts were confirmed."); })}>Queue explicit retry</button>}
        </div>}
      </aside>}
      {reviewer ? <><h4>Compare suggestions with the original source</h4><p>Each field is a separate human decision. Manual entry is available without extraction. Confirming a value does not approve coverage, settle a claim or make a payment.</p>
        {evidenceFieldNames.map(name => <FieldReviewForm key={`${selected.document_id}-${selected.version}-${selected.review_version}-${name}`} document={selected} name={name} busy={busy} locked={locked} review={field => command(async () => {
          setSelected(await evidence.review(selected, [field])); setNotice("Human field review saved separately from the raw extraction.");
        })} />)}</> :
        <><h4>Human-reviewed facts</h4>{evidenceFieldNames.filter(name => !!selected.confirmed_fields[name]).map(name => <p key={name}><strong>{statusLabel(name)}:</strong> {selected.confirmed_fields[name]?.decision === "clear" ? "Explicitly cleared" : String(selected.confirmed_fields[name]?.value ?? "No value")}</p>)}
          {!Object.keys(selected.confirmed_fields).length && <p>No fields have been confirmed by an adjuster.</p>}</>}
    </section>}
  </section>;
}
