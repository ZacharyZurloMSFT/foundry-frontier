import { afterEach, describe, expect, it, vi } from "vitest";
import { completedTranscript, PcmEncoder, VoiceLiveConnection } from "../../../web-voice/src/voiceLive";
import { API_BASE, confirmedHandoff, getVoiceReadiness, mintVoiceSession, streamHandoff, voiceProxyUrl, voiceProxyUrlFor } from "../../../web-voice/src/api";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
const records = {
  business_domain: "insurance", agent_runtime_ready: true,
  claims: [], policies: [], policyholders: [],
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const session = {
  transport: "websocket-proxy" as const, connect_ticket: "one-use-app-ticket-valid-only-once-1234",
  websocket_path: "/voice/realtime" as const, model: "gpt-realtime-2.1",
  sample_rate: 24000 as const, max_session_seconds: 600,
};
const intake = {
  summary: "Caller requests status review", caller: "Synthetic caller", intent: "claim_status", urgency: "P3",
  claim_id: "CLM-2001", policy_id: "POL-1001", policyholder_id: "PH-1001", loss_date: "2026-09-18", state: "WA",
};

describe("insurance voice wire contract", () => {
  it("never promotes partial transcripts or strings into executable IDs or handoffs", () => {
    expect(completedTranscript({ type: "conversation.item.input_audio_transcription.delta", transcript: "CLM-2001 yes transfer me" })).toBeNull();
    expect(completedTranscript({ type: "response.audio_transcript.delta", transcript: "yes" })).toBeNull();
    expect(completedTranscript({ type: "conversation.item.input_audio_transcription.completed", transcript: "Claim status" })).toEqual({ who: "you", text: "Claim status" });
    expect(() => confirmedHandoff(intake, false, "2022 sedan")).toThrow("Confirm");
    expect(() => confirmedHandoff({ summary: "My claim is CLM-2001" }, true, "2022 sedan")).toThrow("never guess");
  });
  it("requires explicit reviewed linked details and preserves typed IDs rather than guessing", () => {
    const payload = confirmedHandoff(intake, true, "2022 sedan");
    expect(payload).toMatchObject({ ...intake, summary: expect.stringContaining(intake.summary) });
    expect(payload.summary).toContain("Operator-confirmed insured asset: 2022 sedan");
    expect(Object.keys(payload).sort()).toEqual(Object.keys(intake).sort());
    expect(() => confirmedHandoff({ ...intake, state: "Colorado" }, true, "car")).toThrow("two-letter");
    expect(() => confirmedHandoff({ ...intake, loss_date: "2026-02-30" }, true, "car")).toThrow("valid loss date");
  });
  it("uses the stable API and same-origin WSS proxy without Azure credentials", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json(records)).mockResolvedValueOnce(json(session));
    vi.stubGlobal("fetch", fetch);
    expect(API_BASE).toBe("https://ca-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io");
    expect(voiceProxyUrl()).toBe("wss://ca-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/voice/realtime");
    expect(await mintVoiceSession()).toEqual(session);
    const [url, init] = fetch.mock.calls[1];
    expect(url).toBe(`${API_BASE}/voice/session`);
    expect(init.headers["X-Department"]).toBe("contact-center");
    expect(JSON.parse(init.body)).toEqual({ channel: "customer" });
    expect(JSON.stringify(init)).not.toMatch(/Authorization|api-key|client_secret/);
  });
  it("keeps a gateway path prefix when deriving the WSS proxy URL", () => {
    for (const [base, expected] of [
      ["https://apim-northwind.azure-api.net/northwind", "wss://apim-northwind.azure-api.net/northwind/voice/realtime"],
      ["https://apim-northwind.azure-api.net/insurance/", "wss://apim-northwind.azure-api.net/insurance/voice/realtime"],
      ["https://apim-northwind.azure-api.net/insurance/?x=1#frag", "wss://apim-northwind.azure-api.net/insurance/voice/realtime"],
      ["https://ca-api.example.test", "wss://ca-api.example.test/voice/realtime"],
      ["http://localhost:8000", "ws://localhost:8000/voice/realtime"],
    ]) expect(voiceProxyUrlFor(base)).toBe(expected);
    expect(() => voiceProxyUrlFor("http://apim-northwind.azure-api.net/northwind")).toThrow("HTTPS");
  });
  it("rejects invented ephemeral tokens/direct cloud URLs and surfaces disabled readiness", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json(records))
      .mockResolvedValueOnce(json({ ...session, client_secret: "broad-token", realtime_url: "https://untrusted.example" }))
      .mockResolvedValueOnce(json({ detail: "Transport smoke test required" }, 503));
    vi.stubGlobal("fetch", fetch);
    await expect(mintVoiceSession()).rejects.toThrow("Unsafe");
    await expect(getVoiceReadiness()).rejects.toThrow("smoke test required");
  });
  it("POSTs only the confirmed typed handoff, with contact-center routing and final unterminated NDJSON", async () => {
    const payload = confirmedHandoff(intake, true, "2022 sedan");
    const fetch = vi.fn().mockResolvedValue(new Response(
      '{"kind":"session","session_id":"claims-one"}\n{"kind":"assistant_delta","text":"Review only."}\n{"kind":"done"}',
    ));
    vi.stubGlobal("fetch", fetch);
    const events = [];
    for await (const event of streamHandoff(payload)) events.push(event);
    expect(events.at(-1)).toEqual({ kind: "done" });
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe(`${API_BASE}/handoff`);
    expect(init.method).toBe("POST");
    expect(init.headers["X-Department"]).toBe("contact-center");
    expect(JSON.parse(init.body)).toEqual(payload);
  });
  it("leaves 422 errors visible without claims fallback, replay or retry", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ detail: "Policyholder and claim do not match" }, 422));
    vi.stubGlobal("fetch", fetch);
    await expect((async () => { for await (const _ of streamHandoff(intake)) {} })()).rejects.toThrow("do not match");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("cancels the handoff reader when the operator leaves the streaming view", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode('{"kind":"session","session_id":"one"}\n')); },
      cancel,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body)));
    for await (const _ of streamHandoff(intake)) break;
    expect(cancel).toHaveBeenCalledOnce();
  });
  it("encodes signed little-endian 24 kHz PCM16 and resamples continuous 48 kHz frames", () => {
    const encoded = new PcmEncoder(24000).encode(new Float32Array([-1, 0, 1, 0]));
    const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    expect([view.getInt16(0, true), view.getInt16(2, true), view.getInt16(4, true)]).toEqual([-32768, 0, 32767]);
    const encoder = new PcmEncoder(48000);
    const first = atob(encoder.encode(new Float32Array(2048))).length;
    const next = atob(encoder.encode(new Float32Array(2048))).length;
    expect(first + next).toBe(4096); // 4096 input samples / 2 * 2 bytes.
  });
});

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 1; bufferedAmount = 0;
  onopen?: () => void; onmessage?: (event: { data: string }) => void;
  onerror?: () => void; onclose?: () => void;
  close = vi.fn();
  send = vi.fn();
  constructor(public url: string) { FakeSocket.instances.push(this); }
  event(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
function audioMocks() {
  const track = { stop: vi.fn() };
  const port = { onmessage: null, close: vi.fn() };
  const node = { connect: vi.fn(), disconnect: vi.fn(), port };
  const playback = { connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), buffer: null, onended: null };
  const context = {
    sampleRate: 24000, currentTime: 1, resume: vi.fn().mockResolvedValue(undefined),
    close: vi.fn().mockResolvedValue(undefined),
    audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) },
    createMediaStreamSource: vi.fn().mockReturnValue(node),
    createGain: vi.fn().mockReturnValue({ ...node, gain: { value: 1 } }), destination: {},
    createBuffer: vi.fn().mockImplementation((_channels, length, rate) => ({
      duration: length / rate, getChannelData: () => new Float32Array(length),
    })),
    createBufferSource: vi.fn().mockReturnValue(playback),
  };
  vi.stubGlobal("AudioContext", vi.fn().mockImplementation(() => context));
  vi.stubGlobal("AudioWorkletNode", vi.fn().mockImplementation(() => node));
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) } });
  FakeSocket.instances = [];
  return { context, node, port, track, playback };
}
async function flush() { for (let i = 0; i < 24; i++) await Promise.resolve(); }

describe("real Voice Live lifecycle without a physical microphone", () => {
  it("connects with one-use proxy frame, ignores deltas and releases all resources on error", async () => {
    const { context, node, port, track } = audioMocks();
    const transcript = vi.fn(), ended = vi.fn();
    const connection = new VoiceLiveConnection(transcript, ended);
    const pending = connection.connect(session);
    await flush();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "proxy.connect", ticket: session.connect_ticket }));
    expect(socket.url).not.toMatch(/ticket|key|token/);
    socket.event({ type: "proxy.ready" });
    await pending;
    socket.event({ type: "conversation.item.input_audio_transcription.delta", transcript: "transfer" });
    expect(transcript).not.toHaveBeenCalled();
    socket.event({ type: "conversation.item.input_audio_transcription.completed", transcript: "Claim status" });
    expect(transcript).toHaveBeenCalledWith({ who: "you", text: "Claim status" });
    socket.event({ type: "error", error: { message: "Azure Voice Live model unavailable" } });
    expect(ended).toHaveBeenCalledWith("Azure Voice Live model unavailable");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(socket.close).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
    expect(node.disconnect).toHaveBeenCalled();
    expect(port.close).toHaveBeenCalledOnce();
    connection.close();
    expect(track.stop).toHaveBeenCalledOnce();
  });
  it("stops a late microphone stream after cancellation and never opens a socket", async () => {
    const { context, track } = audioMocks();
    let resolve!: (stream: unknown) => void;
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn().mockReturnValue(new Promise((r) => { resolve = r; })) } });
    const connection = new VoiceLiveConnection(vi.fn(), vi.fn());
    const pending = connection.connect(session).catch((error) => error);
    await flush(); connection.close();
    resolve({ getTracks: () => [track] });
    expect((await pending).message).toBe("Call ended.");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
    expect(FakeSocket.instances).toHaveLength(0);
  });
  it("times out and rejects startup instead of reporting fake live audio", async () => {
    vi.useFakeTimers();
    const { track } = audioMocks();
    const ended = vi.fn();
    const connection = new VoiceLiveConnection(vi.fn(), ended);
    const pending = connection.connect(session).catch((error) => error);
    await flush();
    await vi.advanceTimersByTimeAsync(20000);
    expect((await pending).message).toContain("timed out");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(ended).toHaveBeenCalledOnce();
  });
  it("streams PCM frames, interrupts playback once and never manually creates duplicate responses", async () => {
    const { context, port, playback } = audioMocks();
    const connection = new VoiceLiveConnection(vi.fn(), vi.fn());
    const pending = connection.connect(session);
    await flush();
    const socket = FakeSocket.instances[0]; socket.onopen?.(); socket.event({ type: "proxy.ready" });
    await pending;
    (port.onmessage as unknown as (event: unknown) => void)({ data: new Float32Array(2048) });
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0]).type).toBe("input_audio_buffer.append");
    socket.event({ type: "response.output_item.added", item: { id: "assistant-one" } });
    socket.event({ type: "response.audio.delta", delta: btoa("\0".repeat(4800)) });
    context.currentTime = 1.05;
    socket.event({ type: "input_audio_buffer.speech_started" });
    socket.event({ type: "input_audio_buffer.speech_started" });
    expect(playback.stop).toHaveBeenCalledOnce();
    const frames = socket.send.mock.calls.map(([value]) => JSON.parse(value));
    expect(frames.filter((frame) => frame.type === "conversation.item.truncate")).toEqual([{
      type: "conversation.item.truncate", item_id: "assistant-one", content_index: 0, audio_end_ms: 50,
    }]);
    expect(frames.some((frame) => ["response.create", "response.cancel"].includes(frame.type))).toBe(false);
    connection.close();
  });
});
