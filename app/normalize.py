"""Turn raw OCR text into clean values, then sanity-check the receipt.

- Buddhist-era (BE) dates -> ISO Christian-era (CE) dates
- Amount strings -> Decimal with 2 places
- Validation rules that decide whether a receipt needs human review
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

BE_OFFSET = 543
CONFIDENCE_THRESHOLD = 0.85

_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# Abbreviated and full Thai month names, plus English, -> month number.
_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("ม.ค.", "มกราคม", "jan", "january"),
        ("ก.พ.", "กุมภาพันธ์", "feb", "february"),
        ("มี.ค.", "มีนาคม", "mar", "march"),
        ("เม.ย.", "เมษายน", "apr", "april"),
        ("พ.ค.", "พฤษภาคม", "may"),
        ("มิ.ย.", "มิถุนายน", "jun", "june"),
        ("ก.ค.", "กรกฎาคม", "jul", "july"),
        ("ส.ค.", "สิงหาคม", "aug", "august"),
        ("ก.ย.", "กันยายน", "sep", "sept", "september"),
        ("ต.ค.", "ตุลาคม", "oct", "october"),
        ("พ.ย.", "พฤศจิกายน", "nov", "november"),
        ("ธ.ค.", "ธันวาคม", "dec", "december"),
    ],
    start=1,
):
    for _n in _names:
        _MONTHS[_n] = _i
        _MONTHS[_n.replace(".", "")] = _i


def thai_digits_to_ascii(text: str) -> str:
    return text.translate(_THAI_DIGITS)


def _resolve_year(raw: str, today: date) -> int:
    """Return a CE year from a 2- or 4-digit year that may be BE or CE."""
    y = int(raw)
    if len(raw) == 4:
        return y - BE_OFFSET if y > 2400 else y
    # Two digits are ambiguous ("66" = BE 2566 = 2023, "26" = CE 2026).
    # Pick whichever reading lands closest to today without being far in the future.
    candidates = [2000 + y, 2500 + y - BE_OFFSET]
    candidates = [c for c in candidates if c <= today.year + 1] or candidates
    return min(candidates, key=lambda c: abs(today.year - c))


def parse_date(value: object, today: date | None = None) -> date | None:
    """Parse receipt dates such as '27/11/2566', '27-11-66', '2566-11-27',
    '27 พ.ย. 2566' or '27 November 2023'. Returns None if it can't be parsed."""
    if value is None:
        return None
    today = today or date.today()
    text = thai_digits_to_ascii(str(value)).strip()
    if not text:
        return None

    try:
        # Year first: 2566-11-27 or 2023/11/27
        m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
        if m:
            return date(_resolve_year(m.group(1), today), int(m.group(2)), int(m.group(3)))

        # Day first (Thai convention): 27/11/2566, 27-11-66, 27.11.2023
        m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\b", text)
        if m:
            return date(_resolve_year(m.group(3), today), int(m.group(2)), int(m.group(1)))

        # Month as a word: 27 พ.ย. 2566, 27 พฤศจิกายน 66, 27 Nov 2023
        m = re.search(r"(\d{1,2})\s*([^\d\s,]+)\s*,?\s*(\d{2}|\d{4})\b", text)
        if m:
            month = _MONTHS.get(m.group(2).lower()) or _MONTHS.get(m.group(2).lower().replace(".", ""))
            if month:
                return date(_resolve_year(m.group(3), today), month, int(m.group(1)))
    except ValueError:  # e.g. 31/02/2566
        return None
    return None


_TWO_PLACES = Decimal("0.01")


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = thai_digits_to_ascii(str(value)).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d.\-]", "", text)
    if text in {"", "-", ".", "-."}:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def parse_amount(value: object) -> Decimal | None:
    """'1,240.50' / '฿1,240.5' / '1240 บาท' / 1240.5 -> Decimal('1240.50')."""
    amount = _to_decimal(value)
    return None if amount is None else amount.quantize(_TWO_PLACES, ROUND_HALF_UP)


def parse_number(value: object) -> Decimal | None:
    """Quantities keep up to 3 decimals (e.g. 1.525 kg)."""
    amount = _to_decimal(value)
    if amount is None:
        return None
    n = amount.quantize(Decimal("0.001"), ROUND_HALF_UP).normalize()
    return n.quantize(Decimal(1)) if n == n.to_integral_value() else n  # 10, not 1E+1


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_tax_id(value: object) -> str:
    """Thai tax IDs are 13 digits; keep digits only so lookups match."""
    return re.sub(r"\D", "", thai_digits_to_ascii(clean_text(value)))


@dataclass
class LineItem:
    item_no: int
    item_code: str = ""
    item_name: str = ""
    qty: Decimal | None = None
    unit_cost: Decimal | None = None
    total_cost: Decimal | None = None


@dataclass
class ReceiptData:
    """Normalized OCR output for one receipt."""

    doc_type: str = ""
    invoice_book: str = ""
    invoice_no: str = ""
    po_no: str = ""
    receipt_date: date | None = None
    raw_date: str = ""
    vendor_name: str = ""
    vendor_tax_id: str = ""
    vendor_phone: str = ""
    vendor_address: str = ""
    customer_name: str = ""
    customer_tax_id: str = ""
    subtotal: Decimal | None = None
    discount: Decimal | None = None
    after_discount: Decimal | None = None
    vat: Decimal | None = None
    grand_total: Decimal | None = None
    payment_method: str = ""
    items: list[LineItem] = field(default_factory=list)
    min_confidence: float | None = None
    low_confidence_fields: list[str] = field(default_factory=list)

    @property
    def net_before_vat(self) -> Decimal | None:
        if self.grand_total is None:
            return None
        return self.grand_total - (self.vat or Decimal("0"))


def _close(a: Decimal, b: Decimal, tolerance: str) -> bool:
    return abs(a - b) <= Decimal(tolerance)


def validate(r: ReceiptData, today: date) -> list[str]:
    """Return human-readable problems. An empty list means the receipt looks right."""
    problems: list[str] = []

    if r.receipt_date is None:
        problems.append("missing date" if not r.raw_date else f"unreadable date '{r.raw_date}'")
    else:
        if r.receipt_date > today:
            problems.append(f"date {r.receipt_date.isoformat()} is in the future")
        elif r.receipt_date < today - timedelta(days=365):
            problems.append(f"date {r.receipt_date.isoformat()} is over 12 months old")

    if r.grand_total is None:
        problems.append("missing grand total")
    if not r.vendor_name and not r.vendor_tax_id:
        problems.append("missing vendor")

    item_totals = [i.total_cost for i in r.items if i.total_cost is not None]
    if item_totals and r.subtotal is not None and not _close(sum(item_totals), r.subtotal, "1"):
        problems.append(f"line items sum {sum(item_totals)} ≠ subtotal {r.subtotal}")

    if r.subtotal is not None and r.discount is not None and r.after_discount is not None:
        if not _close(r.subtotal - r.discount, r.after_discount, "0.5"):
            problems.append(
                f"subtotal − discount ({r.subtotal - r.discount}) ≠ after discount {r.after_discount}"
            )

    # Only check VAT when the receipt actually charges it; many market
    # vendors issue plain receipts with no VAT at all.
    if r.vat and r.grand_total is not None:
        inclusive = r.grand_total * Decimal(7) / Decimal(107)
        net = r.after_discount if r.after_discount is not None else r.subtotal
        on_net = net * Decimal("0.07") if net is not None else None
        if not _close(r.vat, inclusive, "0.5") and not (on_net is not None and _close(r.vat, on_net, "0.5")):
            problems.append(f"VAT {r.vat} isn't 7% of the total")

    if r.min_confidence is not None and r.min_confidence < CONFIDENCE_THRESHOLD:
        fields = ", ".join(r.low_confidence_fields[:4])
        problems.append(f"low OCR confidence ({r.min_confidence:.2f}{': ' + fields if fields else ''})")

    return problems
