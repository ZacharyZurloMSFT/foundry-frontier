import { VoiceSession, voiceProxyUrl } from "./api";

export interface TranscriptLine { who: "you" | "agent"; text: string; }
export interface VoiceEvent { type: string; [key: string]: any; }

export function completedTranscript(event: VoiceEvent): TranscriptLine | null {
  if (typeof event.transcript !== "string" || !event.transcript.trim()) return null;
  if (event.type === "conversation.item.input_audio_transcription.completed") return { who: "you", text: event.transcript.slice(0, 12000) };
  if (event.type === "response.audio_transcript.done") return { who: "agent", text: event.transcript.slice(0, 12000) };
  return null; // Delta text is never verified intake or an executable handoff.
}
export class PcmEncoder {
  private pending: number[] = [];
  private position = 0;
  constructor(private readonly sourceRate: number) {
    if (!Number.isFinite(sourceRate) || sourceRate < 24000 || sourceRate > 192000) throw new Error("Microphone sample rate is unsupported.");
  }
  encode(samples: Float32Array): string {
    this.pending.push(...samples);
    const output: number[] = [];
    const step = this.sourceRate / 24000;
    while (this.position + 1 < this.pending.length) {
      const i = Math.floor(this.position), fraction = this.position - i;
      const sample = Math.max(-1, Math.min(1, this.pending[i] * (1 - fraction) + this.pending[i + 1] * fraction));
      output.push(Math.round(sample * (sample < 0 ? 32768 : 32767)));
      this.position += step;
    }
    const consumed = Math.floor(this.position);
    this.pending.splice(0, consumed); this.position -= consumed;
    const bytes = new Uint8Array(output.length * 2);
    const view = new DataView(bytes.buffer);
    output.forEach((value, i) => view.setInt16(i * 2, value, true));
    return btoa(String.fromCharCode(...bytes));
  }
}

/** No credentials, transcript or raw audio are stored outside this call instance. */
export class VoiceLiveConnection {
  private socket?: WebSocket;
  private stream?: MediaStream;
  private context?: AudioContext;
  private source?: MediaStreamAudioSourceNode;
  private capture?: AudioWorkletNode;
  private silent?: GainNode;
  private playing = new Set<AudioBufferSourceNode>();
  private nextAudio = 0;
  private closed = false;
  private ready = false;
  private timer?: ReturnType<typeof setTimeout>;
  private deadline?: ReturnType<typeof setTimeout>;
  private rejectConnect?: (error: Error) => void;
  private outputItem?: string;
  private outputStarted: number | null = null;
  private outputDuration = 0;
  private playedOffset = 0;
  private interrupted = false;
  constructor(
    private onTranscript: (line: TranscriptLine) => void,
    private onEnd: (message: string) => void,
  ) {}
  private waitForPhase<T>(promise: Promise<T>, timeoutMessage: string): Promise<T> {
    return new Promise((resolve, reject) => {
      this.rejectConnect = reject;
      this.timer = setTimeout(() => this.finish(timeoutMessage), 20000);
      promise.then((result) => {
        if (this.closed) return;
        clearTimeout(this.timer); this.rejectConnect = undefined; resolve(result);
      }, reject);
    });
  }
  async connect(session: VoiceSession): Promise<void> {
    try {
      this.context = new AudioContext({ sampleRate: 24000 });
      await this.waitForPhase(this.context.resume(), "Audio device startup timed out.");
      if (this.closed) throw new Error("Call cancelled.");
      const microphone = navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      const stream = await this.waitForPhase(microphone.then((media) => {
        if (this.closed) media.getTracks().forEach((track) => track.stop());
        else this.stream = media;
        return media;
      }), "Microphone permission timed out. Start a new call when ready.");
      if (this.closed) throw new Error("Call cancelled.");
      await this.waitForPhase(this.context.audioWorklet.addModule(new URL("./pcm-worklet.js", import.meta.url).href), "Audio capture startup timed out.");
      if (this.closed) throw new Error("Call cancelled.");
      this.source = this.context.createMediaStreamSource(stream);
      this.capture = new AudioWorkletNode(this.context, "pcm-capture");
      this.silent = this.context.createGain(); this.silent.gain.value = 0;
      this.source.connect(this.capture); this.capture.connect(this.silent); this.silent.connect(this.context.destination);
      const encoder = new PcmEncoder(this.context.sampleRate);
      this.capture.port.onmessage = ({ data }: MessageEvent<Float32Array>) => {
        if (this.ready && !this.closed) {
          try {
            const audio = encoder.encode(data);
            if (audio) this.send({ type: "input_audio_buffer.append", audio });
          } catch { this.finish("Microphone audio could not be streamed. Use text intake instead."); }
        }
      };
      await new Promise<void>((resolve, reject) => {
        this.rejectConnect = reject;
        const socket = new WebSocket(voiceProxyUrl()); this.socket = socket;
        this.timer = setTimeout(() => { this.finish("Voice Live connection timed out."); }, 20000);
        socket.onopen = () => this.send({ type: "proxy.connect", ticket: session.connect_ticket });
        socket.onerror = () => this.finish("Voice Live WebSocket failed. Text intake remains available.");
        socket.onclose = () => this.finish("Voice Live disconnected. Review your intake; no handoff was sent.");
        socket.onmessage = ({ data }) => {
          if (this.closed) return;
          try {
            if (typeof data !== "string" || data.length > 512 * 1024) throw new Error("Invalid Voice Live frame.");
            const event: VoiceEvent = JSON.parse(data);
            if (event.type === "error") throw new Error(event.error?.message || "Azure Voice Live rejected this call.");
            if (event.type === "proxy.ready" && !this.ready) {
              this.ready = true; clearTimeout(this.timer); this.rejectConnect = undefined;
              this.deadline = setTimeout(() => this.finish("Voice session reached its time limit."), session.max_session_seconds * 1000);
              resolve();
            }
            if (event.type === "conversation.item.input_audio_transcription.failed") {
              throw new Error("Speech transcription failed. End the call and use verified text intake.");
            }
            if (event.type === "response.done" && event.response?.status === "failed") {
              throw new Error("Azure Voice Live could not complete its response.");
            }
            if (event.type === "input_audio_buffer.speech_started" && !this.interrupted) {
              this.interrupted = true;
              const wasPlaying = this.playing.size > 0;
              this.clearPlayback();
              if (wasPlaying && this.outputItem && this.outputStarted !== null) this.send({
                type: "conversation.item.truncate", item_id: this.outputItem, content_index: 0,
                audio_end_ms: Math.max(0, Math.round(this.playedOffset)),
              });
            }
            if (event.type === "response.output_item.added") {
              this.outputItem = event.item?.id; this.outputStarted = null; this.outputDuration = 0;
              this.playedOffset = 0; this.interrupted = false;
            }
            const line = completedTranscript(event);
            if (line) this.onTranscript(line);
            if (event.type === "response.audio.delta" && !this.interrupted) this.play(event.delta);
          } catch (error) { this.finish(error instanceof Error ? error.message : "Voice event failed."); }
        };
      });
    } catch (error) {
      this.close();
      throw error;
    }
  }
  private send(event: VoiceEvent) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    if (this.socket.bufferedAmount > 256 * 1024) { this.finish("Voice connection is too slow; call stopped to avoid queued audio."); return; }
    this.socket.send(JSON.stringify(event));
  }
  private play(base64: string) {
    const context = this.context;
    if (!context || this.closed) return;
    const binary = atob(base64);
    if (binary.length % 2) throw new Error("Invalid PCM16 response audio.");
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    const buffer = context.createBuffer(1, bytes.length / 2, 24000);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    if (this.nextAudio - context.currentTime > 10) throw new Error("Voice playback is delayed; call stopped rather than replaying stale audio.");
    const node = context.createBufferSource(); node.buffer = buffer; node.connect(context.destination);
    const start = Math.max(context.currentTime, this.nextAudio);
    if (this.outputStarted === null) this.outputStarted = start;
    this.outputDuration += buffer.duration;
    node.start(start); this.nextAudio = start + buffer.duration;
    this.playing.add(node);
    node.onended = () => { this.playing.delete(node); node.disconnect(); };
  }
  private clearPlayback() {
    if (this.context && this.outputStarted !== null) {
      this.playedOffset = Math.min(this.outputDuration * 1000, Math.max(0, (this.context.currentTime - this.outputStarted) * 1000));
    }
    for (const node of this.playing) { try { node.stop(); } catch {} node.disconnect(); }
    this.playing.clear(); this.nextAudio = 0;
  }
  private finish(message: string) {
    if (this.closed) return;
    this.rejectConnect?.(new Error(message));
    this.close(); this.onEnd(message);
  }
  close() {
    if (this.closed) return;
    this.closed = true; this.ready = false;
    clearTimeout(this.timer); clearTimeout(this.deadline);
    this.rejectConnect?.(new Error("Call ended.")); this.rejectConnect = undefined;
    this.clearPlayback();
    if (this.capture) { this.capture.port.onmessage = null; this.capture.port.close(); this.capture.disconnect(); }
    this.source?.disconnect(); this.silent?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.socket) { this.socket.onmessage = null; this.socket.onclose = null; this.socket.onerror = null; this.socket.onopen = null; this.socket.close(); }
    void this.context?.close().catch(() => {});
    this.stream = undefined; this.socket = undefined; this.context = undefined;
  }
}
