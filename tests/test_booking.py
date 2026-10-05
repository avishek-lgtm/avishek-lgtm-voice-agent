"""Lead capture and booking, with the model's extraction step scripted."""

import asyncio
import json
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import load_config
from app.intake import Intake
from app.leads import LeadStore
from app.schedule import all_slots, available_slots
from app.server import create_app

CFG = load_config()
TZ = CFG.booking.tz
# Monday 5 October 2026, 9 am
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)

DETAILS = {
    "name": "Ravi Kumar",
    "phone": "98300 12345",
    "email": "Ravi.K@gmail.com",
    "project_type": "new house construction",
    "site_location": "New Town, Kolkata",
    "budget_range": "50 to 60 lakh",
    "timeline": "after Diwali",
}


class ScriptedLLM:
    """Returns the next scripted extraction for each caller turn."""

    def __init__(self, extractions):
        self.extractions = list(extractions)
        self.prompts = []

    async def extract(self, messages, schema):
        return self.extractions.pop(0) if self.extractions else {}

    async def stream(self, messages):
        self.prompts.append(messages[0]["content"])
        yield "Okay."


def run(coro):
    return asyncio.run(coro)


def test_slots_follow_schedule():
    starts = all_slots(CFG.booking, NOW)
    # 24 h notice: nothing on Monday, first slot Tuesday 10:00
    assert starts[0] == datetime(2026, 10, 6, 10, 0, tzinfo=TZ)
    assert all(s.weekday() != 6 for s in starts)  # closed Sundays
    assert all(s.hour != 13 for s in starts if s.weekday() < 5)  # weekday lunch break
    sat = [s for s in starts if s.weekday() == 5]
    assert sat and max(s.hour for s in sat) == 13  # last Saturday slot 13:00-14:00


def test_booked_slot_not_offered():
    first = all_slots(CFG.booking, NOW)[0].isoformat(timespec="minutes")
    offered = available_slots(CFG.booking, {first: 1}, NOW)
    assert first not in {s.iso for s in offered}
    assert len(offered) == CFG.booking.slots_to_offer
    assert len({s.start.date() for s in offered}) >= 3  # spread over several days


def test_books_only_after_read_back_and_yes(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    llm = ScriptedLLM(
        [
            {**DETAILS, "slot_id": "", "confirmed": False},
            {**DETAILS, "slot_id": "S2", "confirmed": True},  # "yes" before any read-back: ignored
            {**DETAILS, "slot_id": "S2", "confirmed": True},  # "yes" after read-back
        ]
    )
    intake = Intake(CFG, store, llm, now=lambda: NOW)
    history = [{"role": "user", "content": "hi"}]

    run(intake.update(history))
    assert intake.fields["email"] == "ravi.k@gmail.com"
    assert intake.fields["phone"] == "9830012345"
    assert "Offer two or three" in intake.context()

    run(intake.update(history))
    assert intake.slot.id == "S2" and not intake.booked_ref
    assert "read back" in intake.context()  # agent now reads back and asks to confirm

    run(intake.update(history))
    assert intake.booked_ref == "L0001"
    assert "BOOKED" in intake.context()

    [lead] = store.list_leads()
    assert lead["status"] == "appointment_booked"
    assert lead["name"] == "Ravi Kumar"
    assert lead["appointment_start"] == intake.slot.iso
    assert "Ravi Kumar" in store.to_csv()


def test_correction_during_confirmation_does_not_book(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    llm = ScriptedLLM(
        [
            {**DETAILS, "slot_id": "S1", "confirmed": False},
            {**DETAILS, "phone": "9830099999", "slot_id": "S1", "confirmed": True},
        ]
    )
    intake = Intake(CFG, store, llm, now=lambda: NOW)
    run(intake.update([]))
    intake.context()  # read-back step
    run(intake.update([]))
    assert not intake.booked_ref
    assert intake.fields["phone"] == "9830099999"


def test_slot_taken_by_another_caller(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    a = Intake(CFG, store, ScriptedLLM([{**DETAILS, "slot_id": "S1"}, {**DETAILS, "slot_id": "S1", "confirmed": True}]), now=lambda: NOW)
    b = Intake(CFG, store, ScriptedLLM([{**DETAILS, "name": "Asha", "slot_id": "S1"}]), now=lambda: NOW)
    run(a.update([]))
    run(b.update([]))
    a.context()
    b.context()
    run(a.update([]))  # A books S1
    assert a.booked_ref
    b.llm.extractions = [{"confirmed": True}]
    run(b.update([]))  # B's slot is gone now
    assert not b.booked_ref and b.slot is None
    assert "just taken" in b.context()


def test_partial_lead_saved_without_booking(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    intake = Intake(CFG, store, ScriptedLLM([{"name": "Meera", "phone": "not provided"}]), now=lambda: NOW)
    run(intake.update([{"role": "user", "content": "I'm Meera"}]))
    [lead] = store.list_leads()
    assert lead["name"] == "Meera" and lead["status"] == "new"
    assert "Caller: I'm Meera" in lead["transcript"]
    assert "phone number" in intake.context()  # still asks for the next missing detail


def test_extraction_failure_does_not_break_call(tmp_path):
    class Broken(ScriptedLLM):
        async def extract(self, messages, schema):
            raise RuntimeError("model returned junk")

    intake = Intake(CFG, LeadStore(tmp_path / "leads.db"), Broken([]), now=lambda: NOW)
    run(intake.update([]))
    assert "full name" in intake.context()


def test_call_over_websocket_creates_lead(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    llm = ScriptedLLM([{**DETAILS, "slot_id": ""}])

    class STT:
        def transcribe(self, pcm):
            return ""

    class TTS:
        def synthesize(self, text):
            return b"RIFF"

    app = create_app(cfg=CFG, stt=STT(), llm=llm, tts=TTS(), store=store)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        while json.loads(ws.receive().get("text") or "{}").get("type") != "done":
            pass
        ws.send_json({"type": "text", "text": "I'm Ravi, I want to build a house in New Town"})
        lead_msg = None
        while True:
            msg = ws.receive()
            if msg.get("text"):
                data = json.loads(msg["text"])
                if data["type"] == "lead":
                    lead_msg = data
                assert data["type"] != "error", data
                if data["type"] == "done":
                    break
    assert lead_msg["fields"]["full name"] == "Ravi Kumar"
    assert "Call progress" in llm.prompts[-1]

    with client:
        leads = client.get("/api/leads").json()
        assert leads[0]["reference"] == "L0001"
        csv = client.get("/api/leads.csv")
        assert csv.headers["content-type"].startswith("text/csv")
        assert "New Town" in csv.text
