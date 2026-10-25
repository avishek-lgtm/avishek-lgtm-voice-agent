"""Google Sheets mirror, against an in-memory stand-in for a gspread worksheet."""

from app.leads import LeadStore
from app.sheets import HEADER, SheetsMirror


class FakeWorksheet:
    def __init__(self, rows=None):
        self.rows = [list(r) for r in rows or []]

    def row_values(self, n):
        return self.rows[n - 1] if n <= len(self.rows) else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def update(self, values, range_name):
        row = int(range_name[1:]) - 1
        while len(self.rows) <= row:
            self.rows.append([])
        self.rows[row] = list(values[0])

    def append_row(self, values, value_input_option="RAW", table_range=None):
        self.rows.append(list(values))
        n = len(self.rows)
        return {"updates": {"updatedRange": f"Leads!A{n}:O{n}"}}


def test_header_written_and_leads_upserted(tmp_path):
    ws = FakeWorksheet()
    mirror = SheetsMirror(ws)
    store = LeadStore(tmp_path / "leads.db", mirror=mirror)

    lead_id = store.save_lead(None, {"name": "Ravi Kumar", "phone": "+919830012345"}, "Caller: hi")
    store.save_lead(lead_id, {"name": "Ravi Kumar", "phone": "+919830012345", "site_location": "New Town"}, "Caller: hi")
    store.book(lead_id, "2026-10-07T10:00+05:30", 60, "free site visit", 1)
    other = store.save_lead(None, {"name": "Asha"}, "")
    mirror.flush()

    assert ws.rows[0] == HEADER
    assert len(ws.rows) == 3  # header + one row per lead, no duplicates
    ravi = dict(zip(HEADER, ws.rows[1]))
    assert ravi["Reference"] == "L0001"
    assert ravi["Phone"] == "+919830012345"
    assert ravi["Site location"] == "New Town"
    assert ravi["Status"] == "Site visit booked"
    assert ravi["Site visit"] == "2026-10-07 10:00"
    assert ws.rows[2][0] == f"L{other:04d}"


def test_fresh_database_does_not_overwrite_sheet_rows(tmp_path):
    ws = FakeWorksheet([HEADER, ["L0001", "old lead"], ["L0002", "old lead"]])
    mirror = SheetsMirror(ws)
    store = LeadStore(tmp_path / "leads.db", mirror=mirror)
    store.reserve_ids_after(mirror.last_reference_number())
    store.save_lead(None, {"name": "Ravi"}, "")
    mirror.flush()
    assert ws.rows[1][1] == ws.rows[2][1] == "old lead"
    assert ws.rows[3][0] == "L0003" and ws.rows[3][4] == "Ravi"


def test_sheet_errors_do_not_break_saving(tmp_path):
    class Broken(FakeWorksheet):
        def append_row(self, *a, **k):
            raise RuntimeError("quota exceeded")

    mirror = SheetsMirror(Broken())
    store = LeadStore(tmp_path / "leads.db", mirror=mirror)
    store.save_lead(None, {"name": "Ravi"}, "")
    mirror.flush()
    assert store.list_leads()[0]["name"] == "Ravi"
