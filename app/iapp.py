"""iApp Thai Receipt OCR v3: the API call (with retries) and response mapping.

Per iApp's docs the response is {"message", "processed": {...fields, "items": [...]}, "raw"},
with item fields itemName / itemUnitCost / itemTotalCost. The parser is deliberately tolerant: a field may be a plain value or an
object like {"value": ..., "confidence": ...}, the payload may be wrapped in
"data"/"result", and multi-page PDFs may come back as a list of pages.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .normalize import (
    LineItem,
    ReceiptData,
    clean_tax_id,
    clean_text,
    parse_amount,
    parse_date,
    parse_number,
)

log = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 10
RETRY_DELAYS = (2.0, 5.0)  # two retries with backoff


class IappError(Exception):
    """OCR failed; the receipt should be kept as pending_ocr."""


class IappAuthError(IappError):
    """401/402: bad key or out of credits. The owner must be told."""


class IappClient:
    def __init__(self, api_key: str, url: str, timeout: float = 60.0, sleep=time.sleep):
        self.api_key = api_key
        self.url = url
        self.timeout = timeout
        self._sleep = sleep

    def ocr(self, content: bytes, filename: str, mime_type: str) -> Any:
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                resp = httpx.post(
                    self.url,
                    headers={"apikey": self.api_key},
                    files={"file": (filename, content, mime_type)},
                    timeout=self.timeout,
                )
            except httpx.TransportError as exc:
                error: IappError = IappError(f"network error: {exc.__class__.__name__}")
            else:
                if resp.status_code in (401, 402):
                    raise IappAuthError(f"iApp returned {resp.status_code} (bad key or out of credits)")
                if resp.status_code == 429 or resp.status_code >= 500:
                    error = IappError(f"iApp returned {resp.status_code}")
                elif resp.status_code >= 400:
                    raise IappError(f"iApp rejected the file ({resp.status_code}): {resp.text[:200]}")
                else:
                    return resp.json()
            if attempt < attempts - 1:
                log.warning("iApp attempt %d failed (%s), retrying", attempt + 1, error)
                self._sleep(RETRY_DELAYS[attempt])
        raise error


# --- response mapping -------------------------------------------------------

_VALUE_KEYS = ("value", "text", "result")
_CONF_KEYS = ("confidence", "conf", "score")


def _unwrap(value: Any) -> tuple[Any, float | None]:
    """Return (value, confidence) for a plain value or a {value, confidence} object."""
    if isinstance(value, dict):
        conf = next((value[k] for k in _CONF_KEYS if isinstance(value.get(k), (int, float))), None)
        raw = next((value[k] for k in _VALUE_KEYS if k in value), None)
        return raw, (float(conf) if conf is not None else None)
    return value, None


def _payload(data: Any) -> Any:
    # iApp v3 puts the extracted fields under "processed" (next to "raw" and "message").
    for key in ("processed", "data", "result", "prediction", "predictions"):
        if isinstance(data, dict) and key in data and isinstance(data[key], (dict, list)):
            return _payload(data[key])
    return data


def _merge_pages(pages: list[dict]) -> dict:
    """Header fields from the page that has a grand total (usually the last),
    line items from every page."""
    pages = [p for p in pages if isinstance(p, dict)]
    if not pages:
        return {}
    main = next((p for p in reversed(pages) if _unwrap(p.get("grandTotal"))[0] not in (None, "")), pages[-1])
    merged = {**main}
    merged["items"] = [item for p in pages for item in (p.get("items") or [])]
    return merged


def _confidences(obj: Any, prefix: str = "") -> list[tuple[str, float]]:
    """Collect every confidence number found in the payload."""
    found: list[tuple[str, float]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _CONF_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                found.append((prefix or k, float(v)))
            elif k in _CONF_KEYS and isinstance(v, dict):
                # {"confidence": {"grandTotal": 0.9, ...}}
                found.extend(
                    (name, float(c)) for name, c in v.items() if isinstance(c, (int, float))
                )
            else:
                found.extend(_confidences(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_confidences(v, f"{prefix}[{i}]"))
    return found


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d:
            value, _ = _unwrap(d[k])
            if value not in (None, ""):
                return value
    return None


def parse_response(data: Any, today=None) -> ReceiptData:
    payload = _payload(data)
    if isinstance(payload, list):
        payload = _merge_pages(payload)
    if not isinstance(payload, dict):
        raise IappError("unexpected iApp response shape")

    def f(*keys):
        return _first(payload, *keys)

    raw_date = clean_text(f("invoiceDate", "date"))
    r = ReceiptData(
        doc_type=clean_text(f("invoiceType", "documentType")),
        invoice_book=clean_text(f("invoiceBook")),
        invoice_no=clean_text(f("invoiceID", "invoiceId", "invoiceNo")),
        po_no=clean_text(f("purchaseOrderID", "purchaseOrderId")),
        raw_date=raw_date,
        receipt_date=parse_date(raw_date, today),
        vendor_name=clean_text(f("issuerName")),
        vendor_tax_id=clean_tax_id(f("issuerTaxID", "issuerTaxId")),
        vendor_phone=clean_text(f("issuerPhone")),
        vendor_address=clean_text(f("issuerAddress")),
        customer_name=clean_text(f("customerName")),
        customer_tax_id=clean_tax_id(f("customerTaxID", "customerTaxId")),
        subtotal=parse_amount(f("totalCost")),
        discount=parse_amount(f("discount")),
        after_discount=parse_amount(f("totalCostAfterDiscount")),
        vat=parse_amount(f("vat")),
        grand_total=parse_amount(f("grandTotal")),
        payment_method=clean_text(f("paymentMethod")),
    )

    for n, item in enumerate(payload.get("items") or [], start=1):
        if not isinstance(item, dict):
            continue
        li = LineItem(
            item_no=n,
            item_code=clean_text(_first(item, "itemCode", "itemID", "code")),
            item_name=clean_text(_first(item, "itemName", "name", "description")),
            qty=parse_number(_first(item, "itemQuantity", "itemQty", "itemAmount", "quantity", "qty")),
            unit_cost=parse_amount(_first(item, "itemUnitCost", "unitCost", "unitPrice", "price")),
            total_cost=parse_amount(_first(item, "itemTotalCost", "totalCost", "amount", "total")),
        )
        if li.item_name or li.total_cost is not None:
            r.items.append(li)

    confs = _confidences(payload)
    if confs and max(c for _, c in confs) > 1:  # reported as a percentage
        confs = [(name, c / 100) for name, c in confs]
    if confs:
        r.min_confidence = round(min(c for _, c in confs), 3)
        r.low_confidence_fields = sorted({name for name, c in confs if c < 0.85})
    return r
