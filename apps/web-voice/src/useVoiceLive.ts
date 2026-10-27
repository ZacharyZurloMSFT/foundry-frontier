import { useEffect, useRef, useState } from "react";
import { mintVoiceSession } from "./api";
import { TranscriptLine, VoiceLiveConnection } from "./voiceLive";

export function useVoiceLive() {
  const [phase, setPhase] = useState<"idle" | "connecting" | "live">("idle");
  const [status, setStatus] = useState("Voice Live has not started.");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const connection = useRef<VoiceLiveConnection>();
  const abort = useRef<AbortController>();
  const generation = useRef(0);
  const starting = useRef(false);
  function stop() {
    generation.current++; starting.current = false;
    abort.current?.abort(); connection.current?.close(); connection.current = undefined;
    setPhase("idle"); setStatus("Call ended. Review and explicitly confirm any handoff.");
  }
  useEffect(() => () => {
    generation.current++; abort.current?.abort(); connection.current?.close();
  }, []);
  async function start() {
    if (starting.current || connection.current) return;
    starting.current = true;
    const version = ++generation.current;
    abort.current = new AbortController();
    setTranscript([]); setPhase("connecting"); setStatus("Checking Voice Live and requesting microphone access…");
    try {
      const session = await mintVoiceSession(abort.current.signal);
      if (version !== generation.current) return;
      const call = new VoiceLiveConnection(
        (line) => { if (version === generation.current) setTranscript((lines) => [...lines.slice(-199), line]); },
        (message) => {
          if (version !== generation.current) return;
          setPhase("idle"); setStatus(message); connection.current = undefined; starting.current = false;
        },
      );
      connection.current = call;
      await call.connect(session);
      if (version !== generation.current) { call.close(); return; }
      setPhase("live"); setStatus(`Connected to Azure Voice Live (${session.model}). Speak to report a loss or discuss your intake.`);
    } catch (error) {
      if (version !== generation.current) return;
      connection.current?.close(); connection.current = undefined;
      setPhase("idle"); setStatus(error instanceof Error ? error.message : String(error));
    } finally { if (version === generation.current) starting.current = false; }
  }
  function reset() { stop(); setTranscript([]); }
  return { phase, status, transcript, start, stop, reset };
}
