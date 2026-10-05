"""Local lead and appointment storage (SQLite, a single file, no server)."""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# Caller details the agent collects, in the order it asks for them.
LEAD_FIELDS = [
    "name",
    "phone",
    "email",
    "project_type",
    "site_location",
    "budget_range",
    "timeline",
]

FIELD_LABELS = {
    "name": "full name",
    "phone": "phone number",
    "email": "email address",
    "project_type": "type of project",
    "site_location": "site location",
    "budget_range": "budget range",
    "timeline": "when they want to start",
    "notes": "notes",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',      -- new | appointment_booked
    name TEXT, phone TEXT, email TEXT,
    project_type TEXT, site_location TEXT, budget_range TEXT, timeline TEXT,
    notes TEXT,
    transcript TEXT
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    start TEXT NOT NULL,                     -- ISO 8601 with UTC offset
    duration_minutes INTEGER NOT NULL,
    appointment_type TEXT,
    status TEXT NOT NULL DEFAULT 'booked',   -- booked | cancelled
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS appointments_start ON appointments(start, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reference(lead_id: int) -> str:
    return f"L{lead_id:04d}"


class SlotTaken(Exception):
    pass


class LeadStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def save_lead(self, lead_id: int | None, fields: dict, transcript: str) -> int:
        """Create or update a lead and return its id."""
        values = {k: fields.get(k) or None for k in LEAD_FIELDS + ["notes"]}
        with self.lock, self.db:
            if lead_id is None:
                cols = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                cur = self.db.execute(
                    f"INSERT INTO leads (created_at, updated_at, transcript, {cols}) VALUES (?, ?, ?, {marks})",
                    (_now(), _now(), transcript, *values.values()),
                )
                return cur.lastrowid
            sets = ", ".join(f"{k} = ?" for k in values)
            self.db.execute(
                f"UPDATE leads SET updated_at = ?, transcript = ?, {sets} WHERE id = ?",
                (_now(), transcript, *values.values(), lead_id),
            )
            return lead_id

    def booked_counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT start, COUNT(*) AS n FROM appointments WHERE status = 'booked' GROUP BY start"
            ).fetchall()
        return {r["start"]: r["n"] for r in rows}

    def book(self, lead_id: int, start_iso: str, duration: int, kind: str, max_per_slot: int) -> int:
        """Book a slot for a lead; raises SlotTaken if someone got there first."""
        with self.lock, self.db:
            (taken,) = self.db.execute(
                "SELECT COUNT(*) FROM appointments WHERE start = ? AND status = 'booked'", (start_iso,)
            ).fetchone()
            if taken >= max_per_slot:
                raise SlotTaken(start_iso)
            cur = self.db.execute(
                "INSERT INTO appointments (lead_id, start, duration_minutes, appointment_type, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (lead_id, start_iso, duration, kind, _now()),
            )
            self.db.execute(
                "UPDATE leads SET status = 'appointment_booked', updated_at = ? WHERE id = ?", (_now(), lead_id)
            )
            return cur.lastrowid

    def list_leads(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                """
                SELECT l.*, a.start AS appointment_start, a.appointment_type
                FROM leads l
                LEFT JOIN appointments a ON a.lead_id = l.id AND a.status = 'booked'
                ORDER BY l.id DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reference"] = reference(d["id"])
            out.append(d)
        return out

    def to_csv(self) -> str:
        cols = ["reference", "created_at", "status", *LEAD_FIELDS, "appointment_start", "appointment_type", "notes"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(self.list_leads())
        return buf.getvalue()
