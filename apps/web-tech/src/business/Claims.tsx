import { useEffect, useRef, useState } from "react";
import { BusinessApi, BusinessError } from "./api";
import { ClaimRecord, Identity, Page, statusLabel } from "./models";
import { DateText, ErrorNotice, Loading, Pager } from "./shared";
import { PolicyView } from "./Policies";
import CaseCommands from "./CaseCommands";
import DocumentsPanel from "./DocumentsPanel";
import AssistantPanel from "./assistants/AssistantPanel";

export function ClaimDetail({ api, initialClaim, claimId, identity, onError }: {
  api: BusinessApi; initialClaim: ClaimRecord | null; claimId: string; identity: Identity; onError: (error: unknown) => void;
}) {
  const [claim, setClaim] = useState<ClaimRecord | null>(initialClaim);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [locked, setLocked] = useState(false);
  const [notice, setNotice] = useState(""), [reload, setReload] = useState(0);
  const executing = useRef(false);
  const title = useRef<HTMLHeadingElement>(null);
  useEffect(() => { title.current?.focus(); }, [claimId]);
  useEffect(() => {
    let current = true; setBusy(true); setError(null);
    api.claim(claimId).then(data => { if (current) { setClaim(data); setLocked(false); setNotice(`Current case read at version ${data.version}.`); } })
      .catch(e => { if (current) { setError(e); onError(e); setLocked(true); } }).finally(() => { if (current) setBusy(false); });
    return () => { current = false; };
  }, [api, claimId, reload, onError]);
  async function execute(command: () => Promise<ClaimRecord>) {
    if (executing.current || busy || locked) return;
    executing.current = true; setBusy(true); setError(null); setNotice("");
    try {
      const updated = await command();
      setClaim(updated); setNotice(`Action saved. Current case version is ${updated.version}.`);
    } catch (e) {
      setError(e); onError(e);
      // A read is required after every failed command; never reuse a new version implicitly.
      setLocked(true);
    } finally { executing.current = false; setBusy(false); }
  }
  const customer = identity.role === "policyholder";
  const notes = claim?.notes.filter(n => !customer || n.visibility === "customer") || [];
  const timeline = claim?.timeline.filter(t => !customer || t.visibility === "customer") || [];
  return <section aria-labelledby="claim-detail-title" className="business-stack">
    <div className="business-heading"><div><p className="business-eyebrow">Connected case record</p><h2 ref={title} tabIndex={-1} id="claim-detail-title">{claimId}</h2></div><button disabled={busy} onClick={() => setReload(v => v + 1)}>Read current case</button></div>
    <ErrorNotice error={error} />{busy && <Loading>Reading or saving connected case…</Loading>}
    <p role="status" aria-live="polite" className="business-muted">{notice}</p>
    {claim && <>
      <article className="business-panel">
        <div className="business-heading"><h3>{statusLabel(claim.incident_type)}</h3><span className="business-badge">{statusLabel(claim.status)}</span></div>
        <dl className="business-facts">
          <div><dt>Loss date</dt><dd><DateText value={claim.loss_date} /></dd></div><div><dt>Loss state</dt><dd>{claim.state}</dd></div>
          <div><dt>Policy</dt><dd>{claim.policy_id}</dd></div><div><dt>Case version</dt><dd>{claim.version}</dd></div>
          {!customer && <div><dt>Assigned adjuster</dt><dd>{claim.assigned_adjuster_id || "Not yet assigned"}</dd></div>}<div><dt>Updated</dt><dd><DateText value={claim.updated_at} /></dd></div>
        </dl>
        <h4>Reported facts</h4><p className="business-prewrap">{claim.description}</p>
        <h4>Next evidence steps</h4>
        {claim.outstanding_evidence.length ? <ul>{claim.outstanding_evidence.map((e, i) => <li key={i}>{e}</li>)}</ul> : <p>No outstanding evidence is currently listed. This is not a coverage decision.</p>}
        {claim.review_flags.length > 0 && <><h4>Review flags</h4><ul>{claim.review_flags.map((flag, i) => <li key={i}>{statusLabel(flag)}</li>)}</ul></>}
        <p className="business-muted">Evidence is managed below when enabled. Voice is not integrated with this case.</p>
      </article>
      <AssistantPanel surface="concierge" context={{ claim_id: claim.claim_id }} onError={onError} />
      <DocumentsPanel claim={claim} identity={identity} api={api} caseBusy={busy} caseLocked={locked}
        onCaseChanged={() => setReload(value => value + 1)} onError={onError} />
      <section className="business-panel" aria-labelledby="case-tasks-title"><h3 id="case-tasks-title">Evidence tasks</h3>
        {!claim.tasks.length && <p>No evidence tasks have been created.</p>}
        <ul className="business-list">{claim.tasks.map(task => <li key={task.task_id}><div className="business-heading"><h4>{task.title}</h4><span className="business-badge">{task.status}</span></div>
          <p>{task.requested_evidence}</p><p className="business-muted">{!customer && <>Assigned to: {task.assigned_to || "Unassigned"} · </>}Updated <DateText value={task.updated_at} /></p>
          {task.completed_at && <p>Completed <DateText value={task.completed_at} /></p>}
          {identity.role === "adjuster" && task.status === "open" && <button disabled={busy || locked} onClick={() => void execute(() => api.complete(claim.claim_id, task.task_id, claim.version))}>Mark “{task.title}” completed at version {claim.version}</button>}
        </li>)}</ul>
      </section>
      <section className="business-panel" aria-labelledby="case-notes-title"><h3 id="case-notes-title">{customer ? "Case updates" : "Case notes"}</h3>
        {!notes.length && <p>No {customer ? "customer-visible updates" : "notes"} yet.</p>}
        <ul className="business-list">{notes.map(note => <li key={note.note_id}><p className="business-prewrap">{note.body}</p><p className="business-muted">{note.visibility === "internal" ? "Internal" : "Customer-visible"} · <DateText value={note.created_at} />{!customer && ` · Author: ${note.author_id || "Unavailable"}`}</p></li>)}</ul>
      </section>
      <section className="business-panel" aria-labelledby="case-timeline-title"><h3 id="case-timeline-title">Case timeline</h3>
        {!timeline.length && <p>No visible timeline events returned.</p>}
        <ol className="business-timeline">{timeline.map(event => <li key={event.event_id}><p className="business-muted"><DateText value={event.occurred_at} /> · {event.actor_role}</p>
          <p>{event.summary}</p><p className="business-muted">Version {event.before_version} → {event.after_version}{!customer && ` · ${event.visibility}`}</p>
          {!customer && event.internal_note && <p className="business-prewrap">Internal note: {event.internal_note}</p>}
        </li>)}</ol>
      </section>
      {identity.role === "adjuster" && <CaseCommands key={`${claim.claim_id}-${claim.version}-${reload}`} claim={claim} adjusters={identity.adjusters} api={api} busy={busy} locked={locked} execute={execute} />}
      {identity.role === "presenter" && <p className="business-callout">Presenter access is read-only. No case commands are available.</p>}
      <details className="business-panel"><summary>Policy snapshot recorded with this claim</summary><PolicyView snapshot={claim.policy_snapshot} /></details>
    </>}
  </section>;
}
export default function Claims({ api, identity, selected, onSelect, createdNotice, onError }: {
  api: BusinessApi; identity: Identity; selected: ClaimRecord | string | null; onSelect: (claim: ClaimRecord | string | null) => void;
  createdNotice: string; onError: (error: unknown) => void;
}) {
  const [page, setPage] = useState<Page<ClaimRecord> | null>(null);
  const [offset, setOffset] = useState(0), [history, setHistory] = useState<number[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [reload, setReload] = useState(0);
  const [lookup, setLookup] = useState("");
  useEffect(() => {
    if (selected) return;
    let current = true; setBusy(true); setPage(null); setError(null);
    api.claims(offset).then(data => { if (current) setPage(data); }).catch(e => { if (current) { setError(e); onError(e); } }).finally(() => { if (current) setBusy(false); });
    return () => { current = false; };
  }, [api, offset, reload, selected, onError]);
  if (selected) return <>
    <button className="business-back" onClick={() => onSelect(null)}>← Back to {identity.role === "policyholder" ? "my claims" : "claim queue"}</button>
    {createdNotice && <div className="business-callout" role="status">{createdNotice}</div>}
    <ClaimDetail key={typeof selected === "string" ? selected : selected.claim_id} api={api} initialClaim={typeof selected === "string" ? null : selected} claimId={typeof selected === "string" ? selected : selected.claim_id} identity={identity} onError={onError} />
  </>;
  return <section aria-labelledby="claims-title">
    <div className="business-heading"><div><p className="business-eyebrow">Every case, one clear record</p><h2 id="claims-title">{identity.role === "policyholder" ? "My claims" : "Claim queue"}</h2></div><button disabled={busy} onClick={() => setReload(v => v + 1)}>Refresh claims</button></div>
    <p>{identity.role === "policyholder" ? "Follow your report and see evidence requests and case updates." : "Open a connected case to review evidence, tasks, notes, assignment, and workflow."}</p>
    <AssistantPanel surface="concierge" onError={onError} />
    <form className="business-lookup" onSubmit={e => { e.preventDefault(); if (lookup.trim()) onSelect(lookup.trim()); }}>
      <label htmlFor="claim-lookup">Find by exact claim number</label><div className="business-actions"><input id="claim-lookup" required maxLength={128} value={lookup} onChange={e => setLookup(e.target.value)} /><button type="submit">Open case</button></div>
    </form>
    <ErrorNotice error={error} />{busy && <Loading />}
    {page && <>
      {!page.items.length && <div className="business-panel"><h3>No claims on this page</h3><p>No case records were returned for this signed-in account and run.</p></div>}
      <div className="business-grid">{page.items.map(claim => <article className="business-panel" key={claim.claim_id}>
        <div className="business-heading"><h3>{claim.claim_id}</h3><span className="business-badge">{statusLabel(claim.status)}</span></div>
        <p>{statusLabel(claim.incident_type)} · <DateText value={claim.loss_date} /></p><p>{claim.policy_snapshot.policy.insured_asset.description}</p>
        <p className="business-muted">{claim.outstanding_evidence.length} outstanding evidence item(s) · Version {claim.version}</p>
        <button onClick={() => onSelect(claim)}>View case {claim.claim_id}</button>
      </article>)}</div>
      <Pager offset={offset} next={page.next_offset} history={history} busy={busy} onPage={(next, back) => { setHistory(h => back ? h.slice(0, -1) : [...h, offset]); setOffset(next); }} />
    </>}
    {error instanceof BusinessError && error.uncertain && <p>No claims were fabricated.</p>}
  </section>;
}
