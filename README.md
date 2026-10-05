# Local Voice Agent

A talk-to-it voice assistant that runs entirely on your own computer, in the browser, for free. No API keys, no cloud services, no phone number.

| Step | What does it | Runs |
|---|---|---|
| Hearing you | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (speech to text) | locally, CPU or GPU |
| Thinking | [Ollama](https://ollama.com) with Llama 3.2 3B (language model) | locally |
| Speaking | [Piper](https://github.com/OHF-Voice/piper1-gpl) (text to speech) | locally |
| Glue | Small FastAPI server + a web page over a WebSocket | locally |

Out of the box the agent is **Ava**, a generic customer-support / FAQ assistant for a made-up company. Who she is and what she knows live in one editable file: [`config/agent.yaml`](config/agent.yaml).

## How a conversation flows

1. The page listens to your microphone and detects when you start and stop speaking.
2. When you pause, the audio goes to the server and Whisper turns it into text.
3. The text, the conversation so far and the agent's persona/knowledge go to the local Llama model.
4. The reply streams back sentence by sentence; each sentence is spoken by Piper as soon as it is ready, so the agent starts talking before the whole answer is written.
5. You can talk over the agent to interrupt it (wear headphones so it doesn't hear itself), or type instead of speaking.

## Requirements

- Python 3.10 or newer
- [Ollama](https://ollama.com/download) installed and running (`ollama serve`; the desktop app does this for you)
- About 4 GB of free disk space and 8 GB of RAM for the default models
- Chrome, Edge or Firefox, with a microphone

## Run it

```bash
git clone https://github.com/avishek-lgtm/avishek-lgtm-voice-agent.git
cd avishek-lgtm-voice-agent

# One time: Python packages, the Piper voice and the Ollama model
./scripts/setup.sh

# Every time
source .venv/bin/activate
python -m app.main
```

Open <http://localhost:8000>, click **Start talking**, allow the microphone, and speak. The first start takes a little longer because Whisper downloads its model (about 150 MB for `base.en`).

On Windows, run the steps in `scripts/setup.sh` by hand in PowerShell (`python -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt`, `python -m piper.download_voices --download-dir voices en_US-lessac-medium`, `ollama pull llama3.2:3b`), then `python -m app.main`.

## Make it your own

Everything is in [`config/agent.yaml`](config/agent.yaml); restart the server after editing.

- **Persona and rules:** `agent.name`, `agent.greeting`, `agent.system_prompt`.
- **What it knows:** the `knowledge` list of questions and answers. These are added to the prompt, and the agent is told to answer only from them and to offer a human hand-off otherwise.
- **Smarter or faster brain:** `llm.model`. Any Ollama model works, e.g. `llama3.1:8b` for better answers if you have the RAM (run `ollama pull <model>` first).
- **Better hearing:** `stt.model` (`small.en` is more accurate, `tiny.en` is faster). Set `device: cuda` and `compute_type: float16` on an NVIDIA GPU.
- **Different voice:** pick one from the [Piper voice list](https://huggingface.co/rhasspy/piper-voices/tree/main), download it with `python -m piper.download_voices --download-dir voices <voice-name>`, and point `tts.voice_path` at it.

You can keep several agents side by side: `python -m app.main --config config/another-agent.yaml`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach Ollama` on start | Start Ollama (`ollama serve` or open the Ollama app). |
| `Ollama model ... is not pulled` | `ollama pull llama3.2:3b` (or the model in your config). |
| `Piper voice not found` | Run `./scripts/setup.sh`, or check `tts.voice_path`. |
| It doesn't notice me talking | Raise **Mic sensitivity**; the black line on the level meter is the trigger level. |
| It keeps interrupting itself | Use headphones, or untick **Let me interrupt**. |
| Replies are slow | Use a smaller model (`llama3.2:1b`, `tiny.en`) or a GPU. |
| Microphone blocked when opened from another machine | Browsers only allow the mic on `localhost` or HTTPS. |

## Project layout

```
app/
  main.py      entry point (python -m app.main)
  server.py    web server and the per-call voice loop (WebSocket /ws)
  stt.py       Whisper speech to text
  llm.py       Ollama chat streaming and sentence splitting
  tts.py       Piper text to speech
  config.py    loads config/agent.yaml
config/agent.yaml   persona, knowledge and model settings
static/        the talk page (HTML, CSS, JS, mic recorder worklet)
scripts/setup.sh    one-time setup
tests/         tests that run with fake models
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests swap Whisper, Ollama and Piper for fakes, so they run in seconds without downloading any models.

## What's next

This prototype is the foundation. Natural next steps, none of which need paid services until you add phone calls:

- Real knowledge: load FAQs from a document or a database instead of the YAML list.
- Actions: let the agent look things up or create records (e.g. Odoo orders, appointments) through tool calls.
- Hand-off: save callback requests the agent collects to a file, email or CRM.
- Phone calls: connect a telephony provider (Twilio, Vonage, or a SIP trunk) when you're ready; that part is pay-per-use.
