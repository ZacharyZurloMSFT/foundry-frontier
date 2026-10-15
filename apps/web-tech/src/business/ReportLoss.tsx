import { FormEvent, useEffect, useRef, useState } from "react";
import { BusinessApi, BusinessError } from "./api";
import { ClaimRecord, Identity, LossReport, Page, PolicySnapshot } from "./models";
import { clearConfirmedCommand, prepareReportCommand, readCommand, reportStorageKey, ReportStorageError } from "./reportRetry";
import { ErrorNotice, Loading, Pager } from "./shared";

export function validateLossReport(report: LossReport, today = new Date().toISOString().slice(0, 10)): string | null {
  if (!report.policy_id || !report.vehicle_id) return "Choose an available policy and its insured vehicle.";
  if (!["auto_collision", "auto_hail", "auto_other"].includes(report.incident_type)) return "Choose a supported incident type.";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(report.loss_date) || !Number.isFinite(Date.parse(report.loss_date))
    || new Date(`${report.loss_date}T00:00:00Z`).toISOString().slice(0, 10) !== report.loss_date || report.loss_date > today) return "Enter a valid loss date that is not in the future.";
  if (!/^[A-Z]{2}$/.test(report.state)) return "Enter a two-letter uppercase loss state.";
  if (report.description.trim().length < 10 || report.description.length > 2000) return "Describe the loss using 10 to 2,000 characters.";
  if (report.synthetic_acknowledged !== true) return "Acknowledge that this report uses synthetic data.";
  return null;
}
export default function ReportLoss({ api, identity, initialPolicy, onCreated, onClaims, onError }: {
  api: BusinessApi; identity: Identity; initialPolicy: PolicySnapshot | null;
  onCreated: (claim: ClaimRecord, replayed: boolean, warning: string | null) => void; onClaims: () => void; onError: (error: unknown) => void;
}) {
  const [policy, setPolicy] = useState<PolicySnapshot | null>(initialPolicy);
  const [page, setPage] = useState<Page<PolicySnapshot> | null>(null), [offset, setOffset] = useState(0), [history, setHistory] = useState<number[]>([]);
  const [incident, setIncident] = useState<LossReport["incident_type"]>("auto_collision");
  const [lossDate, setLossDate] = useState(""), [state, setState] = useState(""), [description, setDescription] = useState("");
  const [acknowledged, setAcknowledged] = useState(false), [busy, setBusy] = useState(false), [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null), [pending, setPending] = useState(false), [resetChecked, setResetChecked] = useState(false);
  const [uncertain, setUncertain] = useState(false), [storageBlocked, setStorageBlocked] = useState(false);
  const sending = useRef(false);
  const slot = reportStorageKey(identity.run_id, identity.policyholder_id || "");
  useEffect(() => {
    try { setPending(readCommand(window.sessionStorage, slot) !== null); }
    catch (e) { setError(e); setPending(true); setStorageBlocked(true); }
  }, [slot]);
  useEffect(() => {
    let current = true; setLoading(true); setPage(null);
    api.policies(offset).then(p => { if (current) setPage(p); }).catch(e => { if (current) { setError(e); onError(e); } }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, offset, onError]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (sending.current || !policy || !acknowledged || storageBlocked) return;
    const report: LossReport = { policy_id: policy.policy.policy_id, vehicle_id: policy.policy.insured_asset.vin,
      incident_type: incident, loss_date: lossDate, state: state.trim().toUpperCase(), description: description.trim(), synthetic_acknowledged: true };
    const validation = validateLossReport(report);
    if (validation) { setError(new Error(validation)); return; }
    sending.current = true; setBusy(true); setError(null);
    try {
      const command = await prepareReportCommand(report, window.sessionStorage, slot);
      setPending(true);
      const result = await api.create(report, command.key);
      const warning = clearConfirmedCommand(window.sessionStorage, slot);
      onCreated(result.claim, result.replayed, warning);
    } catch (e) {
      setError(e); onError(e);
      if (e instanceof ReportStorageError) setStorageBlocked(true);
      else setPending(true);
      if (e instanceof BusinessError && e.uncertain) setUncertain(true);
    } finally { sending.current = false; setBusy(false); }
  }
  function startNew() {
    if (!resetChecked || busy) return;
    try {
      window.sessionStorage.removeItem(slot); setPending(false); setUncertain(false); setStorageBlocked(false); setError(null); setResetChecked(false);
      setPolicy(null); setIncident("auto_collision"); setLossDate(""); setState(""); setDescription(""); setAcknowledged(false);
    } catch { setError(new Error("Session storage is unavailable. This portal cannot safely preserve a report command across refresh. No report was sent.")); }
  }
  return <section aria-labelledby="report-title">
    <p className="business-eyebrow">One step at a time</p><h2 id="report-title">Report an auto loss</h2>
    <p>Tell us what happened. A submitted report opens a case for human review; it does not determine coverage or authorize payment.</p>
    <p className="business-callout">Use synthetic information only. Do not enter real personal details, real loss information, or sensitive documents.</p>
    {pending && <aside className="business-panel" aria-label="Report recovery">
      <h3>{uncertain ? "Report outcome is uncertain" : "A report command is saved in this tab"}</h3>
      <p>No automatic submission will happen. To safely retry, re-enter the exact original fields and submit; the same command key is reused, even after refresh. Edited reports cannot reuse that command.</p>
      <p>Only a key and report hash are saved in session storage — never the description, sign-in token, or report itself.</p>
      <button type="button" onClick={onClaims} disabled={busy}>Check my claims first</button>
      <label className="business-check"><input type="checkbox" checked={resetChecked} disabled={busy} onChange={e => setResetChecked(e.target.checked)} />I checked my claims and want to discard this retry key and start a separate report. This could create a duplicate if the earlier report was saved.</label>
      <button type="button" disabled={!resetChecked || busy} onClick={startNew}>Start a separate report</button>
    </aside>}
    <ErrorNotice error={error} />
    <form onSubmit={e => void submit(e)} className="business-panel business-stack">
      <fieldset disabled={busy}>
        <legend>1. Select your policy</legend>
        {loading && <Loading />}
        <label htmlFor="loss-policy">Policy</label>
        <select id="loss-policy" required value={policy?.policy.policy_id || ""} onChange={e => setPolicy(page?.items.find(p => p.policy.policy_id === e.target.value) || (initialPolicy?.policy.policy_id === e.target.value ? initialPolicy : null))}>
          <option value="">Select an available policy</option>
          {policy && !page?.items.some(p => p.policy.policy_id === policy.policy.policy_id) && <option value={policy.policy.policy_id}>{policy.policy.policy_id} · {policy.policy.insured_asset.description}</option>}
          {page?.items.map(p => <option value={p.policy.policy_id} key={p.policy.policy_id}>{p.policy.policy_id} · {p.policy.insured_asset.description}</option>)}
        </select>
        {page && <Pager offset={offset} next={page.next_offset} history={history} busy={busy || loading} onPage={(next, back) => { setHistory(h => back ? h.slice(0, -1) : [...h, offset]); setOffset(next); }} />}
        {policy && <p>Insured vehicle: {policy.policy.insured_asset.description}<br />VIN: <code>{policy.policy.insured_asset.vin}</code> · Source: {policy.source_version}</p>}
      </fieldset>
      <fieldset disabled={busy}>
        <legend>2. Describe the incident</legend>
        <label htmlFor="loss-incident">Incident type</label>
        <select id="loss-incident" value={incident} onChange={e => {
          const value = e.target.value; if (value === "auto_collision" || value === "auto_hail" || value === "auto_other") setIncident(value);
        }}><option value="auto_collision">Auto collision</option><option value="auto_hail">Auto hail</option><option value="auto_other">Other auto loss</option></select>
        <div className="business-grid">
          <div><label htmlFor="loss-date">Loss date</label><input id="loss-date" type="date" required max={new Date().toISOString().slice(0, 10)} value={lossDate} onChange={e => setLossDate(e.target.value)} /></div>
          <div><label htmlFor="loss-state">State where the loss occurred</label><input id="loss-state" autoCapitalize="characters" required maxLength={2} pattern="[A-Za-z]{2}" placeholder="WA" value={state} onChange={e => setState(e.target.value.toUpperCase())} aria-describedby="loss-state-help" /><small id="loss-state-help">Two-letter US state abbreviation</small></div>
        </div>
        <label htmlFor="loss-description">What happened?</label><textarea id="loss-description" required minLength={10} maxLength={2000} rows={6} value={description} onChange={e => setDescription(e.target.value)} aria-describedby="loss-description-help" />
        <small id="loss-description-help">{description.length}/2,000 characters. Include only synthetic incident facts.</small>
      </fieldset>
      <label className="business-check"><input type="checkbox" required checked={acknowledged} disabled={busy} onChange={e => setAcknowledged(e.target.checked)} />I understand this is a synthetic insurance demonstration and confirm this report contains synthetic information only.</label>
      <p className="business-muted">Document uploads, extraction, an assistant, and voice are not integrated in this manual journey. No external messages are sent.</p>
      <button className="business-primary" type="submit" disabled={busy || !policy || !acknowledged || storageBlocked}>{busy ? "Submitting report…" : pending ? "Explicitly retry this exact report" : "Submit loss report"}</button>
    </form>
  </section>;
}
