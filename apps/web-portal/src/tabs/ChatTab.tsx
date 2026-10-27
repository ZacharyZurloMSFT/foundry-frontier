/**
 * Claims Concierge — gated insurance chat + live orchestrator trace.
 *
 * Left pane: user + manager chat bubbles (unchanged behaviour).
 * Right pane: an orchestrator trace rail that grows as events stream in:
 *   - 💬 Manager thought   — text emitted between tool calls
 *   - 🔀 Handoff → agent   — a manager tool_call, keyed by call_id
 *   - 📄 Result            — the sub-agent's returned text (collapsible)
 *   - 🧠 Reasoning         — reasoning-model chain-of-thought (when the
 *                            manager deployment emits it)
 * A small live handoff graph sits at the top of the rail.
 */
import { useEffect, useRef, useState } from "react";
import TracePanel from "../components/TracePanel";
import HandoffGraph, { HandoffAgent } from "../components/HandoffGraph";
import { StreamEvent } from "../api";
import { streamInsuranceChat } from "../insuranceApi";
import { InsuranceRuntimeNotice, useInsurance } from "../InsuranceContext";
import { onDraftReviewed } from "../notifications";

interface Msg {
  role: "user" | "assistant" | "system";
  text: string;
  agent?: string;
}

type StepBase = { id: string };
type ThoughtStep    = StepBase & { type: "thought";    text: string };
type ReasoningStep  = StepBase & { type: "reasoning";  text: string };
type HandoffStep    = StepBase & {
  type: "handoff";
  call_id: string;
  tool: string;
  to_agent?: string;
  arguments?: any;
  result?: string;
  status: "running" | "done" | "error";
};
type Step = ThoughtStep | ReasoningStep | HandoffStep;

const SUB_AGENT_TOOLS: HandoffAgent[] = [
  "triage",
  "policy_claim_lookup",
  "policy_lookup",
  "claim_action_draft",
];

function isSubAgentTool(tool: string | undefined): tool is HandoffAgent {
  return !!tool && (SUB_AGENT_TOOLS as string[]).includes(tool);
}

export default function ChatTab() {
  const { fixtures, loading, error } = useInsurance();
  const ready = fixtures?.agent_runtime_ready === true && !loading && !error;
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [steps, setSteps] = useState<Step[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const sessionRef = useRef<string | null>(null);
  useEffect(() => { sessionRef.current = sessionId; }, [sessionId]);

  useEffect(() => {
    const off = onDraftReviewed((evt) => {
      if (!evt.chatMessage) return;
      if (!sessionRef.current || evt.sessionId !== sessionRef.current) return;
      setMsgs((m) => [
        ...m,
        {
          role: "system",
          text: evt.chatMessage!,
          agent: evt.decision === "approve" ? "human-approver" : "human-reviewer",
        },
      ]);
    });
    return off;
  }, []);

  // Derived: which sub-agent handoff is currently running (drives the graph).
  const activeHandoff = ((): HandoffAgent | null => {
    for (let i = steps.length - 1; i >= 0; i--) {
      const s = steps[i];
      if (s.type === "handoff" && s.status === "running" && isSubAgentTool(s.tool)) {
        return s.tool;
      }
    }
    return null;
  })();
  const visitedHandoffs: HandoffAgent[] = Array.from(new Set(
    steps.flatMap((s) =>
      s.type === "handoff" && isSubAgentTool(s.tool) ? [s.tool] : []
    )
  ));

  async function send() {
    if (!input.trim() || busy || !ready) return;
    const userMsg = input.trim();
    setInput("");
    setMsgs((m) => [...m, { role: "user", text: userMsg }]);
    setSteps([]);  // reset the trace for each new user turn
    setBusy(true);

    let currentAssistant: Msg = { role: "assistant", text: "" };
    setMsgs((m) => [...m, currentAssistant]);

    // Buffer that captures the manager's inter-tool prose into thought
    // cards. Flushed to a new thought step whenever a tool call arrives.
    let thoughtBuffer = "";
    const flushThought = () => {
      if (!thoughtBuffer.trim()) { thoughtBuffer = ""; return; }
      const text = thoughtBuffer;
      thoughtBuffer = "";
      setSteps((s) => [...s, { id: `t-${s.length}`, type: "thought", text }]);
    };
    let reasoningStepId: string | null = null;

    try {
      for await (const ev of streamInsuranceChat(sessionId, userMsg) as AsyncGenerator<StreamEvent>) {
        if (ev.kind === "session" && ev.session_id) {
          setSessionId(ev.session_id);
        } else if (ev.kind === "assistant_delta" || ev.kind === "assistant_message") {
          const chunk = ev.text ?? "";
          currentAssistant = {
            ...currentAssistant,
            text: (ev.kind === "assistant_message") ? (ev.text ?? "") : currentAssistant.text + chunk,
            agent: ev.agent,
          };
          setMsgs((m) => [...m.slice(0, -1), currentAssistant]);
          thoughtBuffer += chunk;
        } else if (ev.kind === "reasoning_delta") {
          const chunk = ev.text ?? "";
          if (reasoningStepId === null) {
            const id = `r-${Date.now()}`;
            reasoningStepId = id;
            setSteps((s) => [...s, { id, type: "reasoning", text: chunk }]);
          } else {
            const id = reasoningStepId;
            setSteps((s) => s.map((x) =>
              x.id === id && x.type === "reasoning" ? { ...x, text: x.text + chunk } : x
            ));
          }
        } else if (ev.kind === "tool_call") {
          flushThought();
          const call_id = ev.call_id ?? `c-${Date.now()}`;
          const step: HandoffStep = {
            id: `h-${call_id}`,
            type: "handoff",
            call_id,
            tool: ev.tool ?? "?",
            to_agent: ev.to_agent,
            arguments: ev.arguments,
            status: "running",
          };
          setSteps((s) => {
            // Replace an existing step with the same call_id if present
            // (Foundry may emit .added and .done for the same call).
            const idx = s.findIndex((x) => x.type === "handoff" && x.call_id === call_id);
            if (idx >= 0) {
              const next = [...s];
              const prev = next[idx] as HandoffStep;
              next[idx] = { ...prev, ...step, status: prev.status };
              return next;
            }
            return [...s, step];
          });
        } else if (ev.kind === "tool_args_done") {
          if (!ev.call_id) continue;
          setSteps((s) => s.map((x) =>
            x.type === "handoff" && x.call_id === ev.call_id
              ? { ...x, arguments: ev.arguments ?? x.arguments }
              : x
          ));
        } else if (ev.kind === "tool_result") {
          if (!ev.call_id) continue;
          setSteps((s) => s.map((x) =>
            x.type === "handoff" && x.call_id === ev.call_id
              ? { ...x, status: "done", result: ev.result }
              : x
          ));
        } else if (ev.kind === "error") {
          setSteps((s) => s.map((x) =>
            x.type === "handoff" && x.status === "running" ? { ...x, status: "error" } : x
          ));
          setMsgs((m) => [...m, { role: "assistant", text: `Error: ${ev.message ?? "unknown"}` }]);
        }
      }
      flushThought();
    } catch (e: any) {
      setMsgs((m) => [...m, { role: "assistant", text: `Error: ${e.message}` }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="multiagent-shell">
      <div className="chat-pane multiagent-chat">
        <InsuranceRuntimeNotice />
        <div className="chat-log">
          {msgs.map((m, i) => (
            <div key={i} className={`bubble-row ${m.role}`}>
              <div className={`bubble ${m.role}`}>
                {m.agent && <div className="agent-tag">{m.agent}</div>}
                {m.text ? (
                  <div className="bubble-text">{m.text}</div>
                ) : (
                  m.role === "assistant" && <TypingDots />
                )}
              </div>
            </div>
          ))}
          {msgs.length === 0 && (
            <div className="empty-hint">
              Try: "Summarize CLM-2001, identify outstanding evidence, and draft
              an adjuster handoff. Cite the policy and do not determine coverage."
            </div>
          )}
        </div>
        <div className="composer copilot">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={ready ? "Ask about a claim, evidence, policy references or a human-reviewed next step." : "Insurance chat unavailable until authorised rollout."}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            disabled={busy || !ready}
          />
          <button onClick={send} disabled={busy || !ready || !input.trim()}>
            {busy ? <TypingDots small /> : "Send"}
          </button>
        </div>
        <TracePanel sessionId={sessionId} />
      </div>

      <aside className="orch-trace">
        <div className="orch-trace-header">
          <div className="orch-trace-title">Orchestrator trace</div>
          <div className="orch-trace-sub">
            {busy ? "manager is working…" : steps.length ? "turn complete" : "idle"}
          </div>
        </div>
        <HandoffGraph active={activeHandoff} visited={visitedHandoffs} />
        <div className="orch-steps">
          {steps.length === 0 && (
            <div className="orch-empty">
              Send a request to see the manager's plan, hand-offs, and each
              sub-agent's response as they happen.
            </div>
          )}
          {steps.map((s) => (
            <StepCard key={s.id} step={s} />
          ))}
        </div>
      </aside>
    </div>
  );
}

function StepCard({ step }: { step: Step }) {
  const [open, setOpen] = useState(step.type === "handoff" && step.status === "running");
  if (step.type === "thought") {
    return (
      <div className="orch-step thought">
        <div className="orch-step-head">
          <span className="orch-step-icon">💬</span>
          <span className="orch-step-title">manager thought</span>
        </div>
        <div className="orch-step-body">{step.text}</div>
      </div>
    );
  }
  if (step.type === "reasoning") {
    return (
      <div className="orch-step reasoning">
        <div className="orch-step-head">
          <span className="orch-step-icon">🧠</span>
          <span className="orch-step-title">reasoning</span>
        </div>
        <div className="orch-step-body">{step.text}</div>
      </div>
    );
  }
  // handoff
  const argPreview = step.arguments
    ? (typeof step.arguments === "string"
        ? step.arguments
        : JSON.stringify(step.arguments))
    : "";
  const previewShort = argPreview.length > 220 ? argPreview.slice(0, 220) + "…" : argPreview;
  const toolClass = isSubAgentTool(step.tool) ? `handoff-${step.tool}` : "handoff-tool";
  return (
    <div className={`orch-step handoff ${toolClass} status-${step.status}`}>
      <button className="orch-step-head" onClick={() => setOpen((o) => !o)}>
        <span className="orch-step-icon">{step.status === "done" ? "✓" : step.status === "error" ? "✗" : "🔀"}</span>
        <span className="orch-step-title">
          manager → <b>{step.tool}</b>
          {step.to_agent && <span className="orch-target"> · {step.to_agent}</span>}
        </span>
        <span className={`orch-status status-${step.status}`}>{step.status}</span>
        <span className="orch-toggle">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="orch-step-body">
          {previewShort && (
            <div className="orch-block">
              <div className="orch-block-label">prompt</div>
              <pre className="orch-block-content">{previewShort}</pre>
            </div>
          )}
          {step.result && (
            <div className="orch-block">
              <div className="orch-block-label">response</div>
              <pre className="orch-block-content">{step.result}</pre>
            </div>
          )}
          {step.status === "running" && !step.result && <TypingDots small />}
        </div>
      )}
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
