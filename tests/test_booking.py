"""Lead capture and booking, with the model's extraction step scripted."""

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import load_config
from app.intake import ASK, Intake
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
# What the caller said; a phone number is only kept if the caller said its digits.
CALL = [{"role": "user", "content": "I'm Ravi Kumar, my number is 98300 12345, email Ravi dot K at gmail dot com"}]


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


def test_closed_dates_are_skipped():
    # Close Tuesday 6 October, which would otherwise hold the first slot
    cfg = replace(CFG.booking, closed_dates=["2026-10-06"])
    starts = all_slots(cfg, NOW)
    assert all(s.date() != date(2026, 10, 6) for s in starts)
    assert starts[0] == datetime(2026, 10, 7, 10, 0, tzinfo=TZ)


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
    history = CALL

    run(intake.update(history))
    assert intake.fields["email"] == "ravi.k@gmail.com"
    assert intake.fields["phone"] == "9830012345"
    assert intake.next_line.startswith("I can offer a free site visit on Tuesday 6 October at 10 am")
    assert intake.next_line.endswith("Which suits you best?")
    assert intake.lead_in  # the model may answer a question before the offer

    run(intake.update(history))
    assert intake.slot.id == "S2" and not intake.booked_ref
    # The code reads the details back itself, the phone digit by digit.
    assert intake.next_line == (
        "Let me read that back. Ravi Kumar, phone 9 8 3 0 0, 1 2 3 4 5. "
        "New house construction at New Town, Kolkata. "
        f"Free site visit on {intake.slot.spoken()}. Shall I book it?"
    )
    assert not intake.lead_in  # the read-back stands alone

    run(intake.update(history))
    assert intake.booked_ref == "L0001"
    assert intake.next_line.startswith(f"Your free site visit is booked for {intake.slot.spoken()}.")
    assert "reference number is L 0 0 0 1" in intake.next_line
    assert not intake.lead_in

    run(intake.update(history))  # announced once; after that the model answers freely
    assert intake.next_line == "" and "BOOKED" in intake.context()

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
    run(intake.update(CALL))
    intake.context()  # read-back step
    run(intake.update(CALL + [{"role": "user", "content": "Yes, but my number is 98300 99999"}]))
    assert not intake.booked_ref
    assert intake.fields["phone"] == "9830099999"


def test_slot_taken_by_another_caller(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    a = Intake(CFG, store, ScriptedLLM([{**DETAILS, "slot_id": "S1"}, {**DETAILS, "slot_id": "S1", "confirmed": True}]), now=lambda: NOW)
    b = Intake(CFG, store, ScriptedLLM([{**DETAILS, "name": "Asha", "slot_id": "S1"}]), now=lambda: NOW)
    run(a.update(CALL))
    run(b.update(CALL))
    a.context()
    b.context()
    run(a.update(CALL))  # A books S1
    assert a.booked_ref
    b.llm.extractions = [{"confirmed": True}]
    run(b.update(CALL))  # B's slot is gone now
    assert not b.booked_ref and b.slot is None
    assert b.next_line.startswith("Sorry, that time was just taken. I can offer")


def test_partial_lead_saved_without_booking(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    intake = Intake(CFG, store, ScriptedLLM([{"name": "Meera", "phone": "not provided"}]), now=lambda: NOW)
    run(intake.update([{"role": "user", "content": "I'm Meera"}]))
    [lead] = store.list_leads()
    assert lead["name"] == "Meera" and lead["status"] == "new"
    assert "Caller: I'm Meera" in lead["transcript"]
    # The model said "not provided" for a phone nobody asked for yet: ignored, so Riya asks.
    assert intake.next_line == ASK["phone"]

    # Asked and refused: move on to the next detail.
    intake.llm.extractions = [{"phone": "not provided"}]
    run(intake.update([{"role": "user", "content": "I'd rather not give my number"}]))
    assert intake.fields["phone"] == "not provided"
    assert intake.next_line == ASK["project_type"]

    # Everything else given, phone still refused: ask again, saying why, instead of offering slots.
    intake.llm.extractions = [{**DETAILS, "phone": "", "email": ""}, {"email": "not provided"}]
    run(intake.update([{"role": "user", "content": "A new house in New Town, 50 to 60 lakh, after Diwali"}]))
    assert intake.next_line == ASK["email"]
    run(intake.update([{"role": "user", "content": "skip"}]))
    assert intake.next_line == "I'll need your phone number to book the visit. " + ASK["phone"]


def test_extraction_failure_does_not_break_call(tmp_path):
    class Broken(ScriptedLLM):
        async def extract(self, messages, schema):
            raise RuntimeError("model returned junk")

    intake = Intake(CFG, LeadStore(tmp_path / "leads.db"), Broken([]), now=lambda: NOW)
    run(intake.update([]))
    assert intake.next_line == "May I have your full name, please?"


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


def test_phone_kept_only_if_caller_said_it(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    # Whisper dropped digits; the model then made up a full-length number.
    heard = [{"role": "user", "content": "My phone number is 98,312,345."}]
    intake = Intake(CFG, store, ScriptedLLM([{**DETAILS, "phone": "+93123345690"}]), now=lambda: NOW)
    run(intake.update(heard))
    assert intake.fields["phone"] == ""
    assert "phone number" in intake.next_line  # asks again instead of saving a wrong number

    # Too short to be a full number, even though the caller said it.
    intake.llm.extractions = [{**DETAILS, "phone": "98312345"}]
    run(intake.update(heard))
    assert intake.fields["phone"] == ""

    # The caller says it again: taken from their words even though the model is stuck on the old guess.
    for words in ("Sure, slowly, 98300, 12345.", "nine eight three zero zero, one two three four five",
                  "my budget is 50 lakh and my number is 98300 1 2 3 4 5"):
        intake.fields["phone"] = ""
        intake.llm.extractions = [{**DETAILS, "phone": "+98312345"}]
        run(intake.update(heard + [{"role": "user", "content": words}]))
        assert intake.fields["phone"] == "9830012345", words

    # A country code the model added to digits the caller said is fine.
    intake.fields["phone"] = ""
    intake.llm.extractions = [{**DETAILS, "phone": "+91 98300 12345"}]
    run(intake.update([{"role": "user", "content": "it's 98300 12345"}, {"role": "user", "content": "yes"}]))
    assert intake.fields["phone"] == "+919830012345"


def test_false_booking_claims_are_never_spoken(tmp_path):
    intake = Intake(CFG, LeadStore(tmp_path / "leads.db"), ScriptedLLM([]), now=lambda: NOW)
    fine = [
        "Would you like me to go ahead and book it?",
        "Shall I book Tuesday at 10 am for you?",
        "I can offer Tuesday at 10 am or Wednesday at 11 am.",
    ]
    for sentence in fine:
        assert intake.screen(sentence) == sentence

    # Not booked: the first claim becomes a correction, later ones are dropped.
    first = intake.screen("I'll go ahead and book a free site visit for you on Tuesday at 10 am.")
    assert "not booked yet" in first
    assert intake.screen("Your visit is booked for Tuesday.") is None

    for claim in ("I've booked your site visit.", "I've confirmed the visit for Tuesday, October 27th, at 10 am."):
        run(intake.update([]))  # next caller turn resets the correction
        assert "not booked yet" in intake.screen(claim)
    run(intake.update([]))
    assert intake.screen("I've confirmed your phone number.") == "I've confirmed your phone number."

    # Once the code has booked it, saying so is allowed, but promising an email never is.
    intake.refresh_slots()
    intake.booked_ref, intake.slot = "L0001", intake.offered[0]
    run(intake.update([]))
    assert intake.screen("Your visit is booked for Tuesday at 10 am.") == "Your visit is booked for Tuesday at 10 am."
    assert intake.screen("We'll send you a reference number, L0001.") == (
        "Your reference number is L 0 0 0 1. A colleague will call you to confirm the details."
    )


def test_untrue_booking_is_not_spoken_over_websocket(tmp_path):
    class Claims(ScriptedLLM):
        async def stream(self, messages):
            yield "Thank you, Ravi. I'll go ahead and book a free site visit for you on Tuesday at 10 am. "
            yield "I'll send you a confirmation email."

    class STT:
        def transcribe(self, pcm):
            return ""

    class TTS:
        def synthesize(self, text):
            return b"RIFF"

    store = LeadStore(tmp_path / "leads.db")
    app = create_app(cfg=CFG, stt=STT(), llm=Claims([{**DETAILS, "slot_id": "S1"}]), tts=TTS(), store=store)
    spoken = []
    with TestClient(app).websocket_connect("/ws") as ws:
        while json.loads(ws.receive().get("text") or "{}").get("type") != "done":
            pass
        ws.send_json({"type": "text", "text": "Yes, please book it"})
        while True:
            msg = ws.receive()
            data = json.loads(msg.get("text") or "{}")
            if data.get("type") == "assistant":
                spoken.append(data["text"])
            if data.get("type") == "done":
                break
    assert spoken[0] == "Thank you, Ravi."
    assert "not booked yet" in spoken[1]
    # The email promise was dropped; then the code asks for the phone the caller never gave.
    assert spoken[2:] == ["Could I have your phone number? Please say it slowly, digit by digit."]
    assert not store.list_leads() or store.list_leads()[0]["appointment_start"] is None


def test_email_kept_only_if_caller_said_it(tmp_path):
    intake = Intake(CFG, LeadStore(tmp_path / "leads.db"), ScriptedLLM([]), now=lambda: NOW)
    # Whisper garbled it and the model filled in an example address instead.
    intake.llm.extractions = [{"email": "ravi.k@gmail.com"}]
    run(intake.update([{"role": "user", "content": "My email is RaviTechKumar at GM Packham."}]))
    assert intake.fields["email"] == ""

    intake.llm.extractions = [{"email": "ravi.kumar@gmail.com"}]
    run(intake.update([{"role": "user", "content": "It's ravi dot kumar at gmail dot com"}]))
    assert intake.fields["email"] == "ravi.kumar@gmail.com"
