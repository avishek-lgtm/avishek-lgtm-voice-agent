# Local Voice Agent

A talk-to-it voice assistant that runs entirely on your own computer, in the browser, for free. No API keys, no cloud services, no phone number.

| Step | What does it | Runs |
|---|---|---|
| Hearing you | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (speech to text) | locally, CPU or GPU |
| Thinking | [Ollama](https://ollama.com) with Llama 3.2 3B (language model) | locally |
| Speaking | [Piper](https://github.com/OHF-Voice/piper1-gpl) (text to speech) | locally |
| Glue | Small FastAPI server + a web page over a WebSocket | locally |

Out of the box the agent is **Riya**, the front desk of a construction company (placeholder name: BuildRight Constructions). On a call she:

- answers questions about the company's services, areas and process from an editable FAQ list,
- collects the caller's details: name, phone, project type, site location, budget range, timeline and email,
- offers open slots for a **free site visit** from a weekly schedule, reads the details back, and books it once the caller says yes,
- saves every caller as a **lead** (even if they hang up before booking), with the call transcript.

Leads and booked visits go to a **Google Sheet** (one row per caller, updated live during the call) once you connect one; see [Save leads to Google Sheets](#save-leads-to-google-sheets). They are also always kept locally and shown at <http://localhost:8000/leads.html>, with a CSV download. Who the agent is, what it knows, which details it collects and when visits can be booked all live in one editable file: [`config/agent.yaml`](config/agent.yaml).

## How a conversation flows

1. The page listens to your microphone and detects when you start and stop speaking.
2. When you pause, the audio goes to the server and Whisper turns it into text.
3. The local model reads the conversation and pulls out the caller's details as structured data. The code merges them, saves the lead, and decides the next step: ask for a missing detail, offer slots, read back and confirm, or book.
4. The persona, knowledge and that next step go to the local Llama model, which writes the spoken reply.
5. The reply streams back sentence by sentence; each sentence is spoken by Piper as soon as it is ready, so the agent starts talking before the whole answer is written.
6. You can talk over the agent to interrupt it (wear headphones so it doesn't hear itself), or type instead of speaking.

## Requirements

- Python 3.10 or newer
- [Ollama](https://ollama.com/download) 0.5 or newer, installed and running (`ollama serve`; the desktop app does this for you)
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

## How booking works

- Open slots come from `booking.weekly_hours` in the config (with lunch breaks, closed days and `closed_dates`), minus visits already booked, at least `min_notice_hours` ahead. The agent only ever offers times from that list.
- The booking is written by the code, not the model. It happens only after the agent has read the details back and the caller agreed, and only if name, phone, project type and site location are known (`lead.required`). If the caller corrects anything during the read-back, the agent reads back again.
- If two callers pick the same slot, the second is told it was just taken and offered others.
- The agent tells the caller their reference (e.g. L0007), which matches the leads page.

## Save leads to Google Sheets

Free; it uses a Google Cloud *service account* (a robot Google user) that you share the sheet with. One-time setup, about 10 minutes:

1. Go to <https://console.cloud.google.com/>, create a project (any name, no billing needed).
2. In **APIs & Services → Library**, search for **Google Sheets API** and click **Enable**.
3. In **APIs & Services → Credentials**, click **Create credentials → Service account**, give it a name, and finish (no roles needed).
4. Open the new service account, go to **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
5. Save that file in this project as `secrets/google-service-account.json` (the `secrets/` folder is git-ignored; never commit or share this file).
6. Create a Google Sheet for the leads. Click **Share**, paste the service account's email (it looks like `name@project-id.iam.gserviceaccount.com`, shown in the JSON as `client_email`), and give it **Editor** access.
7. Copy the sheet's id from its URL, `docs.google.com/spreadsheets/d/`**`<this part>`**`/edit`, into `config/agent.yaml`:

   ```yaml
   google_sheets:
     enabled: true
     spreadsheet_id: "1AbC...xyz"
   ```

8. Restart the server. The log says `Saving leads to Google Sheet ...`, and a **Leads** tab with a header row appears.

How it behaves:

- Each caller gets one row, keyed by their reference (L0001, L0002, ...). The row is created as soon as the agent has their name or phone and is updated as the call goes on, ending with the booked visit time.
- The local database (`data/leads.db`) is still used to decide which slots are free, so two callers can never book the same slot. That means editing or deleting a visit in the sheet does **not** free the slot for the agent; for now, cancel visits by also telling the agent's operator, or see "What's next".
- Writes happen in the background. If Google is unreachable, calls carry on, the error is logged, and the lead is still saved locally (and on the leads page); the next change to that lead re-sends it.
- Keep the sheet's columns in the same order; you can add your own columns to the right (e.g. "Follow-up by", "Outcome") and they're left alone.

## Make it your own

Everything is in [`config/agent.yaml`](config/agent.yaml); restart the server after editing.

- **Company, persona and rules:** `agent.name`, `agent.company`, `agent.greeting`, `agent.system_prompt`. All company details shipped in the file are placeholders.
- **What it knows:** the `knowledge` list of questions and answers. The agent answers only from them and never quotes prices.
- **What it collects:** `lead.ask` (order of questions), `lead.required` (needed to book) and `lead.project_types`.
- **When visits can be booked:** the `booking` section (hours per weekday, visit length, notice period, closed dates, visits per slot, time zone). Set `booking.enabled: false` to only capture leads.
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
| Replies are slow | Each caller turn makes two model calls (detail extraction, then the reply). Use a GPU, or `tiny.en` for Whisper. |
| Names, phone numbers or emails are misheard | Use `stt.model: small.en`; the agent reads details back before booking so the caller can correct them. |
| The agent skips steps or forgets details | Use a larger model (`llama3.1:8b`). |
| `Google credentials not found` / `Cannot open the Google Sheet` | Check the key file path, the `spreadsheet_id`, and that the sheet is shared as Editor with the service account email. |
| Microphone blocked when opened from another machine | Browsers only allow the mic on `localhost` or HTTPS. |

## Project layout

```
app/
  main.py      entry point (python -m app.main)
  server.py    web server and the per-call voice loop (WebSocket /ws)
  stt.py       Whisper speech to text
  llm.py       Ollama chat streaming, structured extraction, sentence splitting
  intake.py    collects caller details, decides the next step, books visits
  schedule.py  open appointment slots from the weekly schedule
  leads.py     SQLite storage for leads and appointments, CSV export
  sheets.py    copies every lead into Google Sheets
  tts.py       Piper text to speech
  config.py    loads config/agent.yaml
config/agent.yaml   persona, knowledge and model settings
static/        the talk page, the leads page, mic recorder worklet
data/leads.db  created on first run; holds caller personal data, not committed
scripts/setup.sh    one-time setup
tests/         tests that run with fake models
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests swap Whisper, Ollama and Piper for fakes, so they run in seconds without downloading any models.

## Privacy

`data/leads.db` and the Google Sheet contain callers' names, phone numbers and emails; share the sheet only with people who need it. The server listens on `127.0.0.1` only, and the leads page has no login, so don't start it with `--host 0.0.0.0` on a shared network without adding authentication first.

## What's next

- Read cancellations back from the Google Sheet so a cancelled visit frees its slot.
- Push leads and visits into a CRM or ERP (e.g. Odoo CRM leads and calendar events).
- Notify the sales team (email or WhatsApp) when a visit is booked.
- Let callers reschedule or cancel an existing visit by reference number.
- Phone calls: connect a telephony provider (Twilio, Vonage, or a SIP trunk) when you're ready; that part is pay-per-use.
