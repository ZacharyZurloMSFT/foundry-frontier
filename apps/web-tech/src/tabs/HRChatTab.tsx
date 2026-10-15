import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";

interface SupportCitation { id: string; title: string; source_uri: string; version: string }
interface SupportEvent {
  kind: string; session_id?: string; text?: string; agent?: string; message?: string;
  citations?: SupportCitation[];
}

export async function* streamSupportChat(slug: string, sessionId: string | null, message: string,
  signal: AbortSignal): AsyncGenerator<SupportEvent> {
  const response = await apiFetch(`/adjuster-support/${encodeURIComponent(slug)}/chat`, {
    method: "POST", signal,
    headers: { "Content-Type": "application/json", "X-Department": "employee-help-desk" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!response.ok || !response.body) throw new Error(`Grounded support unavailable (${response.status}); no fallback answer was generated.`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", bytes = 0, terminal = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      bytes += value?.length ?? 0;
      if (bytes > 1024 * 1024) throw new Error("Support response exceeded its limit.");
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      if (done && buffer.trim()) { lines.push(buffer); buffer = ""; }
      for (const line of lines) {
        if (!line.trim()) continue;
        if (terminal) throw new Error("Unexpected support data after completion.");
        const event = JSON.parse(line) as SupportEvent;
        if (!event || typeof event !== "object" || !["session", "assistant_delta", "citations", "error", "done"].includes(event.kind)) {
          throw new Error("Invalid support event.");
        }
        if (event.kind === "citations" && (!Array.isArray(event.citations) || event.citations.some((c) =>
          typeof c.id !== "string" || typeof c.title !== "string" || typeof c.version !== "string" ||
          typeof c.source_uri !== "string" || !c.source_uri.startsWith("urn:northwind:synthetic:employee-support:")))) {
          throw new Error("Support returned invalid source citations.");
        }
        if (event.kind === "done") terminal = true;
        yield event;
      }
      if (done) break;
    }
    if (!terminal) throw new Error("Support stream interrupted; no complete answer was confirmed.");
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

interface Msg {
  role: "user" | "assistant";
  text: string;
  agent?: string;
  toolCalls?: { tool: string; args?: any; result?: string }[];
  error?: boolean;
  citations?: SupportCitation[];
}

interface ModelOption {
  slug: string;
  agentName: string;
  label: string;      // dropdown label
  model: string;      // deployment identifier shown in the routing hint
}

const MODEL_OPTIONS: ModelOption[] = [
  {
    slug: "hr-gpt-5-4",
    agentName: "northwind-insurance-adjuster-support",
    label: "gpt-5.4 (flagship)",
    model: "gpt-5.4",
  },
  {
    slug: "hr-gpt-5-4-mini",
    agentName: "northwind-insurance-adjuster-support",
    label: "gpt-5.4-mini (fast · cheap)",
    model: "gpt-5.4-mini",
  },
  {
    slug: "hr-kimi",
    agentName: "northwind-insurance-adjuster-support",
    label: "Kimi-K2.6",
    model: "Kimi-K2.6",
  },
];

const SUGGESTIONS = [
  "How many PTO days do I get in my second year?",
  "How do I set up VPN on a new laptop?",
  "What's the expense policy for client dinners?",
  "How do I request parental leave?",
  "What evidence should I request for a fictional collision claim?",
  "When should an adjuster escalate a jurisdiction or privacy concern?",
];

export default function HRChatTab() {
  // The support backend partitions persisted history by model-specific session.
  const [sessionsBySlug, setSessionsBySlug] = useState<Record<string, string>>({});
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [messagesBySlug, setMessagesBySlug] = useState<Record<string, Msg[]>>({});
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [slug, setSlug] = useState<string>(MODEL_OPTIONS[0].slug);
  const logRef = useRef<HTMLDivElement | null>(null);
  const controller = useRef<AbortController | null>(null);
  const [availability, setAvailability] = useState("Checking isolated support retrieval…");

  const current = MODEL_OPTIONS.find((m) => m.slug === slug) ?? MODEL_OPTIONS[0];

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy]);
  useEffect(() => {
    const abort = new AbortController();
    apiFetch("/adjuster-support/status", { signal: abort.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Support configuration unavailable (${response.status}).`);
        const status = await response.json();
        setAvailability(status.message || "Read-only retrieval; no claim decisions or execution.");
      }).catch((error: unknown) => {
        if (!abort.signal.aborted) setAvailability(error instanceof Error ? error.message : String(error));
      });
    return () => { abort.abort(); controller.current?.abort(); };
  }, []);

  async function send(text?: string) {
    const userMsg = (text ?? input).trim();
    if (!userMsg || busy) return;
    setInput("");
    setMsgs((m) => [
      ...m,
      { role: "user", text: userMsg },
      { role: "assistant", text: "", agent: current.agentName, toolCalls: [] },
    ]);
    setBusy(true);
    const abort = new AbortController();
    controller.current = abort;

    const sessionId = sessionsBySlug[slug] ?? null;

    try {
      for await (const ev of streamSupportChat(slug, sessionId, userMsg, abort.signal)) {
        if (ev.kind === "session" && ev.session_id) {
          setSessionsBySlug((s) => ({ ...s, [slug]: ev.session_id! }));
        } else if (ev.kind === "assistant_delta" && ev.text) {
          setMsgs((m) => {
            const next = [...m];
            const last = { ...next[next.length - 1] };
            last.text = (last.text || "") + ev.text;
            if (ev.agent) last.agent = ev.agent;
            next[next.length - 1] = last;
            return next;
          });
        } else if (ev.kind === "citations") {
          setMsgs((m) => {
            const next = [...m];
            next[next.length - 1] = { ...next[next.length - 1], citations: ev.citations };
            return next;
          });
        } else if (ev.kind === "error") {
          setMsgs((m) => {
            const next = [...m];
            next[next.length - 1] = {
              role: "assistant",
              text: `Error: ${ev.message ?? "unknown"}`,
              agent: current.agentName,
              error: true,
            };
            return next;
          });
        }
      }
    } catch (e: unknown) {
      setMsgs((m) => {
        const next = [...m];
        next[next.length - 1] = {
          role: "assistant",
          text: abort.signal.aborted ? "Stopped. No fallback answer was generated."
            : `Error: ${e instanceof Error ? e.message : String(e)}`,
          agent: current.agentName,
          error: true,
        };
        return next;
      });
    } finally {
      setBusy(false);
      controller.current = null;
    }
  }

  const isEmpty = msgs.length === 0;

  return (
    <div className={`hrchat ${isEmpty ? "empty" : ""}`}>
      <div className="hrchat-topbar">
        <div className="hrchat-topbar-left">
          <span className="hrchat-title">Employee &amp; Adjuster Support</span>
          <span className="hrchat-sub">
            read-only HR/IT and adjuster procedures · <code>nw-insurance-adjuster-support</code>
          </span>
        </div>
        <label className="hrchat-modelpick">
          <span>Model</span>
          <select
            value={slug}
            onChange={(e) => {
              setMessagesBySlug((previous) => ({ ...previous, [slug]: msgs }));
              setMsgs(messagesBySlug[e.target.value] ?? []);
              setSlug(e.target.value);
            }}
            disabled={busy}
          >
            {MODEL_OPTIONS.map((m) => (
              <option key={m.slug} value={m.slug}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="hrchat-mode-hint" role="status">{availability} Fictional sources only; never enter real claim data.</p>
      {busy && <button onClick={() => controller.current?.abort()}>Stop support response</button>}

      {isEmpty ? (
        <div className="hrchat-hero">
          <div className="hrchat-greeting">Hi, what can I help you with?</div>
          <Composer
            input={input}
            setInput={setInput}
            onSend={() => send()}
            busy={busy}
            placeholder="Message Ask Northwind"
            large
          />
          <div className="hrchat-suggestions">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                className="hrchat-pill"
                onClick={() => send(s)}
                disabled={busy}
                title={s}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="hrchat-mode-hint">
            Search + <b>{current.model}</b> select exact source excerpts with title, version and source URI.
            No legacy HR agent, model-memory or web fallback is used.
            Claim coverage, denials and payments always remain outside this support desk.
          </div>
        </div>
      ) : (
        <>
          <div className="chat-log hrchat-log" ref={logRef}>
            {msgs.map((m, i) => (
              <div key={i} className={`bubble-row ${m.role}`}>
                <div className={`bubble ${m.role} ${m.error ? "error" : ""}`}>
                  {m.role === "assistant" && m.agent && <div className="agent-tag">{m.agent}</div>}
                  {m.text ? (
                    <div className="bubble-text">{m.text}</div>
                  ) : (
                    m.role === "assistant" && <TypingDots />
                  )}
                  {m.toolCalls && m.toolCalls.length > 0 && (
                    <div className="tool-call">
                      {m.toolCalls.map((tc, j) => (
                        <div key={j}>→ {tc.tool}({JSON.stringify(tc.args ?? {})}) {tc.result ? " ✓" : ""}</div>
                      ))}
                    </div>
                  )}
                  {!!m.citations?.length && <details><summary>Grounded sources</summary>
                    {m.citations.map((source) => <p key={source.id}>
                      {source.title} · version {source.version}<br /><code>{source.source_uri}</code>
                    </p>)}
                  </details>}
                </div>
              </div>
            ))}
          </div>
          <Composer
            input={input}
            setInput={setInput}
            onSend={() => send()}
            busy={busy}
            placeholder="Message Ask Northwind"
          />
        </>
      )}
    </div>
  );
}

function Composer({
  input,
  setInput,
  onSend,
  busy,
  placeholder,
  large,
}: {
  input: string;
  setInput: (v: string) => void;
  onSend: () => void;
  busy: boolean;
  placeholder: string;
  large?: boolean;
}) {
  return (
    <div className={`composer copilot hrchat-composer ${large ? "large" : ""}`}>
      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder={placeholder}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSend();
          }
        }}
        disabled={busy}
      />
      <button onClick={onSend} disabled={busy || !input.trim()}>
        {busy ? <TypingDots small /> : "Send"}
      </button>
    </div>
  );
}

function TypingDots({ small }: { small?: boolean }) {
  return (
    <span className={`typing-dots ${small ? "small" : ""}`} aria-label="Assistant is typing">
      <span />
      <span />
      <span />
    </span>
  );
}
