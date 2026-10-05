"""Collects caller details during a call and books the appointment.

Each caller turn runs in two steps:
1. Extraction: the local model reads the recent conversation and returns the
   caller's details as JSON (Ollama structured output, temperature 0).
2. The code merges those details, decides the next step (ask for a missing
   detail, offer slots, read back and confirm, or book) and hands that step to
   the conversational model as part of its system prompt.

Booking itself is done by code, never by the model: a slot is only booked
after the agent has read the details back and the caller has said yes, and
the agent is only told "booked" once the database write succeeded.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime

from .config import Config
from .leads import FIELD_LABELS, LEAD_FIELDS, LeadStore, SlotTaken, reference
from .schedule import Slot, available_slots

log = logging.getLogger(__name__)

NOT_PROVIDED = "not provided"
TRANSCRIPT_WINDOW = 10  # messages the extractor sees

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        **{f: {"type": "string"} for f in LEAD_FIELDS},
        "notes": {"type": "string"},
        "slot_id": {"type": "string"},
        "confirmed": {"type": "boolean"},
    },
    "required": [*LEAD_FIELDS, "notes", "slot_id", "confirmed"],
}


class Intake:
    def __init__(self, cfg: Config, store: LeadStore, llm, now: Callable[[], datetime] | None = None):
        self.cfg, self.store, self.llm = cfg, store, llm
        self._now = now or (lambda: datetime.now(cfg.booking.tz))
        self.fields: dict[str, str] = {f: "" for f in LEAD_FIELDS}
        self.fields["notes"] = ""
        self.lead_id: int | None = None
        self.offered: list[Slot] = []
        self.slot: Slot | None = None
        self.awaiting_confirmation = False
        self.booked_ref: str | None = None
        self.slot_lost = False

    # ---------- state ----------

    def missing(self) -> list[str]:
        return [f for f in self.cfg.lead.ask if not self.fields.get(f)]

    def missing_required(self) -> list[str]:
        return [f for f in self.cfg.lead.required if self.fields.get(f) in ("", NOT_PROVIDED)]

    def refresh_slots(self) -> None:
        if self.cfg.booking.enabled:
            self.offered = available_slots(self.cfg.booking, self.store.booked_counts(), self._now())
            if self.slot:
                # Labels can shift as the list changes; follow the slot by its time.
                still_open = next((s for s in self.offered if s.iso == self.slot.iso), None)
                self.slot_lost = still_open is None
                self.slot = still_open

    # ---------- per turn ----------

    async def update(self, history: list[dict]) -> None:
        """Read the latest caller turn and advance the booking."""
        if self.booked_ref:
            return
        self.slot_lost = False
        self.refresh_slots()
        try:
            data = await self.llm.extract(self._extraction_messages(history), EXTRACT_SCHEMA)
        except Exception:  # a failed extraction must never break the call
            log.exception("Detail extraction failed")
            data = {}

        changed = self._merge(data)
        if (
            data.get("confirmed") is True
            and self.awaiting_confirmation
            and not changed
            and self.slot
            and not self.missing_required()
        ):
            self._book(history)
        self.save(history)

    def _merge(self, data: dict) -> bool:
        changed = False
        for f in LEAD_FIELDS + ["notes"]:
            value = _clean(f, data.get(f))
            if value and not _same(value, self.fields.get(f, "")):
                # Don't let "not provided" overwrite a real answer.
                if value == NOT_PROVIDED and self.fields.get(f):
                    continue
                self.fields[f] = value
                # Notes keep growing; they shouldn't block a confirmation.
                changed = changed or f != "notes"
        slot_id = str(data.get("slot_id") or "").strip().upper()
        chosen = next((s for s in self.offered if s.id == slot_id), None)
        if chosen and chosen != self.slot:
            self.slot = chosen
            changed = True
        if changed:
            self.awaiting_confirmation = False
        return changed

    def _book(self, history: list[dict]) -> None:
        b = self.cfg.booking
        self.lead_id = self.save(history)
        try:
            self.store.book(self.lead_id, self.slot.iso, b.duration_minutes, b.appointment_type, b.max_per_slot)
        except SlotTaken:
            log.info("Slot %s was taken during the call", self.slot.iso)
            self.slot, self.slot_lost, self.awaiting_confirmation = None, True, False
            self.refresh_slots()
            return
        self.booked_ref = reference(self.lead_id)
        log.info("Booked %s for lead %s", self.slot.iso, self.booked_ref)

    def save(self, history: list[dict]) -> int | None:
        """Persist the lead once there is a way to reach the caller back."""
        if self.lead_id is None and not (_real(self.fields["name"]) or _real(self.fields["phone"])):
            return None
        self.lead_id = self.store.save_lead(self.lead_id, self.fields, _transcript(history))
        return self.lead_id

    # ---------- prompts ----------

    def _extraction_messages(self, history: list[dict]) -> list[dict]:
        slots = "\n".join(f"{s.id}: {s.spoken()}" for s in self.offered) or "(none)"
        known = "\n".join(f"{f}: {self.fields[f] or '(unknown)'}" for f in LEAD_FIELDS)
        project_types = ", ".join(self.cfg.lead.project_types)
        system = f"""You extract caller details from a phone call transcript for a construction company.
Return JSON with these keys. Use only what the CALLER said. Use "" if not mentioned.
Use "{NOT_PROVIDED}" if the caller refused or does not know.
- name: caller's full name
- phone: digits only, keep a leading + (convert spoken numbers to digits)
- email: written form, e.g. "ravi dot k at gmail dot com" -> "ravi.k@gmail.com"
- project_type: short label; prefer one of: {project_types}
- site_location: area, city or address of the construction site
- budget_range: as the caller said it, e.g. "20 to 30 lakh"
- timeline: when they want to start or finish, as said
- notes: any other useful detail about the project, short
- slot_id: id of the appointment slot the caller chose from the list below, "" if none
- confirmed: true only if the agent's last message read back the details and asked to confirm,
  and the caller clearly agreed in their latest message; otherwise false

Details already known (repeat them unless the caller corrected them):
{known}

Appointment slots offered:
{slots}"""
        lines = []
        for m in history[-TRANSCRIPT_WINDOW:]:
            who = "Caller" if m["role"] == "user" else "Agent"
            lines.append(f"{who}: {m['content']}")
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "Transcript:\n" + "\n".join(lines)},
        ]

    def context(self) -> str:
        """Live call state for the conversational model's system prompt."""
        now = self._now()
        known = "\n".join(
            f"- {FIELD_LABELS[f]}: {self.fields.get(f) or '(not yet given)'}" for f in self.cfg.lead.ask
        )
        parts = [
            "## Call progress (live from the booking system)",
            f"Today is {now.strftime('%A')} {now.day} {now.strftime('%B %Y')}.",
            "Caller details so far:",
            known,
        ]
        b = self.cfg.booking
        if b.enabled and not self.booked_ref:
            slots = "\n".join(f"- {s.spoken()}" for s in self.offered) or "- none available, take details only"
            parts += [f"Open slots for a {b.appointment_type} (only offer these, never invent times):", slots]
            if self.slot:
                parts.append(f"Caller's chosen slot: {self.slot.spoken()}")
        parts.append("Your next step: " + self._next_step())
        parts.append("Never tell the caller the appointment is booked unless this section says it is booked.")
        return "\n".join(parts)

    def _next_step(self) -> str:
        b = self.cfg.booking
        self.awaiting_confirmation = False
        if self.booked_ref:
            return (
                f"The {b.appointment_type} is BOOKED for {self.slot.spoken()}, reference {_spell(self.booked_ref)}. "
                "Tell the caller, say a colleague will call to confirm before the visit, and ask if there is anything else."
            )
        prefix = ""
        if self.slot_lost:
            prefix = "The slot the caller wanted was just taken by someone else; apologise briefly. "
        missing = self.missing()
        if missing:
            return (
                prefix + "If the caller asked a question, answer it first. Then ask for their "
                f"{FIELD_LABELS[missing[0]]}. Ask for one detail at a time."
            )
        if not b.enabled:
            return "Thank the caller, tell them a colleague will contact them soon, and ask if there is anything else."
        if not self.offered:
            return "There are no open slots. Tell the caller a colleague will call them to arrange a visit."
        if not self.slot:
            return prefix + f"Offer two or three of the open slots for a {b.appointment_type} and ask which suits them."
        self.awaiting_confirmation = True
        return (
            "Briefly read back the caller's name, phone number, project type, site location and the chosen slot, "
            "then ask them to confirm so you can book it."
        )

    def snapshot(self) -> dict:
        """What the talk page shows in its side panel."""
        return {
            "fields": {FIELD_LABELS[f]: self.fields.get(f, "") for f in self.cfg.lead.ask},
            "slot": self.slot.spoken() if self.slot else "",
            "reference": self.booked_ref or (reference(self.lead_id) if self.lead_id else ""),
            "booked": bool(self.booked_ref),
        }


def _real(value: str) -> bool:
    return bool(value) and value != NOT_PROVIDED


def _clean(field: str, value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().strip(".")
    if value.lower() in ("", "unknown", "none", "null", "n/a", "(unknown)"):
        return ""
    if value.lower() == NOT_PROVIDED:
        return NOT_PROVIDED
    if field == "phone":
        digits = re.sub(r"[^\d+]", "", value)
        return digits if len(re.sub(r"\D", "", digits)) >= 7 else ""
    if field == "email":
        value = value.replace(" ", "").lower()
        return value if re.fullmatch(r"[^@]+@[^@]+\.[a-z]{2,}", value) else ""
    return value


def _same(a: str, b: str) -> bool:
    """Equal ignoring case, spacing and punctuation."""
    norm = lambda v: re.sub(r"[\W_]+", "", v.lower())  # noqa: E731
    return norm(a) == norm(b)


def _spell(ref: str) -> str:
    """'L0007' -> 'L 0 0 0 7' so TTS reads it character by character."""
    return " ".join(ref)


def _transcript(history: list[dict]) -> str:
    return "\n".join(f"{'Caller' if m['role'] == 'user' else 'Agent'}: {m['content']}" for m in history)
