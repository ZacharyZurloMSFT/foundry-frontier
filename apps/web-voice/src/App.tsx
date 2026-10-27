import { useEffect, useRef, useState } from "react";
import { getInsuranceRecords, getVoiceReadiness, HandoffPayload, InsuranceRecords, streamHandoff } from "./api";
import HandoffReview from "./HandoffReview";
import { useVoiceLive } from "./useVoiceLive";

export default function App() {
  const call = useVoiceLive();
  const [records, setRecords] = useState<InsuranceRecords | null>(null);
  const [voiceReady, setVoiceReady] = useState(false);
  const [readiness, setReadiness] = useState("Checking insurance and Voice Live readiness…");
  const [checkVersion, setCheckVersion] = useState(0);
  const [summary, setSummary] = useState("");
  const [intakeVersion, setIntakeVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [feed, setFeed] = useState<string[]>([]);
  const handoffAbort = useRef<AbortController>();
  const submitted = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    setVoiceReady(false);
    getInsuranceRecords(controller.signal).then((data) => {
      if (controller.signal.aborted) return;
      setRecords(data);
      if (!data.agent_runtime_ready) throw new Error("Insurance agent runtime is not ready. No handoff or voice session can start.");
      return getVoiceReadiness(controller.signal);
    }).then(() => {
      if (!controller.signal.aborted) { setVoiceReady(true); setReadiness("Azure Voice Live transport is enabled. Synthetic records only."); }
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setReadiness(error instanceof Error ? error.message : String(error));
    });
    return () => controller.abort();
  }, [checkVersion]);
  useEffect(() => () => handoffAbort.current?.abort(), []);

  async function handoff(payload: HandoffPayload) {
    if (submitted.current || call.phase !== "idle" || !records?.agent_runtime_ready) return;
    submitted.current = true; setBusy(true); setFeed(["Submitting confirmed intake to Claims Concierge…"]);
    const controller = new AbortController(); handoffAbort.current = controller;
    const deadline = setTimeout(() => controller.abort(), 180000);
    let sawSession = false, sawDone = false;
    try {
      if (!(await getInsuranceRecords(controller.signal)).agent_runtime_ready) throw new Error("Insurance runtime is no longer ready.");
      for await (const event of streamHandoff(payload, controller.signal)) {
        if (event.kind === "error") throw new Error(event.message || "Claims Concierge reported an error.");
        if (event.kind === "session") { sawSession = true; setFeed((f) => [...f, `Claims session: ${event.session_id}`]); }
        if (event.kind === "assistant_delta" && event.text) setFeed((f) => [...f.slice(0, -1), (f.at(-1)?.startsWith("Claims session:") || f.length === 1) ? `${f.at(-1)}\n${event.text}` : `${f.at(-1)}${event.text}`]);
        if (event.kind === "agent") setFeed((f) => [...f, `Reviewing with ${event.agent}`]);
        if (event.kind === "done") sawDone = true;
      }
      if (!sawSession || !sawDone) throw new Error("Handoff stream ended without completion. Check the Claims session before retrying.");
      setFeed((f) => [...f, "Claims review completed. Review any proposed draft in Approvals; nothing was paid or sent externally."]);
    } catch (error) {
      setFeed((f) => [...f, `${error instanceof Error ? error.message : String(error)} No automatic retry was made; check any existing Claims session before submitting again.`]);
    } finally {
      clearTimeout(deadline); setBusy(false);
      // Keep this submission locked until the operator explicitly starts another intake.
    }
  }
  return (
    <main className="container">
      <h1>Northwind Insurance — Policyholder Contact Center</h1>
      <p className="tagline">Fictional auto-claims intake, first notice of loss and claim-status assistance.
        This assistant cannot approve or deny coverage, determine liability, settle or pay claims.
        For immediate danger, contact local emergency services.</p>
      <p role="status">{readiness}</p>
      <button disabled={call.phase !== "idle" || busy} onClick={() => setCheckVersion((v) => v + 1)}>Check readiness again</button>
      <section className="card">
        <h2>Live audio — Azure Voice Live</h2>
        {call.phase === "idle" ?
          <button className="big-button" disabled={!voiceReady || busy || submitted.current} onClick={() => { setSummary(""); setIntakeVersion((v) => v + 1); void call.start(); }}>Start a call</button> :
          <button className="big-button stop" onClick={call.stop}>End call</button>}
        <p role="status">{call.status}</p>
        {call.transcript.length > 0 && <>
          <div className="transcript" aria-label="Completed call transcript">{call.transcript.map((line, i) =>
            <div key={i}><strong>{line.who === "you" ? "Caller" : "Northwind"}:</strong> {line.text}</div>)}</div>
          <button disabled={busy || submitted.current} onClick={() => {
            call.stop();
            setSummary(`Caller-reported intake (speech recognition may contain errors; review before handoff):\n${call.transcript.filter((line) => line.who === "you").map((line) => line.text).join("\n")}`.slice(0, 11000));
          }}>End call and copy completed caller transcript into review</button>
        </>}
        <p>Audio is streamed transiently, not recorded. The transcript stays in this page only.
          Only your confirmed handoff summary enters the existing Claims session store. Starting a new call clears the transcript.</p>
      </section>
      <HandoffReview key={intakeVersion} records={records} ready={records?.agent_runtime_ready === true && call.phase === "idle" && !submitted.current}
        busy={busy} summarySuggestion={summary} onConfirm={(payload) => { void handoff(payload); }} />
      {feed.length > 0 && <section aria-label="Claims Concierge handoff stream" aria-live="polite">
        <h2>Claims Concierge handoff</h2>{feed.map((line, i) => <p key={i} style={{ whiteSpace: "pre-wrap" }}>{line}</p>)}
        {!busy && <button onClick={() => { submitted.current = false; call.reset(); setIntakeVersion((v) => v + 1); setSummary(""); setFeed([]); setCheckVersion((v) => v + 1); }}>Start another intake (check prior session first)</button>}
      </section>}
      <p className="footnote">Voice Live is separate from the Claims Concierge manager and Copilot SDK harness.
        The configured native Voice Live catalog model is not inferred from a text or Azure OpenAI deployment.</p>
    </main>
  );
}
