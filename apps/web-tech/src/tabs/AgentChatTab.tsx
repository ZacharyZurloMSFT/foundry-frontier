import { useEffect, useRef, useState } from "react";
import { StreamEvent, streamAgentChat } from "../api";

interface Msg {
  role: "user" | "assistant";
  text: string;
  agent?: string;
  toolCalls?: { tool: string; args?: any; result?: string }[];
  error?: boolean;
}

export interface AgentChatConfig {
  slug: string;
  agentName: string;
  title: string;
  subtitle: string;
  placeholder: string;
  emptyHint: string;
}

/**
 * Chat directly with one Foundry prompt agent. Owns its own session id,
 * message log, and streaming state, so a parent that keeps this component
 * mounted (via CSS `display:none` toggling) gets per-tab history
 * preservation for free.
 */
export default function AgentChatTab({ slug, agentName, title, subtitle, placeholder, emptyHint }: AgentChatConfig) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy]);

  async function send() {
    if (!input.trim() || busy) return;
    const userMsg = input.trim();
    setInput("");
    setMsgs((m) => [...m, { role: "user", text: userMsg }, { role: "assistant", text: "", agent: agentName, toolCalls: [] }]);
    setBusy(true);

    try {
      for await (const ev of streamAgentChat(slug, sessionId, userMsg) as AsyncGenerator<StreamEvent>) {
        if (ev.kind === "session" && ev.session_id) {
          setSessionId(ev.session_id);
        } else if (ev.kind === "assistant_delta" && ev.text) {
          setMsgs((m) => {
            const next = [...m];
            const last = { ...next[next.length - 1] };
            last.text = (last.text || "") + ev.text;
            if (ev.agent) last.agent = ev.agent;
            next[next.length - 1] = last;
            return next;
          });
        } else if (ev.kind === "tool_call") {
          setMsgs((m) => {
            const next = [...m];
            const last = { ...next[next.length - 1] };
            last.toolCalls = [...(last.toolCalls || []), { tool: ev.tool ?? "?", args: ev.arguments }];
            next[next.length - 1] = last;
            return next;
          });
        } else if (ev.kind === "tool_result") {
          setMsgs((m) => {
            const next = [...m];
            const last = { ...next[next.length - 1] };
            const calls = [...(last.toolCalls || [])];
            if (calls.length) calls[calls.length - 1] = { ...calls[calls.length - 1], result: ev.result };
            last.toolCalls = calls;
            next[next.length - 1] = last;
            return next;
          });
        } else if (ev.kind === "error") {
          setMsgs((m) => {
            const next = [...m];
            next[next.length - 1] = { role: "assistant", text: `Error: ${ev.message ?? "unknown"}`, agent: agentName, error: true };
            return next;
          });
        }
      }
    } catch (e: any) {
      setMsgs((m) => {
        const next = [...m];
        next[next.length - 1] = { role: "assistant", text: `Error: ${e.message}`, agent: agentName, error: true };
        return next;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat-pane">
      <div className="chat-header">
        <strong>{title}</strong>
        <span className="chat-header-sub">
          — {subtitle} <code>{agentName}</code>
        </span>
      </div>
      <div className="chat-log" ref={logRef}>
        {msgs.length === 0 && <div className="empty-hint">{emptyHint}</div>}
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
            </div>
          </div>
        ))}
      </div>
      <div className="composer copilot">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={placeholder}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          disabled={busy}
        />
        <button onClick={send} disabled={busy || !input.trim()}>
          {busy ? <TypingDots small /> : "Send"}
        </button>
      </div>
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
