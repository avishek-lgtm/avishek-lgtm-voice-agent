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
    def __init__(self, path: str | Path, mirror=None):
        # Optional copy of every lead elsewhere (see sheets.SheetsMirror).
        self.mirror = mirror
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def save_lead(self, lead_id: int | None, fields: dict, transcript: str) -> int:
        """Create or update a lead and return its id."""
        lead_id = self._save_lead(lead_id, fields, transcript)
        self._mirror(lead_id)
        return lead_id

    def _save_lead(self, lead_id: int | None, fields: dict, transcript: str) -> int:
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
        appointment_id = self._book(lead_id, start_iso, duration, kind, max_per_slot)
        self._mirror(lead_id)
        return appointment_id

    def _book(self, lead_id: int, start_iso: str, duration: int, kind: str, max_per_slot: int) -> int:
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

    def reserve_ids_after(self, last_id: int) -> None:
        """Make new leads number above last_id (e.g. references already in Google Sheets)."""
        with self.lock, self.db:
            row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'leads'").fetchone()
            if (row["seq"] if row else 0) < last_id:
                self.db.execute("DELETE FROM sqlite_sequence WHERE name = 'leads'")
                self.db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('leads', ?)", (last_id,))

    def _mirror(self, lead_id: int) -> None:
        if self.mirror:
            self.mirror.push(self.get_lead(lead_id))

    def get_lead(self, lead_id: int) -> dict:
        return self.list_leads(lead_id)[0]

    def list_leads(self, lead_id: int | None = None) -> list[dict]:
        where = "WHERE l.id = ?" if lead_id is not None else ""
        with self.lock:
            rows = self.db.execute(
                f"""
                SELECT l.*, a.start AS appointment_start, a.appointment_type
                FROM leads l
                LEFT JOIN appointments a ON a.lead_id = l.id AND a.status = 'booked'
                {where}
                ORDER BY l.id DESC
                """,
                () if lead_id is None else (lead_id,),
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
