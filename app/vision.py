"""Google Cloud Vision OCR: the API call (with retries) and a rule-based receipt parser.

Vision only returns text and where each word sits on the page, so this module
turns that into receipt fields itself:

1. Words are regrouped into the visual rows of the receipt (using their boxes,
   so a table row "หมูสับ 3.00 100.00 300.00" stays on one line even when
   Vision reads the columns as separate blocks).
2. Fields are found by labels ("Grand Total", "รวมทั้งหมด", "Date", "Inv. No.")
   and patterns (13-digit tax IDs with a valid checksum, dates, amounts).
3. Handwritten bills have no labels: the total falls back to the largest
   number standing alone on a row. Low word confidence across the page flags
   them for review, since a misread date or amount there is likely.

Without page geometry (e.g. a text-only response) the plain text lines are used.
"""

from __future__ import annotations

import base64
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

from .normalize import (
    LineItem,
    ReceiptData,
    clean_text,
    parse_amount,
    parse_date,
    parse_number,
    thai_digits_to_ascii,
)

log = logging.getLogger(__name__)

# Base64 makes the upload a third bigger and Vision caps a request at 10 MB.
MAX_BYTES = 7 * 1024 * 1024
MAX_PDF_PAGES = 5  # files:annotate reads at most 5 pages per request
RETRY_DELAYS = (2.0, 5.0)  # two retries with backoff
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
API = "https://vision.googleapis.com/v1"
# Below this average word confidence the page is treated as handwriting.
PAGE_CONFIDENCE_FLOOR = 0.9


class OcrError(Exception):
    """OCR failed; the receipt should be kept as pending_ocr."""


class OcrAuthError(OcrError):
    """401/403: bad key, API disabled or billing off. The owner must be told."""


class VisionClient:
    def __init__(self, sa_info: dict, timeout: float = 60.0, sleep=time.sleep):
        self.sa_info = sa_info
        self.timeout = timeout
        self._sleep = sleep
        self._creds = None

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_info(self.sa_info, scopes=SCOPES)
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    def read(self, content: bytes, filename: str, mime_type: str, today: date | None = None) -> ReceiptData:
        return parse_response(self.ocr(content, filename, mime_type), today)

    def ocr(self, content: bytes, filename: str, mime_type: str) -> Any:
        encoded = base64.b64encode(content).decode()
        features = [{"type": "DOCUMENT_TEXT_DETECTION"}]
        context = {"languageHints": ["th", "en"]}
        if mime_type == "application/pdf":
            url = f"{API}/files:annotate"
            request = {"inputConfig": {"content": encoded, "mimeType": mime_type}, "features": features,
                       "imageContext": context, "pages": list(range(1, MAX_PDF_PAGES + 1))}
        else:
            url = f"{API}/images:annotate"
            request = {"image": {"content": encoded}, "features": features, "imageContext": context}

        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                resp = httpx.post(url, headers={"Authorization": f"Bearer {self._token()}"},
                                  json={"requests": [request]}, timeout=self.timeout)
            except httpx.TransportError as exc:
                error = OcrError(f"network error: {exc.__class__.__name__}")
            else:
                if resp.status_code in (401, 403):
                    raise OcrAuthError(f"Vision API returned {resp.status_code}: {_error_message(resp)}")
                if resp.status_code == 429 or resp.status_code >= 500:
                    error = OcrError(f"Vision API returned {resp.status_code}")
                elif resp.status_code >= 400:
                    raise OcrError(f"Vision API rejected the file ({resp.status_code}): {_error_message(resp)}")
                else:
                    return resp.json()
            if attempt < attempts - 1:
                log.warning("Vision attempt %d failed (%s), retrying", attempt + 1, error)
                self._sleep(RETRY_DELAYS[attempt])
        raise error


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"][:300]
    except (ValueError, KeyError, TypeError):
        return resp.text[:300]


# --- from Vision JSON to rows of text -----------------------------------------

@dataclass
class Row:
    """One visual line of the receipt; conf[i] is the OCR confidence of text[i]."""

    text: str
    conf: list[float] = field(default_factory=list)

    def confidence(self, start: int, end: int) -> float | None:
        values = [c for c in self.conf[start:end] if c is not None]
        return min(values) if values else None


@dataclass
class _Word:
    text: str
    conf: float | None
    x: float
    y: float
    height: float
    space_after: bool
    pts: list = field(default_factory=list)
    right: float = 0.0


_BREAKS = {"SPACE", "SURE_SPACE", "EOL_SURE_SPACE", "LINE_BREAK", "HYPHEN"}


def _pages(data: Any) -> list[dict]:
    """Vision's fullTextAnnotation per image, or per page of a PDF."""
    if not isinstance(data, dict) or not isinstance(data.get("responses"), list) or not data["responses"]:
        raise OcrError("unexpected Vision response shape")
    first = data["responses"][0]
    responses = first.get("responses", [first]) if isinstance(first, dict) else []
    pages = []
    for r in responses:
        if not isinstance(r, dict):
            continue
        if "error" in r:
            raise OcrError(f"Vision couldn't read the file: {r['error'].get('message', r['error'])}")
        pages.append(r.get("fullTextAnnotation") or {})
    return pages


def _words(page: dict) -> list[_Word]:
    width, height = page.get("width") or 1, page.get("height") or 1
    words = []
    for block in page.get("blocks") or []:
        for para in block.get("paragraphs") or []:
            for w in para.get("words") or []:
                symbols = w.get("symbols") or []
                text = "".join(s.get("text", "") for s in symbols)
                box = w.get("boundingBox") or {}
                if box.get("normalizedVertices"):
                    pts = [(v.get("x", 0.0), v.get("y", 0.0)) for v in box["normalizedVertices"]]
                else:
                    pts = [(v.get("x", 0) / width, v.get("y", 0) / height) for v in box.get("vertices") or []]
                if not text or len(pts) != 4:
                    continue
                brk = ((symbols[-1].get("property") or {}).get("detectedBreak") or {}).get("type")
                words.append(_Word(text, w.get("confidence"), 0, 0, 0, brk in _BREAKS, pts))
    return words


def _rows_from_page(page: dict) -> list[Row]:
    words = _words(page)
    if not words:
        return [Row(line) for line in (page.get("text") or "").splitlines()]
    # Turn the page so text runs left to right, whatever way the photo was taken:
    # each word box's first edge (vertex 0 -> 1) follows the reading direction.
    angles = sorted(math.atan2(w.pts[1][1] - w.pts[0][1], w.pts[1][0] - w.pts[0][0]) for w in words)
    angle = angles[len(angles) // 2]
    cos, sin = math.cos(-angle), math.sin(-angle)
    for w in words:
        xs, ys = zip(*((x * cos - y * sin, x * sin + y * cos) for x, y in w.pts))
        w.x, w.right, w.y, w.height = min(xs), max(xs), sum(ys) / 4, max(ys) - min(ys)

    heights = sorted(w.height for w in words)
    tolerance = heights[len(heights) // 2] * 0.6
    rows: list[list[_Word]] = []
    for w in sorted(words, key=lambda w: w.y):
        if rows and abs(w.y - sum(r.y for r in rows[-1]) / len(rows[-1])) <= tolerance:
            rows[-1].append(w)
        else:
            rows.append([w])

    out = []
    for row in rows:
        row.sort(key=lambda w: w.x)
        text, conf = "", []
        for i, w in enumerate(row):
            if i and (row[i - 1].space_after or w.x - row[i - 1].right > row[i - 1].height * 0.3):
                text += " "
                conf.append(None)
            text += w.text
            conf.extend([w.conf] * len(w.text))
        out.append(Row(text, conf))
    return out


# --- patterns -----------------------------------------------------------------

_AMOUNT = re.compile(r"(?<![\d.,/:A-Za-z])(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?(?![\d/:%A-Za-z]|\.\d|,\d{3})")
_DATE = re.compile(
    r"(?<!\d)(\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
    r"|\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*(?:\d{4}|\d{2}))(?!\d)"
    r"|(?<!\d)\d{1,2}\s*(?:ม\.?ค\.?|ก\.?พ\.?|มี\.?ค\.?|เม\.?ย\.?|พ\.?ค\.?|มิ\.?ย\.?|ก\.?ค\.?|ส\.?ค\.?|ก\.?ย\.?"
    r"|ต\.?ค\.?|พ\.?ย\.?|ธ\.?ค\.?|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม"
    r"|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(?:\d{4}|\d{2})(?!\d)",
    re.I,
)
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_PHONE = re.compile(r"(?<!\d)0\d{1,2}[- ]?\d{3}[- ]?\d{3,4}(?!\d)|(?<!\d)[-+]?\d{9,}(?!\d)")
_TAX_ID = re.compile(r"(?<!\d)\d(?:[- ]?\d){12}(?!\d)")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")

_CUSTOMER = re.compile(r"customer|ลูกค้า|ผู้ซื้อ|sold\s*to|bill\s*to|ship\s*to", re.I)
_COMPANY = re.compile(
    r"((?:บริษัท|บจก\.?|ห้างหุ้นส่วน(?:จำกัด|สามัญ)?|หจก\.?)\s*.+?(?:จำกัด(?:\s*\(มหาชน\))?|$))"
    r"|(?:^|(?<=\s))((?:ร้าน|โรงงาน|สหกรณ์)[฀-๿A-Za-z][^:\d(]*?)(?=\s{2,}|\s*[:\d(]|$)"
    r"|([A-Za-z][A-Za-z&.,' -]+?(?:co\.?,?\s*ltd\.?|company\s+limited|limited|ltd\.?))",
    re.I,
)
_DOC_TYPES = ("ใบกำกับภาษี/ใบเสร็จรับเงิน", "ใบกำกับภาษีอย่างย่อ", "ใบกำกับภาษี", "ใบส่งสินค้า/ใบเสร็จรับเงิน",
              "ใบเสร็จรับเงิน", "ใบส่งสินค้า", "บิลเงินสด", "tax invoice", "receipt")


def _label(*names: str) -> re.Pattern:
    """Labels match with or without spaces inside ("รวม ทั้งหมด", "Grand  Total")."""
    alts = [r"\s*".join(re.escape(ch) for ch in n.replace(" ", "")) for n in names]
    return re.compile("|".join(alts), re.I)


_GRAND = [_label("grand total", "ยอดสุทธิ", "ยอดรวมสุทธิ", "รวมทั้งสิ้น", "รวมเงินทั้งสิ้น", "จำนวนเงินรวมทั้งสิ้น",
                 "รวมทั้งหมด", "ยอดเงินสุทธิ", "net total", "total amount", "amount due", "ยอดชำระ")]
_SUBTOTAL = [_label("sub total", "subtotal", "รวมเงิน", "ราคารวม", "ยอดรวม", "รวมราคา")]
_VAT = [_label("total vat", "vat amount", "ภาษีมูลค่าเพิ่ม", "vat")]
_LOOSE_TOTAL = [re.compile(r"(?<![a-z])total(?!\s*vat)|รวม", re.I)]
_DATE_LABEL = re.compile(r"date|วันที่|วัน\s*ที่", re.I)
_DUE_LABEL = re.compile(r"due|ครบ\s*กำหนด|ครบ\s*ก้าหนด", re.I)
_INVOICE = [
    re.compile(r"(?:inv(?:oice)?\.?\s*no\.?|เลขที่(?:\s*ใบ\S*)?|bill\s*no\.?|receipt\s*no\.?|doc(?:ument)?\.?\s*no\.?)"
               r"\s*[:#.]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.I),
    re.compile(r"#\s*([A-Za-z]*\d[A-Za-z0-9\-]{4,})"),
    re.compile(r"(?<![a-z])no\.?\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.I),
]
_NOT_INVOICE = re.compile(r"customer|ลูกค้า|tax|ภาษี|ref|อ้างอิง|p\.?o\.?|tel|โทร|salesman|พนักงาน|pos", re.I)
_ITEM_HEADER = re.compile(r"รายการ|description|สินค้า|item|article|particular|qty|จำนวน|ราคา|price|amount|unit|หน่วย", re.I)
_NOT_ITEM = re.compile(
    r"total|รวม|ยอด|vat|ภาษี|ส่วนลด|discount|ชำระ|เงินทอน|change|cash|เงินสด|โอน|tel|โทร|เลขที่|"
    r"date|วันที่|page|หน้า|จำนวนสินค้า|pos\b|tax|ลงชื่อ|ผู้รับ|ผู้ส่ง",
    re.I,
)


def _blocked(text: str) -> list[tuple[int, int]]:
    """Spans that look like numbers but aren't amounts: dates, times, phones, IDs, percentages."""
    spans = []
    for pattern in (_DATE, _TIME, _PHONE, _TAX_ID, _PERCENT):
        spans.extend(m.span() for m in pattern.finditer(text))
    return spans


def _amounts(text: str, start: int = 0) -> list[tuple[Decimal, int, int]]:
    blocked = _blocked(text)
    out = []
    for m in _AMOUNT.finditer(text, start):
        if any(s <= m.start() < e for s, e in blocked):
            continue
        value = parse_amount(m.group(0))
        if value is not None and value < 1_000_000:
            out.append((value, m.start(), m.end()))
    return out


_BARE = re.compile(r"^\s*(?:฿|บาท)?\s*[=\-:]?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:บาท|฿|-|\.-)?\s*$")


def _labelled_amount(rows: list[Row], labels: list[re.Pattern]):
    """(amount, confidence) for the first label that has a value: on the same row after
    the label, or on one of the next rows standing alone (columns read top-down)."""
    for pattern in labels:
        for i, row in enumerate(rows):
            for m in pattern.finditer(row.text):
                same = _amounts(row.text, m.end())
                if same:
                    value, s, e = same[0]
                    return value, row.confidence(s, e)
                for nxt in rows[i + 1:i + 4]:
                    bare = _BARE.match(nxt.text)
                    if bare and _amounts(nxt.text):
                        return parse_amount(bare.group(1)), nxt.confidence(*bare.span(1))
    return None, None


def valid_tax_id(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    d = [int(c) for c in digits]
    return (11 - sum(d[i] * (13 - i) for i in range(12)) % 11) % 10 == d[12]


# --- the parser -------------------------------------------------------------------

def rows_of(data: Any) -> list[Row]:
    rows: list[Row] = []
    for annotation in _pages(data):
        pages = annotation.get("pages") or [{"text": annotation.get("text", "")}]
        for page in pages:
            page.setdefault("text", annotation.get("text", "") if len(pages) == 1 else "")
            rows.extend(_rows_from_page(page))
    for r in rows:
        r.text = thai_digits_to_ascii(r.text)
    return [r for r in rows if r.text.strip()]


def parse_response(data: Any, today: date | None = None) -> ReceiptData:
    return parse_rows(rows_of(data), today)


def parse_rows(rows: list[Row], today: date | None = None) -> ReceiptData:
    today = today or date.today()
    r = ReceiptData()
    confs: dict[str, float | None] = {}
    full = "\n".join(row.text for row in rows)

    # Document type: the title printed earliest ("สาขาที่ออกใบกำกับภาษี" names the branch, not the type).
    compact = re.sub(r"ออกใบ", "", full.lower().replace(" ", ""))
    found_types = [(compact.find(t.replace(" ", "")), t) for t in _DOC_TYPES if t.replace(" ", "") in compact]
    r.doc_type = min(found_types, key=lambda ft: (ft[0], -len(ft[1])))[1] if found_types else ""

    # Tax IDs: the first valid one is the seller's, unless its row is about the customer.
    for row in rows:
        for m in _TAX_ID.finditer(row.text):
            digits = re.sub(r"\D", "", m.group(0))
            if not valid_tax_id(digits):
                continue
            if _CUSTOMER.search(row.text) or (r.vendor_tax_id and not r.customer_tax_id):
                if digits != r.vendor_tax_id:
                    r.customer_tax_id = r.customer_tax_id or digits
            elif not r.vendor_tax_id:
                r.vendor_tax_id = digits
                confs["vendor_tax_id"] = row.confidence(*m.span())

    # Vendor: the first company-like name that isn't the customer's.
    for row in rows:
        if _CUSTOMER.search(row.text):
            continue
        m = _COMPANY.search(row.text)
        if m:
            r.vendor_name = clean_text(next(g for g in m.groups() if g))
            break
    for row in rows:
        if re.search(r"customer\s*name|ชื่อลูกค้า|นามลูกค้า", row.text, re.I):
            after = row.text.split(":", 1)[-1] if ":" in row.text else ""
            m = _COMPANY.search(after)  # the row may run on into the next column
            r.customer_name = clean_text(next(g for g in m.groups() if g) if m else after)
            break

    for row in rows:
        m = re.search(r"(?:tel|โทร|เบอร์\S*)\.?\s*:?\s*(0\d[\d\- ]{7,11}\d)", row.text, re.I)
        if m:
            r.vendor_phone = clean_text(m.group(1))
            break

    # Invoice number.
    for pattern in _INVOICE:
        for row in rows:
            for m in pattern.finditer(row.text):
                if _NOT_INVOICE.search(row.text[max(0, m.start() - 20):m.start()]) or not re.search(r"\d", m.group(1)):
                    continue
                if _DATE.fullmatch(m.group(1)):
                    continue
                r.invoice_no = m.group(1).strip("-/")
                confs["invoice_no"] = row.confidence(*m.span(1))
                break
            if r.invoice_no:
                break
        if r.invoice_no:
            break

    # Date: prefer one next to a "Date" label or a time, never a due date or a far-off one.
    best = None
    for i, row in enumerate(rows):
        for m in _DATE.finditer(row.text):
            d = parse_date(m.group(0), today)
            if not d:
                continue
            before = row.text[:m.start()]
            prev = rows[i - 1].text if i else ""
            score = 1 - i / max(len(rows), 1)
            if _DATE_LABEL.search(before) or (_DATE_LABEL.search(prev) and len(prev) < 20):
                score += 3
            if _DUE_LABEL.search(before) or (_DUE_LABEL.search(prev) and len(prev) < 30):
                score -= 4
            if _TIME.search(row.text):
                score += 2
            if re.search(r"\d{4}", m.group(0)):
                score += 1
            if d > today + timedelta(days=1) or d < today - timedelta(days=730):
                score -= 5
            if best is None or score > best[0]:
                best = (score, m.group(0), d, row.confidence(*m.span()))
    if best:
        _, r.raw_date, r.receipt_date, confs["date"] = best

    # Totals.
    r.grand_total, confs["grand_total"] = _labelled_amount(rows, _GRAND)
    r.subtotal, _ = _labelled_amount(rows, _SUBTOTAL)
    r.vat, _ = _labelled_amount(rows, _VAT)
    if r.grand_total is None:
        r.grand_total, confs["grand_total"] = _labelled_amount(rows, _LOOSE_TOTAL)
    if r.grand_total is None:
        # Handwritten bills: the total is usually the biggest number on a line of its own.
        bare = [(parse_amount(m.group(1)), row.confidence(*m.span(1))) for row in rows
                if (m := _BARE.match(row.text)) and _amounts(row.text)]
        pool = bare or [(a, row.confidence(s, e)) for row in rows for a, s, e in _amounts(row.text)]
        if pool:
            r.grand_total, confs["grand_total"] = max(pool, key=lambda p: p[0])
    if r.subtotal is not None and r.grand_total is not None and r.subtotal > r.grand_total:
        r.subtotal = None  # picked up something else, e.g. a column header's first amount

    r.items = _items(rows)

    # Confidence: the key fields, plus the page as a whole (handwriting reads poorly).
    found = {k: c for k, c in confs.items() if c is not None}
    page = [c for row in rows for c in row.conf if c is not None]
    if page and sum(page) / len(page) < PAGE_CONFIDENCE_FLOOR:
        found["handwriting"] = sum(page) / len(page)
    if found:
        r.min_confidence = round(min(found.values()), 3)
        r.low_confidence_fields = sorted(k for k, c in found.items() if c < 0.85)
    return r


def _items(rows: list[Row]) -> list[LineItem]:
    """Rows between the column header and the first totals line that end in an amount."""
    # A header row names at least two columns ("สินค้า ... ราคารวม", "Description Qty Price").
    start = next((i + 1 for i, row in enumerate(rows)
                  if len({m.group(0).lower() for m in _ITEM_HEADER.finditer(row.text)}) >= 2), 0)
    items: list[LineItem] = []
    for row in rows[start:]:
        text = row.text
        if items and re.search(r"total|รวม|ยอด", text, re.I) and not _ITEM_HEADER.search(text):
            break
        if ":" in text or _NOT_ITEM.search(text) or _CUSTOMER.search(text):  # "label : value" rows
            continue
        name_m = re.search(r"[A-Za-z฀-๿][^\d=]*[A-Za-z฀-๿.)]", text)
        if not name_m or len(re.sub(r"[^A-Za-z฀-๿]", "", name_m.group(0))) < 2:
            continue
        after = [a for a in _amounts(text) if a[1] >= name_m.end()]
        if not after:
            continue
        lead = re.findall(r"[A-Za-z]*\d[\w-]*", text[:name_m.start()])
        numbers = [a[0] for a in after]
        qty = unit = None
        if len(numbers) >= 3:
            qty, unit = numbers[0], numbers[-2] if len(numbers) == 3 else numbers[1]
        elif len(numbers) == 2:
            qty = numbers[0]
        elif lead and re.fullmatch(r"\d{1,3}(?:\.\d+)?", lead[0]):
            qty = Decimal(lead[0])  # "10 เต้าหู้ไข่ 100.00": a count before the name
        code = next((t for t in reversed(lead) if len(t) >= 4), "")
        items.append(LineItem(
            item_no=len(items) + 1,
            item_code=code,
            item_name=clean_text(name_m.group(0)),
            qty=parse_number(qty) if qty is not None else None,
            unit_cost=unit,
            total_cost=numbers[-1],
        ))
    return items
