// Browser side of the voice agent: captures the microphone, detects when the
// caller speaks, sends each utterance to the server and plays the replies.

const TARGET_RATE = 16000;     // what Whisper expects
const START_MS = 150;          // loud audio needed before we treat it as speech
const END_SILENCE_MS = 800;    // quiet time that ends an utterance
const PREROLL_MS = 300;        // audio kept from just before speech started
const MAX_UTTERANCE_MS = 30000;
const BARGE_IN_FACTOR = 2.5;   // speech must be this much louder while the agent talks

const $ = (id) => document.getElementById(id);
const ui = {
  name: $("agent-name"), status: $("status"), transcript: $("transcript"),
  call: $("call-btn"), reset: $("reset-btn"), sensitivity: $("sensitivity"),
  bargeIn: $("barge-in"), meter: $("meter-fill"), threshold: $("meter-threshold"),
  form: $("text-form"), input: $("text-input"), send: $("send-btn"),
};

let ws = null;
let ctx = null;
let micStream = null;
let micNode = null;
let active = false;

// Playback state
let nextPlayTime = 0;
let sources = [];
let decodeChain = Promise.resolve();
let generation = 0;            // bumps on interrupt so stale audio is dropped
let replyDone = true;
let lastAssistantEl = null;

// Voice activity detection state
let speaking = false, loudMs = 0, silenceMs = 0, utteranceMs = 0;
let preroll = [], chunks = [];

function setStatus(state, label) {
  ui.status.className = "status " + state;
  ui.status.textContent = label || state[0].toUpperCase() + state.slice(1);
}

function addMessage(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.textContent = text;
  ui.transcript.appendChild(el);
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
  return el;
}

function threshold() {
  // Higher sensitivity = lower loudness needed to count as speech.
  return 0.002 + (100 - Number(ui.sensitivity.value)) * 0.0006;
}

function agentAudible() {
  return ctx && ctx.currentTime < nextPlayTime;
}

// ---------- Connection ----------

async function start() {
  ctx = new AudioContext();
  await ctx.resume();

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    await ctx.audioWorklet.addModule("recorder-worklet.js");
    const source = ctx.createMediaStreamSource(micStream);
    micNode = new AudioWorkletNode(ctx, "recorder");
    micNode.port.onmessage = (e) => onAudio(e.data);
    const mute = ctx.createGain();
    mute.gain.value = 0;  // keep the graph running without echoing the mic
    source.connect(micNode).connect(mute).connect(ctx.destination);
  } catch (err) {
    addMessage("system", "Microphone unavailable (" + err.message + "). You can still type.");
  }

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.binaryType = "arraybuffer";
  ws.onmessage = onServerMessage;
  ws.onclose = () => { if (active) { addMessage("system", "Disconnected from server."); stop(); } };
  ws.onerror = () => setStatus("error", "Connection error");

  active = true;
  ui.call.textContent = "Hang up";
  ui.input.disabled = ui.send.disabled = false;
  setStatus("thinking", "Connecting");
}

function stop() {
  active = false;
  stopPlayback();
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (ctx) ctx.close();
  ctx = micStream = micNode = null;
  resetVad();
  ui.call.textContent = "Start talking";
  ui.input.disabled = ui.send.disabled = true;
  ui.meter.style.width = "0";
  setStatus("idle", "Not connected");
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(obj instanceof ArrayBuffer ? obj : JSON.stringify(obj));
}

function onServerMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    enqueueAudio(event.data);
    return;
  }
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case "ready":
      ui.name.textContent = msg.agent;
      document.title = msg.agent;
      break;
    case "user":
      addMessage("user", msg.text);
      lastAssistantEl = null;
      break;
    case "assistant":
      replyDone = false;
      if (lastAssistantEl) lastAssistantEl.textContent += " " + msg.text;
      else lastAssistantEl = addMessage("assistant", msg.text);
      ui.transcript.scrollTop = ui.transcript.scrollHeight;
      break;
    case "status":
      if (msg.state === "thinking") { replyDone = false; lastAssistantEl = null; }
      if (msg.state === "listening") replyDone = true;
      setStatus(msg.state);
      break;
    case "done":
      replyDone = true;
      lastAssistantEl = null;
      break;
    case "error":
      replyDone = true;
      addMessage("system", "Error: " + msg.message);
      setStatus("error", "Error");
      break;
  }
}

// ---------- Playback ----------

function enqueueAudio(data) {
  const gen = generation;
  decodeChain = decodeChain
    .then(() => ctx.decodeAudioData(data))
    .then((buffer) => {
      if (gen !== generation || !ctx) return;
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      const at = Math.max(ctx.currentTime + 0.02, nextPlayTime);
      src.start(at);
      nextPlayTime = at + buffer.duration;
      sources.push(src);
      src.onended = () => {
        sources = sources.filter((s) => s !== src);
        if (!sources.length && replyDone && !speaking) setStatus("listening");
      };
    })
    .catch((err) => console.error("Audio decode failed", err));
}

function stopPlayback() {
  generation++;
  sources.forEach((s) => { try { s.stop(); } catch (_) {} });
  sources = [];
  nextPlayTime = 0;
}

// ---------- Microphone and voice activity detection ----------

function resetVad() {
  speaking = false;
  loudMs = silenceMs = utteranceMs = 0;
  preroll = [];
  chunks = [];
}

function onAudio(samples) {
  if (!active || !ctx) return;
  const frameMs = (samples.length / ctx.sampleRate) * 1000;

  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  const rms = Math.sqrt(sum / samples.length);
  ui.meter.style.width = Math.min(100, (rms / 0.1) * 100) + "%";
  ui.threshold.style.left = Math.min(100, (threshold() / 0.1) * 100) + "%";

  const playing = agentAudible();
  if (replyDone && !playing && !speaking && ui.status.textContent === "Speaking") setStatus("listening");

  if (playing && !ui.bargeIn.checked) { resetVad(); return; }
  const loud = rms > threshold() * (playing ? BARGE_IN_FACTOR : 1);

  if (!speaking) {
    preroll.push(samples);
    while (preroll.length * frameMs > PREROLL_MS) preroll.shift();
    loudMs = loud ? loudMs + frameMs : 0;
    if (loudMs >= START_MS) {
      speaking = true;
      chunks = preroll.slice();
      utteranceMs = chunks.length * frameMs;
      silenceMs = 0;
      if (playing || !replyDone) { stopPlayback(); send({ type: "interrupt" }); replyDone = true; }
      setStatus("listening", "Hearing you…");
    }
    return;
  }

  chunks.push(samples);
  utteranceMs += frameMs;
  silenceMs = loud ? 0 : silenceMs + frameMs;
  if (silenceMs >= END_SILENCE_MS || utteranceMs >= MAX_UTTERANCE_MS) {
    const pcm = toPcm16(chunks, ctx.sampleRate);
    resetVad();
    replyDone = false;
    setStatus("thinking");
    send(pcm);
  }
}

// Join float chunks, downsample to 16 kHz by averaging, convert to Int16.
function toPcm16(parts, inRate) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const input = new Float32Array(total);
  let offset = 0;
  for (const p of parts) { input.set(p, offset); offset += p.length; }

  const ratio = inRate / TARGET_RATE;
  const outLength = Math.floor(total / ratio);
  const out = new Int16Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(total, Math.floor((i + 1) * ratio)) || start + 1;
    let acc = 0;
    for (let j = start; j < end; j++) acc += input[j];
    const v = Math.max(-1, Math.min(1, acc / (end - start)));
    out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  return out.buffer;
}

// ---------- UI wiring ----------

ui.call.addEventListener("click", () => (active ? stop() : start()));

ui.reset.addEventListener("click", () => {
  stopPlayback();
  resetVad();
  send({ type: "reset" });
  ui.transcript.innerHTML = "";
  addMessage("system", "New conversation started.");
});

ui.form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = ui.input.value.trim();
  if (!text) return;
  stopPlayback();
  replyDone = false;
  send({ type: "text", text });
  ui.input.value = "";
});
