import { FormEvent, useState } from "react";
import { BusinessApi } from "./api";
import { ClaimRecord, ClaimStatus, nextStatuses, statusLabel } from "./models";

export default function CaseCommands({ claim, adjusters, api, busy, locked, execute }: {
  claim: ClaimRecord; adjusters: string[]; api: BusinessApi; busy: boolean; locked: boolean;
  execute: (command: () => Promise<ClaimRecord>) => Promise<void>;
}) {
  const targets = nextStatuses(claim.status);
  const [target, setTarget] = useState<ClaimStatus>(targets[0]), [reason, setReason] = useState("");
  const [taskTitle, setTaskTitle] = useState(""), [evidence, setEvidence] = useState(""), [taskAssignee, setTaskAssignee] = useState("");
  const [note, setNote] = useState(""), [visibility, setVisibility] = useState<"customer" | "internal">("internal");
  const [assignee, setAssignee] = useState(claim.assigned_adjuster_id || "");
  function submit(event: FormEvent, command: () => Promise<ClaimRecord>) { event.preventDefault(); if (!busy && !locked) void execute(command); }
  const version = claim.version;
  return <section aria-labelledby="case-actions-title" className="business-panel business-stack">
    <h3 id="case-actions-title">Adjuster actions</h3>
    <p>Current case version: <strong>{version}</strong>. Each action checks this version on the server. You are not authorizing coverage, settlement, payment, or an external communication.</p>
    {locked && <p className="business-callout" role="status">Actions are paused after a conflict or uncertain result. Use “Read current case” above, review the record, and choose an explicit new action. Nothing will be resubmitted automatically.</p>}
    <fieldset disabled={busy || locked}>
      <legend>Manual case management</legend>
      <form onSubmit={e => submit(e, () => api.transition(claim.claim_id, version, target, reason.trim()))} className="business-command">
        <h4>Move workflow</h4>
        <p>Current stage: <strong>{statusLabel(claim.status)}</strong>. The server makes the final transition check.</p>
        <label htmlFor="case-target">Next stage</label><select id="case-target" required value={target} onChange={e => { const t = targets.find(s => s === e.target.value); if (t) setTarget(t); }}>{targets.map(t => <option key={t} value={t}>{statusLabel(t)}</option>)}</select>
        <label htmlFor="case-reason">Reason for transition</label><textarea id="case-reason" required minLength={1} maxLength={2000} rows={3} value={reason} onChange={e => setReason(e.target.value)} />
        <button disabled={!reason.trim()} type="submit">Apply transition at version {version}</button>
      </form>
      <form onSubmit={e => submit(e, () => api.task(claim.claim_id, version, taskTitle.trim(), evidence.trim(), taskAssignee || null))} className="business-command">
        <h4>Create evidence task</h4>
        <label htmlFor="case-task-title">Task title</label><input id="case-task-title" required maxLength={200} value={taskTitle} onChange={e => setTaskTitle(e.target.value)} />
        <label htmlFor="case-evidence">Requested evidence</label><input id="case-evidence" required maxLength={80} value={evidence} onChange={e => setEvidence(e.target.value)} />
        <label htmlFor="case-task-assignee">Task assignee</label><select id="case-task-assignee" value={taskAssignee} onChange={e => setTaskAssignee(e.target.value)}><option value="">Unassigned</option>{adjusters.map(id => <option key={id} value={id}>{id}</option>)}</select>
        <button disabled={!taskTitle.trim() || !evidence.trim()} type="submit">Create task at version {version}</button>
      </form>
      <form onSubmit={e => submit(e, () => api.note(claim.claim_id, version, note.trim(), visibility))} className="business-command">
        <h4>Add case note</h4>
        <label htmlFor="case-note">Note</label><textarea id="case-note" required maxLength={2000} rows={4} value={note} onChange={e => setNote(e.target.value)} />
        <label htmlFor="case-note-visibility">Note visibility</label><select id="case-note-visibility" value={visibility} onChange={e => { if (e.target.value === "customer" || e.target.value === "internal") setVisibility(e.target.value); }}><option value="internal">Internal — employees only</option><option value="customer">Customer-visible in this portal</option></select>
        <p className="business-muted">Saving a note does not email, text, or otherwise contact the policyholder.</p>
        <button disabled={!note.trim()} type="submit">Save note at version {version}</button>
      </form>
      <form onSubmit={e => submit(e, () => api.assign(claim.claim_id, version, assignee || null))} className="business-command">
        <h4>Assign case</h4>
        <label htmlFor="case-assignee">Permitted adjuster</label><select id="case-assignee" value={assignee} onChange={e => setAssignee(e.target.value)}><option value="">Unassigned</option>
          {claim.assigned_adjuster_id && !adjusters.includes(claim.assigned_adjuster_id) && <option value={claim.assigned_adjuster_id} disabled>{claim.assigned_adjuster_id} (not available for reassignment)</option>}
          {adjusters.map(id => <option key={id} value={id}>{id}</option>)}</select>
        <button disabled={assignee === (claim.assigned_adjuster_id || "")} type="submit">Save assignment at version {version}</button>
      </form>
    </fieldset>
  </section>;
}
