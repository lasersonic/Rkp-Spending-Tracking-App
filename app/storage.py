"""Google Sheets (data) and Google Drive (original files).

Sheets is accessed with the service account. Drive uses the owner's OAuth
token when GOOGLE_DRIVE_TOKEN_JSON is set: on a personal Google account a
service account has no storage quota, so it cannot upload into My Drive.
Without that token the service account is used, which works for Shared Drives.
"""

from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from .categorize import Category, DEFAULT_CATEGORIES, Vendor, normalize_vendor_name

log = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

RECEIPT_COLUMNS = [
    "receipt_id", "received_at", "receipt_date", "invoice_no", "vendor_name", "category",
    "subtotal", "discount", "after_discount", "vat", "grand_total", "net_before_vat", "payment_method", "pages", "file_link", "file_hash",
    "min_confidence", "review_status", "review_note", "source",
]
LINE_ITEM_COLUMNS = [
    "receipt_id", "item_no", "item_code", "item_name", "qty", "unit_cost",
    "total_cost", "category",
]
VENDOR_COLUMNS = [
    "vendor_key", "tax_id", "name", "aliases", "category", "first_seen", "receipt_count",
]
CATEGORY_COLUMNS = ["code", "name", "group"]
LOG_COLUMNS = ["timestamp", "level", "receipt_id", "message"]

TABS = {
    "Receipts": RECEIPT_COLUMNS,
    "LineItems": LINE_ITEM_COLUMNS,
    "Vendors": VENDOR_COLUMNS,
    "Categories": CATEGORY_COLUMNS,
    "Log": LOG_COLUMNS,
}

# Written as real dates so Sheets formulas can group by month.
_DATE_COLUMNS = {"receipt_date", "received_at", "first_seen", "timestamp"}

T = TypeVar("T")


def with_retry(fn: Callable[[], T], what: str, attempts: int = 3, sleep=time.sleep) -> T:
    """Retry a Google API call on transient errors."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # gspread.APIError, HttpError, transport errors
            status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(
                getattr(exc, "resp", None), "status", None
            )
            if status is not None:
                transient = int(status) in (408, 429) or int(status) >= 500
            else:  # network trouble (requests/socket errors are OSErrors)
                transient = isinstance(exc, OSError)
            if not transient or attempt == attempts - 1:
                raise
            log.warning("%s failed (%s), retrying", what, exc)
            sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def to_cell(column: str, value: Any) -> Any:
    """Convert a Python value for USER_ENTERED writes.

    Text gets a leading apostrophe so Sheets keeps it as text: tax IDs and
    invoice numbers keep their leading zeros, and OCR text can never be
    evaluated as a formula.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if not text or column in _DATE_COLUMNS:
        return text
    return "'" + text


def col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def drive_file_id(link: str) -> str | None:
    m = re.search(r"/d/([\w-]+)", link or "") or re.search(r"[?&]id=([\w-]+)", link or "")
    return m.group(1) if m else None


class SheetStore:
    """All reads and writes against the spreadsheet."""

    def __init__(self, spreadsheet):
        self.ss = spreadsheet
        self._ws: dict[str, Any] = {}

    @classmethod
    def from_service_account(cls, sa_info: dict, sheet_id: str) -> "SheetStore":
        import gspread

        gc = gspread.service_account_from_dict(sa_info, scopes=SHEETS_SCOPES)
        return cls(gc.open_by_key(sheet_id))

    def ws(self, name: str):
        if name not in self._ws:
            self._ws[name] = with_retry(lambda: self.ss.worksheet(name), f"open {name}")
        return self._ws[name]

    # -- schema -------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create missing tabs with headers and seed the Categories tab."""
        existing = {w.title for w in self.ss.worksheets()}
        for name, columns in TABS.items():
            if name not in existing:
                ws = self.ss.add_worksheet(name, rows=1000, cols=max(len(columns), 10))
                ws.update([columns], "A1", value_input_option="RAW")
                ws.freeze(rows=1)
                # Fixed formats so reads come back as yyyy-mm-dd whatever the locale.
                for c in _DATE_COLUMNS & set(columns):
                    letter = col_letter(columns.index(c))
                    pattern = "yyyy-mm-dd" if c in {"receipt_date", "first_seen"} else "yyyy-mm-dd hh:mm:ss"
                    ws.format(f"{letter}2:{letter}", {"numberFormat": {"type": "DATE_TIME", "pattern": pattern}})
                if name == "Categories":
                    ws.append_rows(
                        [[c.code, c.name, c.group] for c in DEFAULT_CATEGORIES],
                        value_input_option="RAW",
                    )
            else:
                ws = self.ss.worksheet(name)
                header = ws.row_values(1)
                if header != columns:
                    if header and header != columns[: len(header)]:
                        raise RuntimeError(f"Tab {name} has unexpected headers: {header}")
                    ws.update([columns], "A1", value_input_option="RAW")
        if "Dashboard" not in existing:
            from .dashboard import write_dashboard

            write_dashboard(self.ss.add_worksheet("Dashboard", rows=200, cols=40))

    # -- generic helpers ------------------------------------------------------

    def _records(self, tab: str) -> list[dict[str, str]]:
        rows = with_retry(lambda: self.ws(tab).get_all_values(), f"read {tab}")
        if not rows:
            return []
        header = rows[0]
        return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]

    def _append(self, tab: str, columns: list[str], records: list[dict[str, Any]]) -> None:
        if not records:
            return
        values = [[to_cell(c, rec.get(c)) for c in columns] for rec in records]
        with_retry(
            lambda: self.ws(tab).append_rows(
                values, value_input_option="USER_ENTERED", insert_data_option="INSERT_ROWS",
                table_range="A1",
            ),
            f"append {tab}",
        )

    def _find_row(self, tab: str, column: str, value: str) -> int | None:
        """1-based sheet row number of the first match in `column`."""
        columns = TABS[tab]
        col = columns.index(column) + 1
        values = with_retry(lambda: self.ws(tab).col_values(col), f"read {tab}")
        for i, v in enumerate(values[1:], start=2):
            if v == value:
                return i
        return None

    def _update_fields(self, tab: str, row: int, fields: dict[str, Any]) -> None:
        columns = TABS[tab]
        data = [
            {"range": f"{col_letter(columns.index(k))}{row}", "values": [[to_cell(k, v)]]}
            for k, v in fields.items()
        ]
        # gspread rewrites each "range" in place, so pass fresh copies on retry.
        with_retry(
            lambda: self.ws(tab).batch_update([dict(d) for d in data], value_input_option="USER_ENTERED"),
            f"update {tab}",
        )

    # -- receipts -------------------------------------------------------------

    def receipts(self) -> list[dict[str, str]]:
        return self._records("Receipts")

    def find_receipt(self, receipt_id: str) -> dict[str, str] | None:
        return next((r for r in self.receipts() if r["receipt_id"] == receipt_id), None)

    def hash_exists(self, file_hash: str) -> str | None:
        """Receipt ID already holding this file, if any."""
        for r in self.receipts():
            if r.get("file_hash") == file_hash:
                return r["receipt_id"]
        return None

    def invoice_exists(self, vendor_name: str, invoice_no: str, exclude_id: str = "") -> str | None:
        key = normalize_vendor_name(vendor_name)
        if not key or not invoice_no:
            return None
        for r in self.receipts():
            if (
                r.get("invoice_no") == invoice_no
                and normalize_vendor_name(r.get("vendor_name", "")) == key
                and r["receipt_id"] != exclude_id
            ):
                return r["receipt_id"]
        return None

    def add_receipt(self, receipt: dict[str, Any], items: list[dict[str, Any]]) -> None:
        self._append("Receipts", RECEIPT_COLUMNS, [receipt])
        self._append("LineItems", LINE_ITEM_COLUMNS, items)

    def update_receipt(self, receipt_id: str, fields: dict[str, Any]) -> bool:
        row = self._find_row("Receipts", "receipt_id", receipt_id)
        if row is None:
            return False
        self._update_fields("Receipts", row, fields)
        return True

    def add_items(self, items: list[dict[str, Any]]) -> None:
        """Used by /retry: pending_ocr receipts have no items yet."""
        self._append("LineItems", LINE_ITEM_COLUMNS, items)

    def set_items_category(self, receipt_id: str, category: str) -> None:
        ws = self.ws("LineItems")
        ids = with_retry(lambda: ws.col_values(1), "read LineItems")
        col = col_letter(LINE_ITEM_COLUMNS.index("category"))
        data = [
            {"range": f"{col}{i}", "values": [[category]]}
            for i, v in enumerate(ids[1:], start=2)
            if v == receipt_id
        ]
        if data:
            with_retry(lambda: ws.batch_update([dict(d) for d in data], value_input_option="RAW"), "update LineItems")

    # -- vendors & categories -----------------------------------------------

    def categories(self) -> list[Category]:
        cats = [
            Category(r["code"].strip().upper(), r["name"].strip(), r["group"].strip())
            for r in self._records("Categories")
            if r.get("code", "").strip()
        ]
        return cats or list(DEFAULT_CATEGORIES)

    def vendors(self) -> list[Vendor]:
        out = []
        for r in self._records("Vendors"):
            if not r.get("vendor_key"):
                continue
            out.append(
                Vendor(
                    vendor_key=r["vendor_key"],
                    tax_id=r.get("tax_id", ""),
                    name=r.get("name", ""),
                    aliases=[a.strip() for a in r.get("aliases", "").split("|") if a.strip()],
                    category=r.get("category", "").strip().upper(),
                    first_seen=r.get("first_seen", ""),
                    receipt_count=int(r.get("receipt_count") or 0),
                )
            )
        return out

    def save_vendor(self, v: Vendor) -> None:
        record = {
            "vendor_key": v.vendor_key, "tax_id": v.tax_id, "name": v.name,
            "aliases": "|".join(v.aliases), "category": v.category,
            "first_seen": v.first_seen, "receipt_count": v.receipt_count,
        }
        row = self._find_row("Vendors", "vendor_key", v.vendor_key)
        if row is None:
            self._append("Vendors", VENDOR_COLUMNS, [record])
        else:
            self._update_fields("Vendors", row, {k: x for k, x in record.items() if k != "vendor_key"})

    def log(self, level: str, message: str, receipt_id: str = "", now: datetime | None = None) -> None:
        """Best effort: logging must never break processing."""
        try:
            self._append(
                "Log", LOG_COLUMNS,
                [{"timestamp": now or datetime.now(), "level": level, "receipt_id": receipt_id,
                  "message": message[:1000]}],
            )
        except Exception:
            log.exception("could not write to Log tab")


class DriveStore:
    """Originals archived under <root>/YYYY/MM/."""

    def __init__(self, service, root_folder_id: str):
        self.svc = service
        self.root = root_folder_id
        self._folders: dict[tuple[str, str], str] = {}

    @classmethod
    def from_settings(cls, sa_info: dict | None, user_token: dict | None, root_folder_id: str) -> "DriveStore":
        from googleapiclient.discovery import build

        if user_token:
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_info(user_token, scopes=DRIVE_SCOPES)
        else:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                sa_info, scopes=["https://www.googleapis.com/auth/drive"]
            )
        return cls(build("drive", "v3", credentials=creds, cache_discovery=False), root_folder_id)

    def _child_folder(self, parent: str, name: str) -> str:
        q = (
            f"'{parent}' in parents and name = '{name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        found = with_retry(
            lambda: self.svc.files().list(
                q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute(),
            "find Drive folder",
        )["files"]
        if found:
            return found[0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}
        return with_retry(
            lambda: self.svc.files().create(body=meta, fields="id", supportsAllDrives=True).execute(),
            "create Drive folder",
        )["id"]

    def month_folder(self, d: date) -> str:
        key = (f"{d.year:04d}", f"{d.month:02d}")
        if key not in self._folders:
            year = self._child_folder(self.root, key[0])
            self._folders[key] = self._child_folder(year, key[1])
        return self._folders[key]

    def upload(self, content: bytes, name: str, mime_type: str, folder_date: date) -> tuple[str, str]:
        """Return (file_id, web link)."""
        from googleapiclient.http import MediaIoBaseUpload

        meta = {"name": name, "parents": [self.month_folder(folder_date)]}

        def go():
            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
            return self.svc.files().create(
                body=meta, media_body=media, fields="id,webViewLink", supportsAllDrives=True
            ).execute()

        f = with_retry(go, "upload to Drive")
        return f["id"], f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"

    def rename_and_move(self, file_id: str, name: str, folder_date: date) -> None:
        current = with_retry(
            lambda: self.svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute(),
            "read Drive file",
        )
        target = self.month_folder(folder_date)
        old = ",".join(p for p in current.get("parents", []) if p != target)
        with_retry(
            lambda: self.svc.files().update(
                fileId=file_id, body={"name": name}, addParents=target, removeParents=old or None,
                fields="id", supportsAllDrives=True,
            ).execute(),
            "rename Drive file",
        )

    def name(self, file_id: str) -> str:
        meta = with_retry(
            lambda: self.svc.files().get(fileId=file_id, fields="name", supportsAllDrives=True).execute(),
            "read Drive file",
        )
        return meta.get("name", "")

    def trash(self, file_id: str) -> None:
        with_retry(
            lambda: self.svc.files().update(
                fileId=file_id, body={"trashed": True}, supportsAllDrives=True
            ).execute(),
            "trash Drive file",
        )

    def download(self, file_id: str) -> tuple[bytes, str, str]:
        """Return (content, name, mime type)."""
        meta = with_retry(
            lambda: self.svc.files().get(fileId=file_id, fields="name,mimeType", supportsAllDrives=True).execute(),
            "read Drive file",
        )
        content = with_retry(
            lambda: self.svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute(),
            "download Drive file",
        )
        return content, meta["name"], meta["mimeType"]
