import { KeyboardEvent, useEffect, useId, useReducer, useRef, useState } from "react";
import { BusinessError, errorMessage } from "../api";
import { ErrorNotice } from "../shared";
import { useAssistantAccess } from "./AssistantContext";
import { AssistantApi } from "./assistantApi";
import { MAX_MESSAGE, SurfaceStatus, ToolStatus } from "./assistantModels";
import { Exchange, emptyConversation, reduce } from "./assistantState";
import { revealWording } from "./wording";

export interface PanelContext { claim_id?: string; policy_id?: string }
const STEP_LABEL: Record<ToolStatus, string> = { ok: "done", not_found: "not found", invalid: "rejected", error: "failed" };

export function contextSuggestions(context: PanelContext | undefined, status: SurfaceStatus): string[] {
  if (context?.claim_id) return ["What happens next on this claim?", "What evidence is still needed?", "Why does my claim show a review flag?"];
  if (context?.policy_id) return ["What does my collision coverage say?", "What are my deductibles?", "Which endorsements apply to this policy?"];
  return status.suggestions;
}

/** Wording that differs by what a surface may read. Availability and scope are always decided by the server. */
export interface Variant {
  articles: boolean; heading: (context?: PanelContext) => string; scope: (context?: PanelContext) => string;
  checking: string; placeholder: string; reads: string;
}
const RECORDS: Variant = {
  articles: false,
  heading: context => context?.claim_id ? "Ask about this claim" : context?.policy_id ? "Ask about this policy" : "Ask about your policies and claims",
  scope: context => `Only your own records${context?.claim_id ? ` · ${context.claim_id}` : context?.policy_id ? ` · ${context.policy_id}` : ""}`,
  checking: "Checking your own records and the policy wording…",
  placeholder: "Ask about your coverage, a claim's status or what to do next. Do not enter real personal data.",
  reads: "Only your own records were available to the assistant. Its answer is checked against what it read before it is shown.",
};
const VARIANTS: Record<string, Variant> = {
  help_center: {
    articles: true, heading: () => "Ask the Help Center", scope: () => "Help articles only · no personal records",
    checking: "Searching the Help Center articles…",
    placeholder: "Ask how reporting a loss, evidence, deductibles, rentals or billing work. The Help Center cannot see your policy or claims, so do not enter personal details.",
    reads: "This assistant reads only published help articles, never your policy or claims. Anything it quotes is a verbatim excerpt of those articles, and when no article answers your question it says so instead of guessing.",
  },
};
export const variantFor = (surface: string): Variant => VARIANTS[surface] ?? RECORDS;

export function Turn({ exchange, latest, busy, label, variant, onAgain, onReveal }: {
  exchange: Exchange; latest: boolean; busy: boolean; label: string; variant: Variant; onAgain: () => void; onReveal: (policyRef: string) => void;
}) {
  const provenance = exchange.provenance;
  return <article className="business-assistant-turn">
    <p className="business-assistant-question"><span className="business-muted">You asked</span><br />{exchange.question}</p>
    {exchange.steps.length > 0 && <ul className="business-assistant-steps" aria-label="Records checked for this answer">
      {exchange.steps.map((step, index) => <li key={index}>{step.label} <span className="business-badge">{STEP_LABEL[step.status]}</span></li>)}
    </ul>}
    {exchange.status === "pending" && <p role="status" className="business-muted">{variant.checking}</p>}
    {exchange.status === "answered" && <div className="business-assistant-answer">
      <p className="business-muted">{label}</p>
      <p className="business-prewrap">{exchange.answer}</p>
      {provenance?.human_review && <p className="business-callout">A person needs to review this. The assistant cannot decide coverage or claims.</p>}
      {exchange.sources.length > 0 && <div className="business-help-sources">
        <p className="business-muted">Help articles quoted</p>
        <ul>{exchange.sources.map(source => <li key={source.id}><strong>{source.title}</strong> · version {source.version} · effective {source.effective_from} · <code>{source.id}</code></li>)}</ul>
      </div>}
      {exchange.citations.length > 0 && <details className="business-assistant-sources">
        <summary>Policy wording cited ({exchange.citations.length})</summary>
        {exchange.citations.map(citation => <section className="business-source" key={citation.policy_ref}>
          <h5>{citation.title} · version {citation.version}</h5><code>{citation.policy_ref}</code>
          <p className="business-muted">{citation.state} · effective {citation.effective_from} – {citation.effective_to}</p>
          <p className="business-prewrap">{citation.text}</p>
          <button type="button" onClick={() => onReveal(citation.policy_ref)}>Show in policy wording</button>
        </section>)}
      </details>}
      {provenance && <details className="business-assistant-sources">
        <summary>How this answer was produced</summary>
        <dl className="business-facts">
          <div><dt>Model deployment</dt><dd>{provenance.model === "none" ? "Not used (no matching article)" : <code>{provenance.model}</code>}</dd></div>
          <div><dt>Prompt version</dt><dd><code>{provenance.prompt_sha}</code></dd></div>
          {variant.articles
            ? <div><dt>Personal records read</dt><dd>None</dd></div>
            : <>
              <div><dt>Records checked</dt><dd>{provenance.tool_calls}</dd></div>
              <div><dt>Human review recommended</dt><dd>{provenance.human_review ? "Yes" : "Not flagged"}</dd></div>
            </>}
        </dl>
        <p className="business-muted">{variant.reads}</p>
      </details>}
    </div>}
    {exchange.status === "failed" && exchange.error && <div className="business-error" role="alert">
      <strong>No answer was shown</strong><p>{exchange.error.message}</p>
      <p className="business-muted">Reference: <code>{exchange.error.code}</code></p>
      {latest && !busy && <button type="button" onClick={onAgain}>Ask this again</button>}
    </div>}
  </article>;
}

function Conversation({ api, status, notice, context, onError }: {
  api: AssistantApi; status: SurfaceStatus; notice: string; context?: PanelContext; onError?: (error: unknown) => void;
}) {
  const [state, dispatch] = useReducer(reduce, emptyConversation);
  const [draft, setDraft] = useState(""), [problem, setProblem] = useState<unknown>(null), [hint, setHint] = useState("");
  const controller = useRef<AbortController | null>(null), log = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => { log.current?.scrollTo?.({ top: log.current.scrollHeight }); }, [state.exchanges]);

  async function ask(question: string) {
    const message = question.trim();
    if (!message || state.busy) return;
    setProblem(null); setHint(""); setDraft(""); dispatch({ type: "ask", question: message });
    const abort = new AbortController();
    controller.current = abort;
    const scoped = context && (context.claim_id || context.policy_id) ? { context } : {};
    try {
      for await (const event of api.chat(status.surface, { message, ...(state.sessionId ? { session_id: state.sessionId } : {}), ...scoped }, abort.signal)) {
        dispatch({ type: "event", event });
      }
      dispatch({ type: "ended" });
    } catch (error) {
      if (abort.signal.aborted) dispatch({ type: "failed", code: "stopped", message: "Stopped. Nothing was confirmed." });
      else if (error instanceof BusinessError) { onError?.(error); setProblem(error); dispatch({ type: "failed", code: error.code, message: error.message }); }
      else dispatch({ type: "failed", code: "invalid_response", message: errorMessage(error) });
    } finally { if (controller.current === abort) controller.current = null; }
  }
  function submitWithKeyboard(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); void ask(draft); }
  }
  const suggestions = contextSuggestions(context, status);
  const variant = variantFor(status.surface);
  const latest = state.exchanges[state.exchanges.length - 1];
  return <section className="business-panel business-assistant" aria-labelledby={`${id}-title`}>
    <div className="business-heading">
      <div><p className="business-eyebrow">{status.label} · AI assistant</p><h3 id={`${id}-title`}>{variant.heading(context)}</h3></div>
      <span className="business-badge">{variant.scope(context)}</span>
    </div>
    <p className="business-muted">{notice}</p>
    <div className="business-assistant-log" role="log" aria-live="polite" aria-relevant="additions" aria-label={`${status.label} conversation`} aria-busy={state.busy} ref={log}>
      {state.exchanges.map(exchange => <Turn key={exchange.id} exchange={exchange} latest={exchange === latest} busy={state.busy} label={status.label} variant={variant}
        onAgain={() => void ask(exchange.question)} onReveal={policyRef => setHint(revealWording(policyRef) ? "" : "Open the policy to read this wording in full.")} />)}
    </div>
    {hint && <p role="status" className="business-muted">{hint}</p>}
    {problem !== null && <ErrorNotice error={problem} />}
    {state.exchanges.length === 0 && suggestions.length > 0 && <div className="business-actions business-assistant-chips" aria-label="Suggested questions">
      {suggestions.map(suggestion => <button type="button" key={suggestion} disabled={state.busy} onClick={() => void ask(suggestion)}>{suggestion}</button>)}
    </div>}
    <form onSubmit={event => { event.preventDefault(); void ask(draft); }}>
      <label htmlFor={`${id}-question`}>Your question</label>
      <textarea id={`${id}-question`} rows={3} maxLength={MAX_MESSAGE} value={draft} disabled={state.busy} onKeyDown={submitWithKeyboard}
        onChange={event => setDraft(event.target.value)} aria-describedby={`${id}-note`} placeholder={variant.placeholder} />
      <div className="business-actions">
        <button type="submit" className="business-primary" disabled={state.busy || !draft.trim()}>{state.busy ? "Asking…" : "Ask"}</button>
        {state.busy && <button type="button" onClick={() => controller.current?.abort()}>Stop</button>}
        {!state.busy && state.exchanges.length > 0 && <button type="button" onClick={() => { dispatch({ type: "reset" }); setProblem(null); setHint(""); }}>Start a new conversation</button>}
      </div>
    </form>
    <p className="business-muted" id={`${id}-note`}>Synthetic demonstration. This is not a coverage decision, legal advice or a promise to pay; a person reviews every claim. Press Ctrl+Enter to ask.</p>
  </section>;
}

/** An assistant embedded where the work happens. It renders only when the server lists the surface for this account. */
export default function AssistantPanel({ surface, context, onError }: { surface: string; context?: PanelContext; onError?: (error: unknown) => void }) {
  const access = useAssistantAccess();
  const status = access.surfaces[surface];
  if (!access.api || !status) return null;
  if (!status.available) return <aside className="business-panel business-assistant-unavailable" role="status">
    <p className="business-eyebrow">{status.label}</p><p>The {status.label} is not available right now. Everything else on this page still works.</p></aside>;
  // A general-help surface reads no records, so it is never given (or sent) a claim or policy hint.
  const scoped = variantFor(surface).articles ? undefined : context;
  return <Conversation key={`${surface}:${scoped?.claim_id ?? ""}:${scoped?.policy_id ?? ""}`} api={access.api} status={status}
    notice={access.notice} context={scoped} onError={onError} />;
}
