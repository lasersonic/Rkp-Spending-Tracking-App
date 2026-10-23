"""The receipt pipeline and the bot's conversation logic.

In LINE (no OCR):
download -> dedupe (hash) -> archive in Drive -> Sheets row (incomplete)
-> ask the supplier (Suppliers tab chips, or typed) -> ask the price
-> save, rename in Drive -> reply with the result and Change price /
Change category / Change supplier buttons

The one-time Telegram backfill has no one to ask, so a Bot built with an
`ocr` client reads each file instead:
download -> dedupe -> archive -> OCR -> validate -> dedupe (invoice no.)
-> categorize -> rename in Drive -> Sheets rows
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

from .categorize import Category, Vendor, find_vendor, normalize_vendor_name, short_name, vendor_key
from .config import Settings
from .vision import MAX_BYTES, MAX_PDF_PAGES, OcrAuthError, OcrError
from .line_client import Button, OutMessage
from .normalize import ReceiptData, parse_amount, parse_date, validate
from .storage import drive_file_id

log = logging.getLogger(__name__)

# One receipt at a time: keeps the hash/invoice dedupe free of races.
_lock = threading.Lock()

HELP_TEXT = (
    "📸 Send a receipt photo or PDF here. I'll save it to Drive, then ask for the "
    "supplier and the price.\n\n"
    "Commands:\n"
    "• daily – receipts logged today\n"
    "• summary – this month's spend by category\n"
    "• help – this message"
)

CATEGORY_CACHE_SECONDS = 300
# How long the bot treats your next message as the answer it asked for.
WAIT_SECONDS = 1800
MAX_SUPPLIER_CHIPS = 12  # LINE allows 13 quick replies; the last is "Type name"
DAILY_LIST_LIMIT = 30  # keeps the "daily" reply well under LINE's 5,000-character limit

STATUS_OK = "ok"
STATUS_REVIEW = "needs_review"
STATUS_PENDING = "pending_ocr"
STATUS_FLAGGED = "flagged"
STATUS_CONFIRMED = "confirmed"
STATUS_INCOMPLETE = "incomplete"  # archived; supplier or price not given yet

ASK_SUPPLIER, TYPE_SUPPLIER, ASK_PRICE = "supplier", "supplier_text", "price"


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


def supplier_ref(name: str) -> str:
    """Short, stable id for a supplier in postback data (Thai names get long once URL-encoded)."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


_PRICE_RE = re.compile(r"^(?:฿|thb)?\s*([\d๐-๙][\d๐-๙,]*(?:\.\d{1,2})?)\s*(?:บาท|baht|thb|฿)?$", re.IGNORECASE)


def parse_typed_price(text: str) -> Decimal | None:
    """'1250', '1,250.50', '฿1250', '๑๒๕๐ บาท' -> Decimal. Anything else (dates, words) -> None."""
    m = _PRICE_RE.match(text.strip())
    amount = parse_amount(m.group(1)) if m else None
    return amount if amount is not None and amount > 0 else None


def vendor_label(name: str) -> str:
    """LINE chip labels max 20 chars: drop the "ร้าน" (shop) prefix before cutting."""
    if len(name) > 20 and name.startswith("ร้าน"):
        name = name[len("ร้าน"):].strip()
    return name[:20]


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
    def __init__(self, settings: Settings, line, sheets, drive, ocr=None, now: Callable[[], datetime] | None = None):
        self.settings = settings
        self.line = line
        self.sheets = sheets
        self.drive = drive
        self.ocr = ocr  # only the backfill passes one; in LINE people type the details
        self._now = now or (lambda: datetime.now(settings.tz))
        self._cat_cache: tuple[float, list[Category]] | None = None
        # (user, chat) -> (receipt_id, step, deadline): what the bot asked that person.
        # In memory: fine with one instance; after a restart the buttons still work.
        self._waits: dict[tuple[str, str], tuple[str, str, float]] = {}

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

    def _supplier_buttons(self, receipt_id: str) -> tuple[Button, ...]:
        """The fixed list on the Suppliers tab, in sheet order, then "Type name"."""
        chips = [
            Button(vendor_label(name), postback("ven", receipt_id, v=supplier_ref(name)))
            for name in self.sheets.suppliers()[:MAX_SUPPLIER_CHIPS]
        ]
        return (*chips, Button("✏️ Type name", postback("venin", receipt_id)))

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
                msg = self.handle_postback(event.postback.data, user_id, to)
            elif isinstance(event.message, (ImageMessageContent, FileMessageContent)):
                msg = self.handle_file_message(event.message, user_id, to)
            elif isinstance(event.message, TextMessageContent):
                msg = self.handle_text(event.message.text, user_id, to)
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
            if self.ocr and message.file_size and message.file_size > MAX_BYTES:
                return OutMessage(f"That file is {message.file_size / 1_048_576:.1f} MB; the limit is {MAX_BYTES // 1_048_576} MB.")
            filename = message.file_name or f"{message.id}.pdf"
        else:
            filename = f"{message.id}.jpg"
        if user_id == to:
            self.line.show_loading(user_id)
        content = self.line.download(message.id)
        outcome = self.process_file(content, filename, submitted_by=user_id, source="line")
        if outcome.status == "waiting":
            self._wait(user_id, to, outcome.receipt_id, ASK_SUPPLIER)
        return outcome.message

    def handle_text(self, text: str, user_id: str = "", to: str = "") -> OutMessage | None:
        """Exact commands, and the answer to what the bot just asked (supplier name,
        price); any other chat is ignored."""
        t = text.strip().lower()
        if t in {"daily", "/daily"}:
            return self.daily_list()
        if t in {"summary", "/summary"}:
            return self.month_summary()
        if t in {"/retry", "retry"} and self.ocr:
            return self.retry_pending()
        if t in {"help", "/help"}:
            return OutMessage(HELP_TEXT)
        waiting = self._waiting(user_id, to)
        if not waiting:
            return None
        rid, step = waiting
        if t in {"cancel", "ยกเลิก"}:
            self._waits.pop((user_id, to), None)
            return OutMessage("OK, stopped. Tap a button on the receipt when you're ready.")
        if step == ASK_PRICE:
            return self.handle_price_reply(rid, text, user_id, to)
        # A typed supplier name counts after "Type name", or straight away in a 1:1
        # chat; in a group, ordinary chat right after a photo mustn't become a name.
        if step == TYPE_SUPPLIER or (step == ASK_SUPPLIER and to == user_id):
            return self.handle_supplier_reply(rid, text, user_id, to)
        return None

    # -- what the bot asked: the next message from the same person in the same chat

    def _wait(self, user_id: str, to: str, receipt_id: str, step: str) -> None:
        self._waits[(user_id, to)] = (receipt_id, step, time.monotonic() + WAIT_SECONDS)

    def _waiting(self, user_id: str, to: str) -> tuple[str, str] | None:
        entry = self._waits.get((user_id, to))
        if entry and entry[2] > time.monotonic():
            return entry[0], entry[1]
        self._waits.pop((user_id, to), None)
        return None

    def ask_supplier(self, receipt_id: str, intro: str) -> OutMessage:
        if not self.sheets.suppliers():
            return OutMessage(f"{intro}\nType the supplier name.",
                              quick_replies=(Button("✏️ Type name", postback("venin", receipt_id)),))
        return OutMessage(f"{intro}\nWhich supplier? Pick one, or tap ✏️ to type the name.",
                          quick_replies=self._supplier_buttons(receipt_id))

    def ask_price(self, receipt: dict[str, str], user_id: str, to: str) -> OutMessage:
        self._wait(user_id, to, receipt["receipt_id"], ASK_PRICE)
        current = parse_amount(receipt.get("grand_total"))
        vendor = receipt.get("vendor_name") or receipt["receipt_id"]
        if current is None:
            return OutMessage(f'Type the total price for {vendor}, e.g. 1250.\nSend "cancel" to stop.')
        return OutMessage(f'Type the correct total for {vendor} (now {fmt_baht(current)}).\nSend "cancel" to stop.')

    def handle_price_reply(self, rid: str, text: str, user_id: str, to: str) -> OutMessage:
        amount = parse_typed_price(text)
        if amount is None:
            return OutMessage('That isn\'t a price. Type a number like 1250, or "cancel".')
        self._waits.pop((user_id, to), None)
        receipt = self.sheets.find_receipt(rid)
        if receipt is None:
            return OutMessage("I couldn't find that receipt any more.")
        return self.set_price(receipt, amount)

    def handle_supplier_reply(self, rid: str, text: str, user_id: str, to: str) -> OutMessage:
        name = re.sub(r"\s+", " ", text).strip()
        if not 2 <= len(name) <= 80 or parse_typed_price(name) is not None:
            return OutMessage('Type the supplier\'s name (e.g. "ร้านป้าแดง"), or "cancel".')
        receipt = self.sheets.find_receipt(rid)
        if receipt is None:
            self._waits.pop((user_id, to), None)
            return OutMessage("I couldn't find that receipt any more.")
        return self.set_vendor_name(receipt, name, user_id, to)

    def set_price(self, receipt: dict[str, str], amount: Decimal) -> OutMessage:
        rid = receipt["receipt_id"]
        old = parse_amount(receipt.get("grand_total"))
        vat = parse_amount(receipt.get("vat")) or Decimal(0)
        # Warnings about the total no longer apply once a person has typed it.
        problems = [
            p for p in receipt.get("review_note", "").split("; ")
            if p and not p.startswith(("missing grand total", "VAT ", "total edited by hand", "waiting for"))
        ]
        if not receipt.get("vendor_name"):
            problems.append("waiting for supplier")
        fields: dict[str, Any] = {
            "grand_total": amount,
            "net_before_vat": amount - vat,
            # Keep a trail when a total that was already there is changed.
            "review_note": "; ".join(problems + ([f"total edited by hand (was {fmt_baht(old)})"]
                                                 if old is not None and old != amount else [])),
        }
        if receipt.get("review_status") in (STATUS_REVIEW, STATUS_INCOMPLETE) and not problems:
            fields["review_status"] = STATUS_OK
        self.sheets.update_receipt(rid, fields)
        receipt = {**receipt, **{k: str(v) for k, v in fields.items()}}
        self._rename_in_drive(receipt, receipt.get("vendor_name") or "unknown")
        return self.result_message(receipt)

    def result_message(self, receipt: dict[str, str]) -> OutMessage:
        """The saved receipt with buttons to revise it, plus category chips if it has none."""
        rid = receipt["receipt_id"]
        today = self.today()
        d = parse_date(receipt.get("receipt_date"), today) or parse_date(receipt.get("received_at"), today)
        category = receipt.get("category", "")
        parts = [
            fmt_baht(parse_amount(receipt.get("grand_total"))),
            receipt.get("vendor_name") or "Unknown supplier",
            self._category_name(category) if category else "❓ category",
            f"{d:%d/%m}" if d else "no date",
        ]
        text = "Saved: " + " · ".join(parts) + (" ✅" if category else "")
        buttons = (
            Button("Change price", postback("price", rid)),
            Button("Change category", postback("chcat", rid)),
            Button("Change supplier", postback("chven", rid)),
        )
        if not category:
            return OutMessage(text + "\nNew supplier: pick a category below.", buttons=buttons,
                              quick_replies=self._category_buttons(rid))
        return OutMessage(text, buttons=buttons)

    def handle_postback(self, data: str, user_id: str = "", to: str = "") -> OutMessage:
        q = {k: v[0] for k, v in parse_qs(data).items()}
        action, rid = q.get("a", ""), q.get("r", "")
        receipt = self.sheets.find_receipt(rid) if rid else None
        if receipt is None:
            return OutMessage("I couldn't find that receipt any more.")

        # "ok" and "flag" are no longer offered; kept so buttons on old messages still work.
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
            return self.result_message({**receipt, "category": code})
        if action == "chven":
            self._wait(user_id, to, rid, ASK_SUPPLIER)
            return self.ask_supplier(rid, f"Change the supplier for {receipt.get('vendor_name') or rid}.")
        if action == "venin":
            self._wait(user_id, to, rid, TYPE_SUPPLIER)
            return OutMessage('Type the supplier name.\nSend "cancel" to stop.')
        if action == "ven":
            return self.set_vendor(receipt, q.get("v", ""), user_id, to)
        if action == "price":
            return self.ask_price(receipt, user_id, to)
        return OutMessage(HELP_TEXT)

    def set_vendor(self, receipt: dict[str, str], ref: str, user_id: str = "", to: str = "") -> OutMessage:
        """The user picked a supplier chip from the Suppliers tab."""
        name = next((n for n in self.sheets.suppliers() if supplier_ref(n) == ref), None)
        if name is None:
            return OutMessage("That supplier isn't on the Suppliers tab any more.")
        return self.set_vendor_name(receipt, name, user_id, to)

    def set_vendor_name(self, receipt: dict[str, str], name: str, user_id: str = "", to: str = "") -> OutMessage:
        """Save the supplier (picked or typed), then ask for the price if there is none yet."""
        rid = receipt["receipt_id"]
        vendors = self.sheets.vendors()
        known = find_vendor(vendors, "", name)
        if known:
            name = known.name  # a typed "makro" is saved as the vendor's usual name
        vendor = known or Vendor(vendor_key("", name), "", name, first_seen=self.today().isoformat())
        old_name = receipt.get("vendor_name", "")
        if old_name and normalize_vendor_name(old_name) != normalize_vendor_name(name):
            wrong = find_vendor(vendors, "", old_name)
            # An OCR misreading is remembered so the next receipt like it matches by
            # itself; a supplier a person picked by mistake is just a different shop.
            if receipt.get("source") != "line" and old_name not in vendor.aliases:
                vendor.aliases.append(old_name)
            if wrong and wrong.vendor_key != vendor.vendor_key:
                if not wrong.category and wrong.receipt_count <= 1:
                    self.sheets.delete_vendor(wrong.vendor_key)  # created from this misreading
                else:
                    wrong.receipt_count = max(0, wrong.receipt_count - 1)
                    self.sheets.save_vendor(wrong)

        has_price = parse_amount(receipt.get("grand_total")) is not None
        problems = [p for p in receipt.get("review_note", "").split("; ")
                    if p and p != "missing vendor" and not p.startswith("waiting for")]
        if not has_price:
            problems.append("waiting for price")
        fields: dict[str, Any] = {"vendor_name": name, "review_note": "; ".join(problems)}
        if receipt.get("review_status") in (STATUS_REVIEW, STATUS_INCOMPLETE) and not problems:
            fields["review_status"] = STATUS_OK
        if vendor.category:
            fields["category"] = vendor.category
            self.sheets.set_items_category(rid, vendor.category)
        self.sheets.update_receipt(rid, fields)
        vendor.receipt_count += 1
        self.sheets.save_vendor(vendor)
        receipt = {**receipt, **fields}
        self._rename_in_drive(receipt, name)

        if not has_price:
            return OutMessage(f"Supplier: {name}\n" + self.ask_price(receipt, user_id, to).text)
        self._waits.pop((user_id, to), None)
        return self.result_message(receipt)

    def _rename_in_drive(self, receipt: dict[str, str], vendor_name: str) -> None:
        """Same name pattern as _finish, now that the vendor is known."""
        file_id = drive_file_id(receipt.get("file_link", ""))
        if not file_id:
            return
        today = self.today()
        folder_date = parse_date(receipt.get("receipt_date"), today) or parse_date(receipt.get("received_at"), today) or today
        total = parse_amount(receipt.get("grand_total")) or 0

        def rename() -> None:
            ext = self.drive.name(file_id).rsplit(".", 1)[-1]
            name = f"{folder_date.isoformat()}_{file_slug(vendor_name)}_{total}.{ext}"
            self.drive.rename_and_move(file_id, name, folder_date)

        self._safe(rename, "rename in Drive", receipt["receipt_id"])

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
            "pages": pages,
            "file_link": link,
            "file_hash": file_hash,
            "source": source,
        }

        if self.ocr is None:
            # 2. No OCR: log the row now; the supplier and price come from the person.
            record.update(receipt_date=received_at.date(), review_status=STATUS_INCOMPLETE,
                          review_note="waiting for supplier and price")
            try:
                self.sheets.add_receipt(record, [])
            except Exception as exc:
                log.exception("Sheets write failed")
                self.sheets.log("ERROR", f"Sheets write failed: {exc}", receipt_id, self.now())
                self.notify_owner(f"Couldn't write {receipt_id} to the Sheet; the file is safe in Drive.")
                return Outcome(
                    "error", OutMessage(f"📁 Saved to Drive as {receipt_id}, but I couldn't update the Sheet. The owner has been told."), receipt_id
                )
            return Outcome("waiting", self.ask_supplier(receipt_id, f"📁 Saved to Drive ({receipt_id})."), receipt_id)

        # 2. Check the OCR limits, then OCR.
        limit_problem = ""
        if len(content) > MAX_BYTES:
            limit_problem = f"file is {len(content) / 1_048_576:.1f} MB (OCR limit {MAX_BYTES // 1_048_576} MB)"
        elif pages > MAX_PDF_PAGES:
            limit_problem = f"PDF has {pages} pages (OCR limit {MAX_PDF_PAGES})"
        elif mime == "image/heic":
            limit_problem = "HEIC files can't be read by the OCR; send it as a normal photo instead"
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
        dup = self.sheets.invoice_exists(r.vendor_name, r.invoice_no)
        if dup:
            self._safe(lambda: self.drive.trash(file_id), "trash duplicate", receipt_id)
            return Outcome(
                "duplicate", OutMessage(f"Invoice {r.invoice_no} from {r.vendor_name or r.vendor_tax_id} is already logged as {dup}, skipped. ✅"), dup
            )

        return self._finish(record, r, result.problems, file_id, ext, self.sheets.add_receipt)

    def _run_ocr(self, content: bytes, filename: str, mime: str, receipt_id: str, as_of: date) -> _Ocr:
        try:
            data = self.ocr.read(content, filename, mime, as_of)
        except OcrAuthError as exc:
            self.sheets.log("ERROR", str(exc), receipt_id, self.now())
            self.notify_owner(
                f"Google Vision OCR: {exc}. Receipts are being saved as pending_ocr; "
                "check the Vision API and billing in Google Cloud, then send /retry."
            )
            return _Ocr(error=str(exc))
        except OcrError as exc:
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
            if r.vendor_tax_id and not vendor.tax_id:
                vendor.tax_id = r.vendor_tax_id
            self._safe(lambda: self.sheets.save_vendor(vendor), "update vendor", receipt_id)
        elif r.vendor_name or r.vendor_tax_id:
            # Save new vendors now, while the tax ID is at hand: the Receipts tab
            # doesn't keep it, so the category button can only find them by name.
            new = Vendor(vendor_key(r.vendor_tax_id, r.vendor_name), r.vendor_tax_id, r.vendor_name,
                         first_seen=self.today().isoformat(), receipt_count=1)
            self._safe(lambda: self.sheets.save_vendor(new), "save vendor", receipt_id)

        received = record["received_at"].date()
        # A misread date (e.g. year 2099) shouldn't create a stray folder.
        plausible = r.receipt_date and timedelta(0) <= received - r.receipt_date <= timedelta(days=366)
        folder_date = r.receipt_date if plausible else received
        name = f"{folder_date.isoformat()}_{file_slug(r.vendor_name or r.vendor_tax_id)}_{r.grand_total or 0}.{ext}"
        self._safe(lambda: self.drive.rename_and_move(file_id, name, folder_date), "rename in Drive", receipt_id)

        record.update(
            receipt_date=r.receipt_date,
            invoice_no=r.invoice_no,
            vendor_name=r.vendor_name,
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
            record.get("vendor_name") or "Unknown vendor",
            self._category_name(record["category"]) if record.get("category") else "❓ category",
            f"{d:%d/%m}" if d else "no date",
        ]
        line = " · ".join(parts) + (" ⚠️" if problems else " ✅")
        vendor_chips = () if record.get("vendor_name") else self._supplier_buttons(rid)
        if vendor_chips:
            line += "\nWhich shop is this? Pick below."
        elif not record.get("category"):
            line += "\nNew vendor: pick a category below."
        if problems:
            line += "\nCheck: " + "; ".join(problems)
        buttons = (
            Button("Change category", postback("chcat", rid)),
            Button("Change supplier", postback("chven", rid)),
            Button("Change price", postback("price", rid)),
        )
        if vendor_chips:
            return OutMessage(line, buttons=buttons, quick_replies=vendor_chips)
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

    def daily_list(self) -> OutMessage:
        """Receipts the bot logged today (by when they arrived, not the date printed on them)."""
        today = self.today()
        rows = [r for r in self.sheets.receipts() if parse_date(r.get("received_at"), today) == today]
        if not rows:
            return OutMessage(f"No receipts logged today ({today:%d/%m}) yet.")
        icons = {STATUS_OK: "✅", STATUS_CONFIRMED: "✅", STATUS_REVIEW: "⚠️",
                 STATUS_FLAGGED: "🚩", STATUS_PENDING: "⏳", STATUS_INCOMPLETE: "⏳"}
        entries = []
        for r in rows:
            status = r.get("review_status", "")
            if status == STATUS_PENDING:
                entries.append(f"⏳ {r.get('receipt_id', '')} · waiting for OCR")
                continue
            if status == STATUS_INCOMPLETE and not r.get("vendor_name"):
                entries.append(f"⏳ {r.get('receipt_id', '')} · waiting for supplier and price")
                continue
            category = self._category_name(r["category"]) if r.get("category") else "❓ category"
            parts = [fmt_baht(parse_amount(r.get("grand_total"))), r.get("vendor_name") or "Unknown vendor", category]
            entries.append(" · ".join(parts) + f" {icons.get(status, '')}".rstrip())
        lines = [f"🧾 Today {today:%d/%m} ({len(rows)} receipt{'s' if len(rows) != 1 else ''})"]
        lines += [f"• {e}" for e in entries[: DAILY_LIST_LIMIT]]
        if len(entries) > DAILY_LIST_LIMIT:
            lines.append(f"…and {len(entries) - DAILY_LIST_LIMIT} more (see the Sheet)")
        total = sum((a for r in rows if (a := parse_amount(r.get("grand_total"))) is not None), Decimal(0))
        lines.append(f"Total: {fmt_baht(total)}")
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
        dup = self.sheets.invoice_exists(r.vendor_name, r.invoice_no, exclude_id=rid)
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
