class PcmCapture extends AudioWorkletProcessor {
  constructor() { super(); this.pending = new Float32Array(2048); this.offset = 0; }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (samples) {
      for (const sample of samples) {
        this.pending[this.offset++] = sample;
        if (this.offset === this.pending.length) {
          this.port.postMessage(this.pending);
          this.pending = new Float32Array(2048);
          this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
