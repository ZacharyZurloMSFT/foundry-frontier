import { AssistantEvent, Citation, HelpSource, Provenance, ToolStep } from "./assistantModels";

export interface Exchange {
  id: number; question: string; status: "pending" | "answered" | "failed";
  answer: string | null; citations: Citation[]; sources: HelpSource[]; steps: ToolStep[]; provenance: Provenance | null;
  error: { code: string; message: string } | null; notice: string | null;
}
export interface Conversation { sessionId: string | null; exchanges: Exchange[]; busy: boolean; next: number }
export type Action =
  | { type: "ask"; question: string }
  | { type: "event"; event: AssistantEvent }
  | { type: "failed"; code: string; message: string }
  | { type: "ended" }
  | { type: "reset" };

export const emptyConversation: Conversation = { sessionId: null, exchanges: [], busy: false, next: 1 };
// Typed server codes that mean the saved conversation can no longer be continued.
const SESSION_LOST = new Set(["session_not_found", "session_full", "session_mismatch"]);

function last(state: Conversation, change: (exchange: Exchange) => Exchange): Conversation {
  const index = state.exchanges.length - 1;
  if (index < 0 || state.exchanges[index].status !== "pending") return state;
  const exchanges = state.exchanges.slice();
  exchanges[index] = change(exchanges[index]);
  return { ...state, exchanges };
}

export function reduce(state: Conversation, action: Action): Conversation {
  switch (action.type) {
    case "ask":
      return {
        ...state, busy: true, next: state.next + 1,
        exchanges: [...state.exchanges, { id: state.next, question: action.question, status: "pending", answer: null,
          citations: [], sources: [], steps: [], provenance: null, error: null, notice: null }],
      };
    case "event": {
      const event = action.event;
      switch (event.kind) {
        case "session": return { ...state, sessionId: event.session_id };
        case "tool": return last(state, e => ({ ...e, steps: [...e.steps, { name: event.name, status: event.status, label: event.label }] }));
        case "assistant_delta": return last(state, e => ({ ...e, answer: event.text }));
        case "citations": return last(state, e => ({ ...e, citations: event.citations }));
        case "sources": return last(state, e => ({ ...e, sources: event.sources }));
        case "error": {
          const lost = SESSION_LOST.has(event.code);
          return { ...last(state, e => ({ ...e, status: "failed", answer: null, citations: [], sources: [], error: { code: event.code, message: event.message } })),
            sessionId: lost ? null : state.sessionId };
        }
        case "done": {
          const next = last(state, e => e.error || e.answer === null
            ? { ...e, status: "failed", answer: null, citations: [], sources: [], error: e.error ?? { code: "no_answer", message: "The assistant did not return an answer. Nothing was confirmed." } }
            : { ...e, status: "answered", provenance: event.provenance, notice: event.notice });
          return { ...next, busy: false };
        }
      }
      return state;
    }
    case "failed": {
      const next = last(state, e => ({ ...e, status: "failed", answer: null, citations: [], sources: [], error: { code: action.code, message: action.message } }));
      return { ...next, busy: false, sessionId: SESSION_LOST.has(action.code) ? null : state.sessionId };
    }
    case "ended": {
      const next = last(state, e => ({ ...e, status: "failed", answer: null, citations: [], sources: [],
        error: { code: "interrupted", message: "The assistant response was interrupted. Nothing was confirmed; you can ask again." } }));
      return { ...next, busy: false };
    }
    case "reset":
      return emptyConversation;
  }
}
