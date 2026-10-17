"""The receipt pipeline and the bot's conversation logic.

download -> dedupe (hash) -> archive in Drive -> OCR -> normalize/validate
-> dedupe (tax ID + invoice no.) -> categorize -> rename in Drive
-> write Sheets rows -> reply in LINE
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode

from linebot.v3.webhooks import (
    FileMessageContent,
    ImageMessageContent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

from .categorize import Category, Vendor, find_vendor, short_name, vendor_key
from .config import Settings
from .iapp import MAX_BYTES, MAX_PDF_PAGES, IappAuthError, IappError, parse_response
from .line_client import Button, OutMessage
from .normalize import ReceiptData, parse_amount, parse_date, validate
from .storage import drive_file_id

log = logging.getLogger(__name__)

# One receipt at a time: keeps the hash/invoice dedupe free of races.
_lock = threading.Lock()

HELP_TEXT = (
    "📸 Forward a receipt photo or PDF here and I'll archive it, read it and log it.\n\n"
    "Commands:\n"
    "• summary – this month's spend by category\n"
    "• /retry – re-run OCR on receipts that are pending\n"
    "• help – this message"
)

CATEGORY_CACHE_SECONDS = 300

STATUS_OK = "ok"
STATUS_REVIEW = "needs_review"
STATUS_PENDING = "pending_ocr"
STATUS_FLAGGED = "flagged"
STATUS_CONFIRMED = "confirmed"


def detect_type(content: bytes) -> tuple[str, str] | None:
    """(mime type, extension) from the file's magic bytes."""
    if content.startswith(b"%PDF"):
        return "application/pdf", "pdf"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if content[4:8] == b"ftyp" and content[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif"):
        return "image/heic", "heic"  # iPhone photos sent as files
    return None


def pdf_page_count(content: bytes) -> int:
    return max(1, len(re.findall(rb"/Type\s*/Page(?!s)", content)))


def fmt_baht(amount: Decimal | None) -> str:
    if amount is None:
        return "฿?"
    if amount == amount.to_integral_value():
        return f"฿{amount:,.0f}"
    return f"฿{amount:,.2f}"


def file_slug(text: str, limit: int = 40) -> str:
    """Safe for a Drive file name; keeps Thai characters."""
    text = re.sub(r'[\\/:*?"<>|\s]+', "-", text.strip())
    return text.strip("-")[:limit] or "unknown"


def postback(action: str, receipt_id: str, **extra: str) -> str:
    return urlencode({"a": action, "r": receipt_id, **extra})


@dataclass
class Outcome:
    status: str  # logged | duplicate | pending | rejected | error
    message: OutMessage
    receipt_id: str = ""
    needs_category: bool = False


@dataclass
class _Ocr:
    data: ReceiptData | None = None
    problems: list[str] = field(default_factory=list)
    error: str = ""


class Bot:
    def __init__(self, settings: Settings, line, sheets, drive, ocr, now: Callable[[], datetime] | None = None):
        self.settings = settings
        self.line = line
        self.sheets = sheets
        self.drive = drive
        self.ocr = ocr
        self._now = now or (lambda: datetime.now(settings.tz))
        self._cat_cache: tuple[float, list[Category]] | None = None

    # -- helpers ----------------------------------------------------------------

    def now(self) -> datetime:
        return self._now().replace(tzinfo=None, microsecond=0)

    def today(self) -> date:
        return self.now().date()

    def notify_owner(self, text: str) -> None:
        if not self.settings.owner_user_id:
            return
        try:
            self.line.send(self.settings.owner_user_id, OutMessage(f"⚠️ {text}"))
        except Exception:
            log.exception("could not notify owner")

    def _categories(self) -> list[Category]:
        """Read from the Categories tab, cached for a few minutes to save API calls."""
        now = time.monotonic()
        if self._cat_cache is None or now - self._cat_cache[0] > CATEGORY_CACHE_SECONDS:
            self._cat_cache = (now, self.sheets.categories())
        return self._cat_cache[1]

    def _category_buttons(self, receipt_id: str) -> tuple[Button, ...]:
        return tuple(
            Button(short_name(c), postback("cat", receipt_id, c=c.code)) for c in self._categories()[:13]
        )

    def _category_name(self, code: str) -> str:
        for c in self._categories():
            if c.code == code:
                return short_name(c)
        return code

    # -- LINE events ------------------------------------------------------------

    def handle(self, event) -> None:
        source = event.source
        user_id = getattr(source, "user_id", None) or ""
        to = getattr(source, "group_id", None) or getattr(source, "room_id", None) or user_id
        reply_token = getattr(event, "reply_token", None)

        if not isinstance(event, (MessageEvent, PostbackEvent)):
            return
        if user_id not in self.settings.allowed_user_ids:
            log.info("rejected event from non-allowlisted user")
            if to != user_id:
                return  # stay quiet in group chats
            # Telling people their own ID makes adding them to the allowlist easy.
            self.line.send(to, OutMessage(f"Sorry, you're not set up to use this bot.\nYour LINE user ID: {user_id}"), reply_token)
            return

        try:
            if isinstance(event, PostbackEvent):
                msg = self.handle_postback(event.postback.data)
            elif isinstance(event.message, (ImageMessageContent, FileMessageContent)):
                msg = self.handle_file_message(event.message, user_id, to)
            elif isinstance(event.message, TextMessageContent):
                msg = self.handle_text(event.message.text)
            else:
                msg = None  # stickers, locations, etc.
        except Exception as exc:
            log.exception("event failed")
            self.sheets.log("ERROR", f"{type(exc).__name__}: {exc}")
            self.notify_owner(f"Bot error: {type(exc).__name__}. See the Log tab.")
            msg = OutMessage("Sorry, something went wrong. The owner has been notified; please try again later.")
        if msg is not None:
            self.line.send(to, msg, reply_token)

    def handle_file_message(self, message, user_id: str, to: str) -> OutMessage:
        if isinstance(message, FileMessageContent):
            if message.file_size and message.file_size > MAX_BYTES:
                return OutMessage(f"That file is {message.file_size / 1_048_576:.1f} MB; the limit is 10 MB.")
            filename = message.file_name or f"{message.id}.pdf"
        else:
            filename = f"{message.id}.jpg"
        if user_id == to:
            self.line.show_loading(user_id)
        content = self.line.download(message.id)
        return self.process_file(content, filename, submitted_by=user_id, source="line").message

    def handle_text(self, text: str) -> OutMessage | None:
        """Only exact commands get an answer; any other chat is ignored."""
        t = text.strip().lower()
        if t in {"summary", "/summary"}:
            return self.month_summary()
        if t in {"/retry", "retry"}:
            return self.retry_pending()
        if t in {"help", "/help"}:
            return OutMessage(HELP_TEXT)
        return None

    def handle_postback(self, data: str) -> OutMessage:
        q = {k: v[0] for k, v in parse_qs(data).items()}
        action, rid = q.get("a", ""), q.get("r", "")
        receipt = self.sheets.find_receipt(rid) if rid else None
        if receipt is None:
            return OutMessage("I couldn't find that receipt any more.")

        if action == "ok":
            self.sheets.update_receipt(rid, {"review_status": STATUS_CONFIRMED})
            return OutMessage(f"👍 Confirmed {rid}.")
        if action == "flag":
            self.sheets.update_receipt(rid, {"review_status": STATUS_FLAGGED})
            return OutMessage(f"🚩 Flagged {rid} for review.")
        if action == "chcat":
            return OutMessage(
                f"Pick a category for {receipt.get('vendor_name') or rid}:",
                quick_replies=self._category_buttons(rid),
            )
        if action == "cat":
            code = q.get("c", "").upper()
            if code not in {c.code for c in self._categories()}:
                return OutMessage("Unknown category.")
            self.set_category(receipt, code)
            vendor = receipt.get("vendor_name") or "this vendor"
            return OutMessage(f"✅ {vendor} → {self._category_name(code)}. I'll remember this vendor.")
        return OutMessage(HELP_TEXT)

    def set_category(self, receipt: dict[str, str], code: str) -> None:
        rid = receipt["receipt_id"]
        self.sheets.update_receipt(rid, {"category": code})
        self.sheets.set_items_category(rid, code)
        tax_id, name = receipt.get("vendor_tax_id", ""), receipt.get("vendor_name", "")
        if not tax_id and not name:
            return
        vendor = find_vendor(self.sheets.vendors(), tax_id, name)
        if vendor is None:
            vendor = Vendor(vendor_key(tax_id, name), tax_id, name, first_seen=self.today().isoformat(), receipt_count=1)
        vendor.category = code
        self.sheets.save_vendor(vendor)

    # -- receipt processing -------------------------------------------------------

    def process_file(
        self,
        content: bytes,
        filename: str,
        submitted_by: str,
        source: str,
        received_at: datetime | None = None,
    ) -> Outcome:
        with _lock:
            return self._process_file(content, filename, submitted_by, source, received_at)

    def _process_file(self, content, filename, submitted_by, source, received_at) -> Outcome:
        received_at = received_at or self.now()
        kind = detect_type(content)
        if kind is None:
            return Outcome("rejected", OutMessage("I can only read JPG/PNG/HEIC photos and PDF files."))
        mime, ext = kind

        file_hash = hashlib.sha256(content).hexdigest()
        existing = self.sheets.hash_exists(file_hash)
        if existing:
            return Outcome("duplicate", OutMessage(f"Already logged as {existing}, skipped. ✅"), existing)

        receipt_id = f"R{received_at:%y%m%d}-{file_hash[:6]}"
        pages = pdf_page_count(content) if ext == "pdf" else 1

        # 1. Archive the original before anything else can fail.
        try:
            file_id, link = self.drive.upload(
                content, f"{received_at:%Y-%m-%d}_{receipt_id}.{ext}", mime, received_at.date()
            )
        except Exception as exc:
            log.exception("Drive upload failed")
            self.sheets.log("ERROR", f"Drive upload failed: {exc}", receipt_id, self.now())
            self.notify_owner(f"Drive upload failed for a receipt from {submitted_by}.")
            return Outcome("error", OutMessage("Sorry, I couldn't save that file. Please send it again in a minute."))

        record: dict[str, Any] = {
            "receipt_id": receipt_id,
            "received_at": received_at,
            "submitted_by": submitted_by,
            "pages": pages,
            "file_link": link,
            "file_hash": file_hash,
            "source": source,
        }

        # 2. Check the OCR limits, then OCR.
        limit_problem = ""
        if len(content) > MAX_BYTES:
            limit_problem = f"file is {len(content) / 1_048_576:.1f} MB (OCR limit 10 MB)"
        elif pages > MAX_PDF_PAGES:
            limit_problem = f"PDF has {pages} pages (OCR limit {MAX_PDF_PAGES})"
        if limit_problem:
            record.update(review_status=STATUS_REVIEW, review_note=f"{limit_problem}; enter manually")
            self.sheets.add_receipt(record, [])
            return Outcome(
                "pending", OutMessage(f"📁 Saved to Drive as {receipt_id}, but I can't read it: {limit_problem}."), receipt_id
            )

        # Dates are judged against when the receipt arrived (matters for backfill).
        result = self._run_ocr(content, filename, mime, receipt_id, received_at.date())
        if result.data is None:
            record.update(review_status=STATUS_PENDING, review_note=result.error)
            self.sheets.add_receipt(record, [])
            return Outcome(
                "pending",
                OutMessage(f"📁 Saved to Drive as {receipt_id}. OCR is unavailable right now; send /retry later."),
                receipt_id,
            )

        r = result.data
        dup = self.sheets.invoice_exists(r.vendor_tax_id, r.invoice_no)
        if dup:
            self._safe(lambda: self.drive.trash(file_id), "trash duplicate", receipt_id)
            return Outcome(
                "duplicate", OutMessage(f"Invoice {r.invoice_no} from {r.vendor_name or r.vendor_tax_id} is already logged as {dup}, skipped. ✅"), dup
            )

        return self._finish(record, r, result.problems, file_id, ext, self.sheets.add_receipt)

    def _run_ocr(self, content: bytes, filename: str, mime: str, receipt_id: str, as_of: date) -> _Ocr:
        try:
            raw = self.ocr.ocr(content, filename, mime)
            data = parse_response(raw, as_of)
        except IappAuthError as exc:
            self.sheets.log("ERROR", str(exc), receipt_id, self.now())
            self.notify_owner(f"iApp OCR: {exc}. Receipts are being saved as pending_ocr; top up, then send /retry.")
            return _Ocr(error=str(exc))
        except IappError as exc:
            self.sheets.log("WARN", f"OCR failed: {exc}", receipt_id, self.now())
            return _Ocr(error=str(exc))
        return _Ocr(data=data, problems=validate(data, as_of))

    def _finish(self, record, r: ReceiptData, problems: list[str], file_id: str, ext: str, write) -> Outcome:
        """Categorize, rename in Drive, write the rows and build the reply.

        `write(record, items)` either appends a new row or, for /retry,
        fills in an existing pending_ocr row."""
        receipt_id = record["receipt_id"]
        vendor = find_vendor(self.sheets.vendors(), r.vendor_tax_id, r.vendor_name)
        category = vendor.category if vendor else ""
        if vendor:
            vendor.receipt_count += 1
            if r.vendor_name and r.vendor_name != vendor.name and r.vendor_name not in vendor.aliases:
                vendor.aliases.append(r.vendor_name)
            self._safe(lambda: self.sheets.save_vendor(vendor), "update vendor", receipt_id)

        received = record["received_at"].date()
        # A misread date (e.g. year 2099) shouldn't create a stray folder.
        plausible = r.receipt_date and timedelta(0) <= received - r.receipt_date <= timedelta(days=366)
        folder_date = r.receipt_date if plausible else received
        name = f"{folder_date.isoformat()}_{file_slug(r.vendor_name or r.vendor_tax_id)}_{r.grand_total or 0}.{ext}"
        self._safe(lambda: self.drive.rename_and_move(file_id, name, folder_date), "rename in Drive", receipt_id)

        record.update(
            receipt_date=r.receipt_date,
            doc_type=r.doc_type,
            invoice_book=r.invoice_book,
            invoice_no=r.invoice_no,
            po_no=r.po_no,
            vendor_name=r.vendor_name,
            vendor_tax_id=r.vendor_tax_id,
            vendor_phone=r.vendor_phone,
            vendor_address=r.vendor_address,
            customer_name=r.customer_name,
            customer_tax_id=r.customer_tax_id,
            category=category,
            subtotal=r.subtotal,
            discount=r.discount,
            after_discount=r.after_discount,
            vat=r.vat,
            grand_total=r.grand_total,
            net_before_vat=r.net_before_vat,
            payment_method=r.payment_method,
            min_confidence=r.min_confidence,
            review_status=STATUS_REVIEW if problems else STATUS_OK,
            review_note="; ".join(problems),
        )
        items = [
            {
                "receipt_id": receipt_id, "item_no": i.item_no, "item_code": i.item_code,
                "item_name": i.item_name, "qty": i.qty, "unit_cost": i.unit_cost,
                "total_cost": i.total_cost, "category": category,
            }
            for i in r.items
        ]
        try:
            write(record, items)
        except Exception as exc:
            log.exception("Sheets write failed")
            self.sheets.log("ERROR", f"Sheets write failed: {exc}", receipt_id, self.now())
            self.notify_owner(f"Couldn't write {receipt_id} to the Sheet; the file is safe in Drive.")
            return Outcome(
                "error", OutMessage(f"📁 Saved to Drive as {receipt_id}, but I couldn't update the Sheet. The owner has been told."), receipt_id
            )
        return Outcome("logged", self._summary_message(record, problems), receipt_id, needs_category=not category)

    def _summary_message(self, record: dict[str, Any], problems: list[str]) -> OutMessage:
        rid = record["receipt_id"]
        d = record.get("receipt_date")
        parts = [
            fmt_baht(record.get("grand_total")),
            record.get("vendor_name") or record.get("vendor_tax_id") or "Unknown vendor",
            self._category_name(record["category"]) if record.get("category") else "❓ category",
            f"{d:%d/%m}" if d else "no date",
        ]
        line = " · ".join(parts) + (" ⚠️" if problems else " ✅")
        if not record.get("category"):
            line += "\nNew vendor: pick a category below."
        if problems:
            line += "\nCheck: " + "; ".join(problems)
        buttons = (
            Button("OK", postback("ok", rid)),
            Button("Change category", postback("chcat", rid)),
            Button("Flag", postback("flag", rid)),
        )
        if not record.get("category"):
            return OutMessage(line, buttons=buttons, quick_replies=self._category_buttons(rid))
        return OutMessage(line, buttons=buttons)

    # -- commands -----------------------------------------------------------------

    def month_summary(self) -> OutMessage:
        today = self.today()
        totals: dict[str, Decimal] = {}
        count = 0
        for r in self.sheets.receipts():
            d = parse_date(r.get("receipt_date"), today)
            amount = parse_amount(r.get("grand_total"))
            if not d or amount is None or (d.year, d.month) != (today.year, today.month):
                continue
            code = r.get("category") or "?"
            totals[code] = totals.get(code, Decimal(0)) + amount
            count += 1
        if not count:
            return OutMessage(f"No receipts logged for {today:%m/%Y} yet.")
        lines = [f"📊 {today:%m/%Y} so far ({count} receipts)"]
        for code, amount in sorted(totals.items(), key=lambda kv: -kv[1]):
            name = self._category_name(code) if code != "?" else "Uncategorized"
            lines.append(f"• {name}: {fmt_baht(amount)}")
        lines.append(f"Total: {fmt_baht(sum(totals.values()))}")
        return OutMessage("\n".join(lines))

    def retry_pending(self) -> OutMessage:
        pending = [r for r in self.sheets.receipts() if r.get("review_status") == STATUS_PENDING]
        if not pending:
            return OutMessage("Nothing is pending OCR. 👍")
        done = failed = 0
        for row in pending:
            with _lock:
                ok = self._retry_one(row)
            done, failed = done + ok, failed + (not ok)
        return OutMessage(f"Retried {len(pending)}: {done} logged, {failed} still pending.")

    def _retry_one(self, row: dict[str, str]) -> bool:
        rid = row["receipt_id"]
        file_id = drive_file_id(row.get("file_link", ""))
        if not file_id:
            return False
        try:
            content, filename, mime = self.drive.download(file_id)
        except Exception as exc:
            self.sheets.log("ERROR", f"retry download failed: {exc}", rid, self.now())
            return False
        received = parse_date(row.get("received_at")) or self.today()
        result = self._run_ocr(content, filename, mime, rid, received)
        if result.data is None:
            self.sheets.update_receipt(rid, {"review_note": result.error})
            return False
        r = result.data
        dup = self.sheets.invoice_exists(r.vendor_tax_id, r.invoice_no, exclude_id=rid)
        if dup:
            result.problems.append(f"possible duplicate of {dup}")
        kind = detect_type(content)
        ext = kind[1] if kind else filename.rsplit(".", 1)[-1]
        record = {"receipt_id": rid, "received_at": datetime.combine(received, datetime.min.time())}

        def write(rec, items):
            fields = {k: v for k, v in rec.items() if k not in {"receipt_id", "received_at"}}
            self.sheets.update_receipt(rid, fields)
            self.sheets.add_items(items)

        return self._finish(record, r, result.problems, file_id, ext, write).status == "logged"

    def _safe(self, fn: Callable[[], Any], what: str, receipt_id: str) -> None:
        """Non-critical step: log and notify, but keep going."""
        try:
            fn()
        except Exception as exc:
            log.exception("%s failed", what)
            self.sheets.log("ERROR", f"{what} failed: {exc}", receipt_id, self.now())
