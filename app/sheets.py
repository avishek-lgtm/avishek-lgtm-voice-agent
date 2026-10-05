"""Mirrors leads and booked site visits into a Google Sheet.

The local SQLite file stays the source of truth: it decides which slots are
free and makes booking atomic, so two callers can never get the same slot.
Every change to a lead is then copied to the sheet, one row per lead keyed by
its reference (L0001, L0002, ...), so the sales team works from Google Sheets.

Writes happen on a background thread so a slow or failing Google API never
delays or breaks a call; errors are logged and the next change retries.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HEADER = [
    "Reference",
    "Received (UTC)",
    "Updated (UTC)",
    "Status",
    "Name",
    "Phone",
    "Email",
    "Project type",
    "Site location",
    "Budget",
    "Timeline",
    "Site visit",
    "Visit type",
    "Notes",
    "Transcript",
]
MAX_CELL = 45_000  # Google Sheets limit is 50,000 characters per cell
STATUS_LABELS = {"new": "New lead", "appointment_booked": "Site visit booked"}


@dataclass
class SheetsConfig:
    enabled: bool = False
    spreadsheet_id: str = ""
    credentials_file: str = "secrets/google-service-account.json"
    worksheet: str = "Leads"


def lead_to_row(lead: dict) -> list[str]:
    visit = lead.get("appointment_start") or ""
    if visit:
        visit = visit[:16].replace("T", " ")  # company-local wall time, e.g. 2026-10-07 10:00
    return [
        lead["reference"],
        (lead.get("created_at") or "")[:19].replace("T", " "),
        (lead.get("updated_at") or "")[:19].replace("T", " "),
        STATUS_LABELS.get(lead.get("status"), lead.get("status") or ""),
        lead.get("name") or "",
        lead.get("phone") or "",
        lead.get("email") or "",
        lead.get("project_type") or "",
        lead.get("site_location") or "",
        lead.get("budget_range") or "",
        lead.get("timeline") or "",
        visit,
        (lead.get("appointment_type") or "") if visit else "",
        lead.get("notes") or "",
        (lead.get("transcript") or "")[-MAX_CELL:],
    ]


class SheetsMirror:
    """Upserts lead rows into one worksheet. Pass a worksheet object in tests."""

    def __init__(self, worksheet):
        self.ws = worksheet
        self.rows: dict[str, int] = {}  # reference -> 1-based row number
        self.queue: queue.Queue = queue.Queue()
        self._prepare()
        threading.Thread(target=self._worker, name="sheets-sync", daemon=True).start()

    @classmethod
    def connect(cls, cfg: SheetsConfig, root: Path) -> "SheetsMirror":
        import gspread

        creds = Path(cfg.credentials_file)
        if not creds.is_absolute():
            creds = root / creds
        if not creds.exists():
            raise RuntimeError(
                f"Google credentials not found at {creds}. See 'Save leads to Google Sheets' in the README."
            )
        try:
            gc = gspread.service_account(
                filename=str(creds), scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            sheet = gc.open_by_key(cfg.spreadsheet_id)
        except Exception as e:
            raise RuntimeError(
                "Cannot open the Google Sheet. Check google_sheets.spreadsheet_id and that the sheet is "
                f"shared (as Editor) with the service account's email. Details: {e}"
            ) from e
        try:
            ws = sheet.worksheet(cfg.worksheet)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=cfg.worksheet, rows=1000, cols=len(HEADER))
        log.info("Saving leads to Google Sheet %s (tab '%s')", cfg.spreadsheet_id, cfg.worksheet)
        return cls(ws)

    def _prepare(self) -> None:
        first = self.ws.row_values(1)
        if first[: len(HEADER)] != HEADER:
            self.ws.update([HEADER], "A1")
        for i, ref in enumerate(self.ws.col_values(1), start=1):
            if i > 1 and ref:
                self.rows[ref] = i

    def last_reference_number(self) -> int:
        """Highest lead number already in the sheet (L0042 -> 42)."""
        nums = [int(ref[1:]) for ref in self.rows if ref[:1] == "L" and ref[1:].isdigit()]
        return max(nums, default=0)

    def push(self, lead: dict) -> None:
        """Queue a lead for upload; returns immediately."""
        self.queue.put(lead)

    def _worker(self) -> None:
        while True:
            items = [self.queue.get()]
            while True:
                try:
                    items.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            # Several turns of the same call may be queued; only send the latest.
            latest: dict[str, dict] = {}
            waiters = []
            for item in items:
                if isinstance(item, threading.Event):
                    waiters.append(item)
                else:
                    latest[item["reference"]] = item
            for lead in latest.values():
                try:
                    self.write(lead)
                except Exception:
                    log.exception("Could not write lead %s to Google Sheets", lead["reference"])
            for w in waiters:
                w.set()

    def write(self, lead: dict) -> None:
        row = lead_to_row(lead)
        ref = lead["reference"]
        if ref in self.rows:
            self.ws.update([row], f"A{self.rows[ref]}")
        else:
            resp = self.ws.append_row(row, value_input_option="RAW", table_range="A1")
            self.rows[ref] = _row_number(resp) or len(self.ws.col_values(1))

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until queued writes are done (used in tests and on shutdown)."""
        done = threading.Event()
        self.queue.put(done)
        done.wait(timeout)


def _row_number(resp) -> int | None:
    """Row from an append response, e.g. updatedRange 'Leads!A7:O7' -> 7."""
    try:
        cell = resp["updates"]["updatedRange"].split("!")[-1].split(":")[0]
        return int("".join(ch for ch in cell if ch.isdigit()))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
