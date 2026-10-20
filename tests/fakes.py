"""In-memory stand-ins for LINE, Google Vision, Sheets and Drive."""

from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal

from app.categorize import DEFAULT_CATEGORIES, Vendor, normalize_vendor_name
from app.normalize import LineItem, ReceiptData, parse_amount
from app.storage import RECEIPT_COLUMNS, to_cell


def _as_sheet_text(column: str, value) -> str:
    """Mimic what get_all_values returns after a USER_ENTERED write."""
    cell = to_cell(column, value)
    if isinstance(cell, str):
        return cell[1:] if cell.startswith("'") else cell
    if isinstance(cell, float):
        return f"{cell:,.2f}"
    return str(cell)


class FakeSheets:
    def __init__(self):
        self.rows: list[dict[str, str]] = []
        self.items: list[dict] = []
        self.vendor_rows: dict[str, Vendor] = {}
        self.supplier_names: list[str] = []
        self.logs: list[tuple[str, str, str]] = []

    def receipts(self):
        return [dict(r) for r in self.rows]

    def find_receipt(self, rid):
        return next((dict(r) for r in self.rows if r["receipt_id"] == rid), None)

    def hash_exists(self, h):
        return next((r["receipt_id"] for r in self.rows if r["file_hash"] == h), None)

    def invoice_exists(self, vendor_name, invoice_no, exclude_id=""):
        key = normalize_vendor_name(vendor_name)
        if not key or not invoice_no:
            return None
        return next(
            (r["receipt_id"] for r in self.rows
             if normalize_vendor_name(r["vendor_name"]) == key and r["invoice_no"] == invoice_no
             and r["receipt_id"] != exclude_id),
            None,
        )

    def add_receipt(self, record, items):
        self.rows.append({c: _as_sheet_text(c, record.get(c)) for c in RECEIPT_COLUMNS})
        self.add_items(items)

    def add_items(self, items):
        self.items.extend(dict(i) for i in items)

    def update_receipt(self, rid, fields):
        for r in self.rows:
            if r["receipt_id"] == rid:
                r.update({k: _as_sheet_text(k, v) for k, v in fields.items()})
                return True
        return False

    def set_items_category(self, rid, code):
        for i in self.items:
            if i["receipt_id"] == rid:
                i["category"] = code

    def categories(self):
        return list(DEFAULT_CATEGORIES)

    def vendors(self):
        return [Vendor(**{**v.__dict__, "aliases": list(v.aliases)}) for v in self.vendor_rows.values()]

    def save_vendor(self, v):
        self.vendor_rows[v.vendor_key] = v

    def delete_vendor(self, key):
        self.vendor_rows.pop(key, None)

    def suppliers(self):
        return list(self.supplier_names)

    def log(self, level, message, receipt_id="", now=None):
        self.logs.append((level, receipt_id, message))


class FakeDrive:
    def __init__(self, fail=False):
        self.files: dict[str, dict] = {}
        self.fail = fail

    def upload(self, content, name, mime, folder_date: date):
        if self.fail:
            raise RuntimeError("drive down")
        fid = f"f{len(self.files) + 1}"
        self.files[fid] = {"content": content, "name": name, "mime": mime,
                           "folder": f"{folder_date:%Y/%m}", "trashed": False}
        return fid, f"https://drive.google.com/file/d/{fid}/view"

    def rename_and_move(self, fid, name, folder_date):
        self.files[fid].update(name=name, folder=f"{folder_date:%Y/%m}")

    def name(self, fid):
        return self.files[fid]["name"]

    def trash(self, fid):
        self.files[fid]["trashed"] = True

    def download(self, fid):
        f = self.files[fid]
        return f["content"], f["name"], f["mime"]


class FakeLine:
    def __init__(self, content: bytes = b""):
        self.content = content
        self.sent: list[tuple[str, object, str | None]] = []

    def download(self, message_id):
        return self.content

    def show_loading(self, user_id, seconds=30):
        pass

    def send(self, to, msg, reply_token=None):
        self.sent.append((to, msg, reply_token))


class FakeOcr:
    """Stands in for VisionClient: returns a ready-parsed receipt."""

    def __init__(self, response: ReceiptData | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def read(self, content, filename, mime, today=None):
        self.calls += 1
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)


def receipt(**overrides) -> ReceiptData:
    """A parsed Makro tax invoice. Amount overrides may be strings ("" means not found)."""
    r = ReceiptData(
        doc_type="ใบกำกับภาษี/ใบเสร็จรับเงิน",
        invoice_no="INV0012345",
        raw_date="15/10/2569",
        receipt_date=date(2026, 10, 15),
        vendor_name="บริษัท สยามแม็คโคร จำกัด (มหาชน)",
        vendor_tax_id="0107537000521",
        vendor_phone="02-067-8999",
        customer_name="ร้านอาหาร RKP",
        subtotal=Decimal("1240.00"),
        vat=Decimal("81.12"),
        grand_total=Decimal("1240.00"),
        items=[
            LineItem(1, item_name="หมูสามชั้น", qty=Decimal("2"), unit_cost=Decimal("300.00"), total_cost=Decimal("600.00")),
            LineItem(2, item_name="ผักกาดขาว", qty=Decimal("4"), unit_cost=Decimal("160.00"), total_cost=Decimal("640.00")),
        ],
        min_confidence=0.93,
    )
    for key, value in overrides.items():
        if key in {"subtotal", "discount", "after_discount", "vat", "grand_total"} and isinstance(value, str):
            value = parse_amount(value)
        setattr(r, key, value)
    return r


JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
NOW = datetime(2026, 10, 17, 14, 30, 0)
TOTAL = Decimal("1240.00")
