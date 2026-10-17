"""In-memory stand-ins for LINE, iApp, Sheets and Drive."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.categorize import DEFAULT_CATEGORIES, Vendor
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
        self.logs: list[tuple[str, str, str]] = []

    def receipts(self):
        return [dict(r) for r in self.rows]

    def find_receipt(self, rid):
        return next((dict(r) for r in self.rows if r["receipt_id"] == rid), None)

    def hash_exists(self, h):
        return next((r["receipt_id"] for r in self.rows if r["file_hash"] == h), None)

    def invoice_exists(self, tax_id, invoice_no, exclude_id=""):
        if not tax_id or not invoice_no:
            return None
        return next(
            (r["receipt_id"] for r in self.rows
             if r["vendor_tax_id"] == tax_id and r["invoice_no"] == invoice_no and r["receipt_id"] != exclude_id),
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
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def ocr(self, content, filename, mime):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def iapp_response(**overrides):
    """An iApp v3 response in the documented layout, with {value, confidence} fields."""
    fields = {
        "invoiceType": "ใบกำกับภาษี/ใบเสร็จรับเงิน",
        "invoiceBook": "",
        "invoiceID": "INV0012345",
        "invoiceDate": "15/10/2569",
        "purchaseOrderID": "",
        "issuerName": "บริษัท สยามแม็คโคร จำกัด (มหาชน)",
        "issuerTaxID": "0107537000521",
        "issuerPhone": "02-067-8999",
        "issuerAddress": "1468 ถนนพัฒนาการ กรุงเทพฯ",
        "customerName": "ร้านอาหาร RKP",
        "customerTaxID": "",
        "totalCost": "1,240.00",
        "discount": "0.00",
        "totalCostAfterDiscount": "1,240.00",
        "vat": "81.12",
        "grandTotal": "1,240.00",
    }
    fields.update(overrides)
    data = {k: {"value": v, "confidence": 0.97} for k, v in fields.items()}
    data["items"] = [
        {"itemName": {"value": "หมูสามชั้น", "confidence": 0.95}, "itemQuantity": "2",
         "itemUnitCost": "300.00", "itemTotalCost": "600.00"},
        {"itemName": {"value": "ผักกาดขาว", "confidence": 0.93}, "itemQuantity": "4",
         "itemUnitCost": "160.00", "itemTotalCost": "640.00"},
    ]
    # Layout from iApp's docs: fields under "processed", OCR extras under "raw".
    return {"message": "success", "processed": data, "raw": {}}


JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
NOW = datetime(2026, 10, 17, 14, 30, 0)
TOTAL = Decimal("1240.00")
