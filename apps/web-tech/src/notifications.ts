/**
 * Tiny in-browser event bus so the HITL tab can notify other tabs when a
 * resolution gets approved or rejected — used to inject a confirmation
 * message into the Multi-Agent chat and to force the Resolutions tab to
 * refresh without waiting for its next poll.
 */
export type WorkOrderResolved = {
  hitlId: string;
  workflowId: string;
  sessionId: string | null;
  decision: "approve" | "reject";
  chatMessage: string | null;
  payload: any;
  resolvedAt: string;
};

const bus = new EventTarget();
const EVENT = "work-order-resolved";

export function emitWorkOrderResolved(detail: WorkOrderResolved) {
  bus.dispatchEvent(new CustomEvent(EVENT, { detail }));
}

export function onWorkOrderResolved(handler: (e: WorkOrderResolved) => void): () => void {
  const wrapped = (e: Event) => handler((e as CustomEvent<WorkOrderResolved>).detail);
  bus.addEventListener(EVENT, wrapped);
  return () => bus.removeEventListener(EVENT, wrapped);
}
