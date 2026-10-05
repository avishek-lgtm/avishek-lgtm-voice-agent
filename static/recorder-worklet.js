// Collects microphone samples off the main thread and posts them in ~20 ms batches.
class RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.batch = new Float32Array(1024);
    this.length = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this.batch[this.length++] = channel[i];
        if (this.length === this.batch.length) {
          this.port.postMessage(this.batch.slice(0));
          this.length = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("recorder", RecorderProcessor);
