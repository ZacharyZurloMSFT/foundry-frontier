import { BusinessApi, BusinessError } from "../api";
import { AssistantEvent, AssistantStatus, parseEvent, parseStatus } from "./assistantModels";

export const MAX_STREAM_BYTES = 1024 * 1024;
const MAX_EVENTS = 64;
export interface ChatRequest {
  message: string; session_id?: string; context?: { claim_id?: string; policy_id?: string };
}

/** Reads a bounded, ordered NDJSON stream: one session first, then events, then exactly one terminal done. */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<AssistantEvent> {
  const reader = body.getReader(), decoder = new TextDecoder();
  let buffer = "", bytes = 0, count = 0, started = false, finished = false;
  function* lines(final: boolean): Generator<AssistantEvent> {
    const parts = buffer.split("\n");
    buffer = final ? "" : parts.pop() ?? "";
    for (const line of final ? parts.filter(part => part.trim()) : parts) {
      if (!line.trim()) continue;
      if (finished) throw new Error("Unexpected assistant data after completion.");
      if (++count > MAX_EVENTS) throw new Error("The assistant response exceeded its event limit.");
      let value: unknown;
      try { value = JSON.parse(line); } catch { throw new Error("Invalid assistant event."); }
      const event = parseEvent(value);
      if (!started && event.kind !== "session") throw new Error("The assistant response did not start with a session.");
      if (started && event.kind === "session") throw new Error("Unexpected second assistant session.");
      started = true;
      if (event.kind === "done") finished = true;
      yield event;
    }
  }
  try {
    for (;;) {
      const { value, done } = await reader.read();
      bytes += value?.length ?? 0;
      if (bytes > MAX_STREAM_BYTES) throw new Error("The assistant response exceeded its size limit.");
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      yield* lines(done);
      if (done) break;
    }
    if (!finished) throw new Error("The assistant response was interrupted; nothing was confirmed.");
  } finally {
    try { await reader.cancel(); } catch { /* the stream already ended */ }
    reader.releaseLock();
  }
}

export class AssistantApi {
  constructor(private readonly api: BusinessApi) {}
  status(): Promise<AssistantStatus> { return this.api.request("/assistants/status", parseStatus); }
  async *chat(surface: string, request: ChatRequest, signal?: AbortSignal): AsyncGenerator<AssistantEvent> {
    const response = await this.api.exchange(
      `/assistants/${encodeURIComponent(surface)}/chat`, "POST", JSON.stringify(request),
      { "Content-Type": "application/json", Accept: "application/x-ndjson" }, { signal, readOnly: true });
    if (!response.ok) {
      await this.api.readJson(response, () => { throw new Error("Unexpected assistant response."); });
      return;
    }
    if (!response.body || !(response.headers.get("content-type") ?? "").startsWith("application/x-ndjson")) {
      throw new BusinessError("The assistant response had an unexpected format. Nothing was confirmed.", response.status, "invalid_response", response.headers.get("X-Correlation-ID"));
    }
    yield* readEvents(response.body);
  }
}
