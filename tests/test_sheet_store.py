"""SheetStore against a small in-memory imitation of gspread."""

import re
from datetime import datetime
from decimal import Decimal

from app.categorize import Vendor
from app.storage import RECEIPT_COLUMNS, SheetStore


def _rc(a1: str) -> tuple[int, int]:
    m = re.fullmatch(r"(?:.*!)?([A-Z]+)(\d+)", a1)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


def _entered(v):
    """What USER_ENTERED leaves in the cell, as get_all_values shows it."""
    if isinstance(v, str) and v.startswith("'"):
        return v[1:]
    return str(v)


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.grid: list[list[str]] = []

    def _set(self, row, col, value):
        while len(self.grid) < row:
            self.grid.append([])
        r = self.grid[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def get_all_values(self, **render_options):
        width = max((len(r) for r in self.grid), default=0)
        return [r + [""] * (width - len(r)) for r in self.grid]

    def row_values(self, row):
        return list(self.grid[row - 1]) if row <= len(self.grid) else []

    def col_values(self, col):
        return [r[col - 1] if len(r) >= col else "" for r in self.grid]

    def update(self, values, range_name="A1", value_input_option=None):
        row, col = _rc(range_name)
        for i, vals in enumerate(values):
            for j, v in enumerate(vals):
                self._set(row + i, col + j, _entered(v))

    def append_rows(self, values, value_input_option=None, insert_data_option=None, table_range=None):
        for vals in values:
            self.grid.append([_entered(v) for v in vals])

    def batch_update(self, data, value_input_option=None):
        for d in data:
            d["range"] = f"'{self.title}'!{d['range']}"  # gspread mutates ranges
            self.update(d["values"], d["range"])

    def freeze(self, rows=None):
        pass

    def format(self, ranges, fmt):
        pass


class FakeSpreadsheet:
    def __init__(self):
        self.tabs = {"Sheet1": FakeWorksheet("Sheet1")}

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, name):
        return self.tabs[name]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]


def make_store():
    ss = FakeSpreadsheet()
    store = SheetStore(ss)
    store.ensure_schema()
    return store, ss


def test_ensure_schema_creates_tabs_and_is_idempotent():
    store, ss = make_store()
    assert {"Receipts", "LineItems", "Vendors", "Categories", "Log", "Dashboard"} <= set(ss.tabs)
    assert ss.tabs["Receipts"].row_values(1) == RECEIPT_COLUMNS
    assert ss.tabs["Dashboard"].grid[1][0].startswith("=QUERY(")
    assert [c.code for c in store.categories()][:3] == ["FOOD", "BEV", "PACK"]
    store.ensure_schema()
    assert len(ss.tabs["Categories"].grid) == 12


def test_receipt_round_trip():
    store, ss = make_store()
    store.add_receipt(
        {"receipt_id": "R1", "received_at": datetime(2026, 10, 17, 9, 0), "vendor_name": "Makro",
         "invoice_no": "00123", "grand_total": Decimal("1240.00"), "file_hash": "abc", "category": ""},
        [{"receipt_id": "R1", "item_no": 1, "item_name": "หมู", "total_cost": Decimal("600")},
         {"receipt_id": "R1", "item_no": 2, "item_name": "ผัก", "total_cost": Decimal("640")}],
    )
    assert store.hash_exists("abc") == "R1"
    assert store.hash_exists("zzz") is None
    assert store.invoice_exists(" makro ", "00123") == "R1"  # leading zeros kept
    assert store.invoice_exists("Makro", "00123", exclude_id="R1") is None

    assert store.update_receipt("R1", {"category": "FOOD", "review_status": "confirmed"})
    store.set_items_category("R1", "FOOD")
    r = store.find_receipt("R1")
    assert (r["category"], r["review_status"], r["grand_total"]) == ("FOOD", "confirmed", "1240.0")
    assert [row[-1] for row in ss.tabs["LineItems"].grid[1:]] == ["FOOD", "FOOD"]
    assert not store.update_receipt("missing", {"category": "X"})


def test_vendor_save_and_update():
    store, _ = make_store()
    v = Vendor("tax:1", "1", "Makro", aliases=["SIAM MAKRO"], category="FOOD", first_seen="2026-10-17", receipt_count=1)
    store.save_vendor(v)
    v.receipt_count = 2
    store.save_vendor(v)
    [loaded] = store.vendors()
    assert loaded.receipt_count == 2 and loaded.aliases == ["SIAM MAKRO"] and loaded.category == "FOOD"


def test_log_never_raises():
    store, ss = make_store()
    store.log("ERROR", "boom", "R1", datetime(2026, 10, 17))
    assert ss.tabs["Log"].grid[1][1:] == ["ERROR", "R1", "boom"]
    del ss.tabs["Log"]
    store._ws.pop("Log")
    store.log("ERROR", "still fine")


def test_reads_raw_dates_whatever_the_sheet_locale():
    """A Sheet in en_US shows 10/23/2026; the store asks for raw serial numbers instead."""
    from app.storage import from_cell

    class RawWorksheet:
        def get_all_values(self, **render_options):
            assert render_options == {"value_render_option": "UNFORMATTED_VALUE",
                                      "date_time_render_option": "SERIAL_NUMBER"}
            return [["receipt_id", "received_at", "receipt_date", "grand_total", "pages", "invoice_no"],
                    ["R1", 46318.4266435185, 46318, 1250.5, 1, "0012345"],
                    ["R2", "typed by hand", "", 180, 2.0]]

    store = SheetStore(None)
    store._ws["Receipts"] = RawWorksheet()
    assert store.receipts() == [
        {"receipt_id": "R1", "received_at": "2026-10-23 10:14:22", "receipt_date": "2026-10-23",
         "grand_total": "1250.5", "pages": "1", "invoice_no": "0012345"},
        {"receipt_id": "R2", "received_at": "typed by hand", "receipt_date": "", "grand_total": "180",
         "pages": "2", "invoice_no": ""},
    ]
    assert from_cell("first_seen", 46312) == "2026-10-17" and from_cell("x", True) == "TRUE"
