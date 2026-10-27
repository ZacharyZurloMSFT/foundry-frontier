/**
 * Tiny in-browser event bus so the Approvals tab can notify other tabs when a
 * claim-action draft gets approved or rejected — used to inject a confirmation
 * message into the Claims Concierge chat and to force the Reviewed drafts tab
 * to refresh without waiting for its next poll.
 */
import type { InsuranceActionDraft } from "./insuranceApi";

export type DraftReviewed = {
  hitlId: string;
  workflowId: string;
  sessionId: string | null;
  decision: "approve" | "reject";
  chatMessage: string | null;
  payload: Partial<InsuranceActionDraft>;
  resolvedAt: string;
};

const bus = new EventTarget();
const EVENT = "draft-reviewed";

export function emitDraftReviewed(detail: DraftReviewed) {
  bus.dispatchEvent(new CustomEvent(EVENT, { detail }));
}

export function onDraftReviewed(handler: (e: DraftReviewed) => void): () => void {
  const wrapped = (e: Event) => handler((e as CustomEvent<DraftReviewed>).detail);
  bus.addEventListener(EVENT, wrapped);
  return () => bus.removeEventListener(EVENT, wrapped);
}
