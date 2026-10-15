import { useEffect, useRef, useState } from "react";
import {
  ExistingRunError, getHarnessRun, getHarnessStatus, streamHarnessRun,
  type HarnessEvent, type HarnessRun, type HarnessStatus, type RunState,
} from "../harnessApi";
import "../harness.css";

const HISTORY_KEY = "northwind.harness.runs";
const SCENARIOS = [
  ["Claim status", "Summarize claim CLM-2001 and its outstanding evidence. Do not infer a coverage decision."],
  ["Policy question", "Explain the collision deductible recorded on POL-1001, citing the policy version and jurisdiction. Do not adjudicate coverage."],
  ["Human review", "Draft a request for the outstanding evidence on CLM-2001. Look up the claim and policy first, cite sources, and stop for human review."],
];
function history(): string[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(HISTORY_KEY) ?? "[]");
    return Array.isArray(saved) ? saved.filter((id): id is string => typeof id === "string").slice(0, 10) : [];
  } catch (error) { console.warn("Could not restore harness run history", error); return []; }
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }

export function insuranceReview(value: unknown, workflowId?: string) {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  const event = v.kind === "approval_pending";
  const kind = event ? v.action_kind : v.kind;
  const workflow = event ? v.workflow_id : v.workflowId;
  const payload = event ? v.draft : v.payload;
  if (kind !== "insurance_claim_action" || v.execution_status !== "not_executed" ||
      typeof workflow !== "string" || !workflow.startsWith("wf-harness-") ||
      (workflowId && workflow !== workflowId) || !payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  return { payload, status: event ? "pending" : String(v.status), workflowId: workflow };
}

export default function AgentHarnessTab({ active, onBack, onApprovals }: {
  active: boolean; onBack: () => void; onApprovals: () => void;
}) {
  const [status, setStatus] = useState<HarnessStatus | null>(null);
  const [input, setInput] = useState(SCENARIOS[2][1]);
  const [events, setEvents] = useState<HarnessEvent[]>([]);
  const [saved, setSaved] = useState<HarnessRun | null>(null);
  const [ids, setIds] = useState(history);
  const [runId, setRunId] = useState<string | null>(() => history()[0] ?? null);
  const [state, setState] = useState<RunState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState("");
  const controller = useRef<AbortController | null>(null);
  const currentRun = useRef<string | null>(runId);
  const mounted = useRef(true);

  async function load(id: string) {
    const run = await getHarnessRun(id);
    if (!mounted.current || currentRun.current !== id) return;
    setRunId(id); setSaved(run); setEvents(run.events); setState(run.status);
    setOutput(run.output || run.events.reduce((text, event) =>
      event.kind === "assistant_message" ? event.text : event.kind === "assistant_delta" ? text + event.text : text, ""));
    if (run.error) setError(run.error);
  }
  function remember(id: string) {
    currentRun.current = id;
    setRunId(id);
    setIds((previous) => {
      const next = [id, ...previous.filter((v) => v !== id)].slice(0, 10);
      try { localStorage.setItem(HISTORY_KEY, JSON.stringify(next)); }
      catch (e) { console.warn("Could not save harness run history", e); }
      return next;
    });
  }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controller.current?.abort(); };
  }, []);
  useEffect(() => {
    if (!active) { controller.current?.abort(); return; }
    let cancelled = false;
    getHarnessStatus().then((s) => { if (!cancelled) setStatus(s); })
      .catch((e: unknown) => { if (!cancelled) setError(message(e)); });
    return () => { cancelled = true; };
  }, [active]);
  useEffect(() => {
    if (!active || busy || !runId) return;
    load(runId).catch((e: unknown) => setError(message(e)));
    if (state !== "running" && saved?.approval?.status !== "pending") return;
    const timer = window.setInterval(() => {
      load(runId).catch((e: unknown) => setError(message(e)));
    }, 3000);
    return () => clearInterval(timer);
  }, [active, busy, runId, state, saved?.approval?.status]);

  async function run() {
    if (busy || !input.trim() || !ready) return;
    const abort = new AbortController();
    controller.current = abort;
    currentRun.current = null;
    setBusy(true); setSaved(null); setRunId(null); setEvents([]); setOutput("");
    setError(null); setState("running");
    try {
      const latest = await getHarnessStatus();
      if (!latest.enabled || !["configured", "ready"].includes(latest.state)) {
        throw new Error(latest.message || "The insurance SDK harness is not available. No prompt was submitted.");
      }
      for await (const event of streamHarnessRun(crypto.randomUUID(), input.trim(), abort.signal)) {
        if (event.kind === "run_started") remember(event.run_id);
        setEvents((previous) => [...previous, event]);
        if (event.kind === "assistant_delta") setOutput((text) => text + event.text);
        if (event.kind === "assistant_message") setOutput(event.text);
        if (event.kind === "error") setError(event.message);
        if (event.kind === "done") setState(event.status);
      }
    } catch (e: unknown) {
      if (e instanceof ExistingRunError) { remember(e.runId); }
      else {
        setError(abort.signal.aborted
          ? "Stopped. A draft may already have been saved; check approval status before starting another run."
          : message(e));
        setState(abort.signal.aborted ? "cancelled" : "failed");
      }
    } finally {
      if (mounted.current) setBusy(false);
      controller.current = null;
    }
  }

  const ready = status?.enabled && (status.state === "configured" || status.state === "ready");
  const calls = new Map<string, { start: HarnessEvent; end?: HarnessEvent }>();
  for (const e of events) {
    if (e.kind === "tool_start") calls.set(e.call_id, { start: e });
    if (e.kind === "tool_result" || e.kind === "tool_error") {
      const call = calls.get(e.call_id);
      calls.set(e.call_id, { start: call?.start ?? e, end: e });
    }
  }
  const approvalEvent = events.find((e) => e.kind === "approval_pending");
  const review = insuranceReview(saved?.approval ?? approvalEvent, saved?.workflow_id);
  const unverifiedReview = (saved?.approval || approvalEvent) && !review;
  const usage = events.filter((e) => e.kind === "usage");

  return (
    <section className="harness-workspace" aria-label="Agent Harness">
      <header className="harness-header">
        <button onClick={onBack}>Back to Northwind</button>
        <div><h1>Agent Harness</h1><p>GitHub Copilot SDK orchestrates grounded insurance lookups and human-review-only drafts.</p></div>
        <span className="harness-badge">{busy ? "Running" : state ?? status?.state ?? "Loading"}</span>
      </header>
      <div className="harness-config">
        <span><strong>Harness</strong> Copilot SDK {status?.sdk_version ?? "unavailable"}</span>
        <span><strong>Runtime</strong> {status?.runtime_version ?? "starts on demand"} in FastAPI</span>
        <span><strong>Model</strong> {status?.model ?? "loading"}</span>
        <span><strong>Provider</strong> Azure Foundry / managed identity</span>
      </div>
      <p className="harness-notice">{status?.message ?? "Checking runtime configuration."} Synthetic demo only; approvals do not adjudicate claims, send messages or execute payments.</p>
      {error && <div className="harness-error" role="alert">{error}</div>}
      <div className="harness-columns">
        <div className="harness-main">
          <div className="harness-card">
            <h2>One request, one bounded run</h2>
            <div className="harness-scenarios">{SCENARIOS.map(([label, prompt]) =>
              <button key={label} disabled={busy} onClick={() => setInput(prompt)}>{label}</button>)}</div>
            <label htmlFor="harness-input">Claims Concierge request</label>
            <textarea id="harness-input" value={input} maxLength={8000} disabled={busy || !ready}
              onChange={(e) => setInput(e.target.value)} rows={5} />
            <div className="harness-actions">
              <button className="harness-primary" onClick={run} disabled={!ready || busy || !input.trim()}>Run agent</button>
              <button onClick={() => controller.current?.abort()} disabled={!busy}>Stop</button>
              <button disabled={busy} onClick={() => {
                currentRun.current = null; setRunId(null); setSaved(null);
                setEvents([]); setOutput(""); setState(null); setError(null);
              }}>New run</button>
              <span>{status?.max_tool_calls ?? 8} specialist calls maximum</span>
            </div>
          </div>
          <div className="harness-card" aria-live="polite">
            <h2>Result</h2>
            <p className="harness-output">{output || (busy ? "The SDK is working..." : "Run a scenario to see a live result. No simulated output is shown.")}</p>
            {runId && <code className="harness-run-id">{runId}</code>}
            {saved && <p className="harness-notice">Prompt SHA: {saved.prompt_sha.slice(0, 12)} · Saved execution: {saved.status}</p>}
          </div>
          {unverifiedReview && <div className="harness-error" role="alert">
            A review record could not be verified as a non-executed insurance draft. Check Approvals before retrying.
          </div>}
          {review && <div className="harness-card">
            <h2>Human draft review: {review.status}</h2>
            <p>Review kind: insurance_claim_action. Execution: not_executed. A human reviews this draft only; approval does not decide coverage, approve a claim, contact a customer or execute payment.</p>
            <button onClick={onApprovals}>Open Claims Concierge Approvals</button>
            <details><summary>Recorded draft</summary><pre>{JSON.stringify(review.payload, null, 2)}</pre></details>
          </div>}
          <div className="harness-card">
            <h2>Recent runs in this browser</h2>
            <p className="harness-notice">Saved for seven days. Opening a run never replays tools or resumes the SDK.</p>
            <div className="harness-history">{ids.map((id) =>
              <button key={id} disabled={busy} aria-pressed={id === runId} onClick={() => {
                currentRun.current = id; setRunId(id); setError(null);
                load(id).catch((e: unknown) => setError(message(e)));
              }}>{id.slice(8, 20)}</button>)}</div>
          </div>
        </div>
        <aside className="harness-card harness-timeline">
          <h2>Live tool activity</h2>
          <p className="harness-notice">SDK manager → canonical policy/claim tools and insurance Foundry specialists. These are actual calls, not private reasoning or a sample trace.</p>
          {!calls.size && <p>No specialist calls recorded.</p>}
          {[...calls].map(([id, { start, end }]) => {
            if (!("tool" in start)) return null;
            return <details key={id} open={!end} className={`harness-step ${end?.kind === "tool_error" ? "failed" : ""}`}>
              <summary>{start.tool} <span>{end?.kind === "tool_error" ? "failed" : end ? "completed" : state === "running" ? "running" : "interrupted"}</span></summary>
              <h3>Input</h3><pre>{start.text}</pre>
              {end && "text" in end && <><h3>Result</h3><pre>{end.text}</pre></>}
              {end && "duration_ms" in end && <p>{end.duration_ms} ms</p>}
            </details>;
          })}
          <h3>Measured usage</h3>
          {!usage.length && <p>Not reported. Cost is unavailable.</p>}
          {usage.map((e) => e.kind === "usage" && <p key={e.seq}>
            {e.source}: {e.input_tokens ?? "unknown"} input / {e.output_tokens ?? "unknown"} output tokens
          </p>)}
          {!!usage.length && <p className="harness-notice">Usage is separated by caller. Dollar cost is not estimated.</p>}
        </aside>
      </div>
    </section>
  );
}
