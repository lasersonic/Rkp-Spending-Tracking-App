from datetime import date
from decimal import Decimal

import httpx
import pytest

from app import vision
from app.normalize import validate
from app.vision import OcrAuthError, OcrError, VisionClient, parse_response, rows_of, valid_tax_id

TODAY = date(2026, 10, 20)
SELLER_TAX = "0105561234560"
BUYER_TAX = "0105558000120"


def page(rows, conf=0.98, angle=0, width=1000, height=1400):
    """A Vision fullTextAnnotation page. Each row is a list of (text, x) words, or a string
    whose words are laid out left to right; rows are 40 px apart. `angle` turns the page
    (90 = photo taken sideways)."""
    words = []
    for i, row in enumerate(rows):
        if isinstance(row, str):
            x, laid = 20, []
            for w in row.split():
                laid.append((w, x))
                x += 14 * len(w) + 12
            row = laid
        y = 40 * i + 20
        for text, x in row:
            box = [(x, y), (x + 14 * len(text), y), (x + 14 * len(text), y + 30), (x, y + 30)]
            if angle == 90:  # rotate clockwise: text now runs top to bottom
                box = [(height - by, bx) for bx, by in box]
            symbols = [{"text": ch} for ch in text]
            symbols[-1]["property"] = {"detectedBreak": {"type": "SPACE"}}
            c = conf(text) if callable(conf) else conf
            words.append({"boundingBox": {"vertices": [{"x": bx, "y": by} for bx, by in box]},
                          "symbols": symbols, "confidence": c})
    return {"width": width, "height": height, "blocks": [{"paragraphs": [{"words": words}]}]}


def image_response(rows, **kw):
    pg = page(rows, **kw)
    text = "\n".join(r if isinstance(r, str) else " ".join(t for t, _ in r) for r in rows)
    return {"responses": [{"fullTextAnnotation": {"text": text, "pages": [pg]}}]}


def pdf_response(*pages_rows):
    return {"responses": [{"responses": [{"fullTextAnnotation": {"pages": [page(rows)], "text": ""}}
                                         for rows in pages_rows]}]}


TAX_INVOICE = [
    "บริษัท ทดสอบ เฟรช ฟู้ด จำกัด",
    [("เลขประจำตัวผู้เสียภาษี", 20), (":", 330), (SELLER_TAX, 350), ("Inv.", 650), ("No.", 710), (":", 760), ("FF6910000036", 780)],
    [("Tel.02-123-4567", 20), ("Date", 650), (":", 720), ("09/10/2569", 740)],
    [("Customer", 20), ("Name", 150), (":", 220), ("บริษัท", 240), ("ร้านข้าว", 330), ("จำกัด", 450), ("Due", 650), ("Date", 700), ("30/10/2569", 760)],
    [("เลขประจำตัวผู้เสียภาษี", 20), (":", 330), (BUYER_TAX, 350)],
    "299/194 หมู่บ้าน สุขใจ 2",
    [("รายการ", 120), ("จำนวน", 400), ("ราคา/หน่วย", 600), ("จำนวนเงิน", 850)],
    [("1", 20), ("520000", 50), ("หมูสับ", 150), ("3.00", 400), ("กิโลกรัม", 460), ("100.00", 600), ("300.00", 850)],
    [("2", 20), ("520001", 50), ("หมูชิ้น", 150), ("1.00", 400), ("กิโลกรัม", 460), ("110.00", 600), ("110.00", 850)],
    [("รวมเงิน", 500), ("Sub", 650), ("Total", 700), ("410.00", 850)],
    [("ภาษีมูลค่าเพิ่ม", 450), ("7%", 600), ("Total", 650), ("VAT", 720), ("26.82", 850)],
    [("ยอดสุทธิ", 500), ("Grand", 620), ("Total", 700), ("410.00", 850)],
]


def test_printed_tax_invoice():
    r = parse_response(image_response(TAX_INVOICE), TODAY)
    assert r.vendor_name == "บริษัท ทดสอบ เฟรช ฟู้ด จำกัด"
    assert (r.vendor_tax_id, r.customer_tax_id) == (SELLER_TAX, BUYER_TAX)
    assert r.customer_name == "บริษัท ร้านข้าว จำกัด"
    assert r.vendor_phone == "02-123-4567"
    assert r.invoice_no == "FF6910000036"
    assert r.receipt_date == date(2026, 10, 9)  # BE year; the due date is ignored
    assert (r.subtotal, r.vat, r.grand_total) == (Decimal("410.00"), Decimal("26.82"), Decimal("410.00"))
    assert [(i.item_code, i.item_name, i.qty, i.unit_cost, i.total_cost) for i in r.items] == [
        ("520000", "หมูสับ", Decimal("3"), Decimal("100.00"), Decimal("300.00")),
        ("520001", "หมูชิ้น", Decimal("1"), Decimal("110.00"), Decimal("110.00")),
    ]
    assert r.min_confidence == 0.98 and r.low_confidence_fields == []
    assert validate(r, TODAY) == []


def test_columns_read_top_down_still_find_totals():
    """Without geometry Vision's text has labels and values on separate lines."""
    data = {"responses": [{"fullTextAnnotation": {"text": "ร้านหมูอนามัย BETAGRO\n#INV2610170001\n"
                                                         "17/10/2026 08:05:22\nรวมทั้งหมด\n100.00\n"}}]}
    r = parse_response(data, TODAY)
    assert r.vendor_name == "ร้านหมูอนามัย BETAGRO"
    assert r.invoice_no == "INV2610170001"
    assert r.receipt_date == date(2026, 10, 17)
    assert r.grand_total == Decimal("100.00")
    assert r.min_confidence is None


def test_thermal_receipt_with_packaging_noise():
    rows = [
        "TAGRO",
        "ร้านหมูอนามัย BETAGRO",
        "#INV2610170001 ใบเสร็จรับเงิน",
        [("พนักงานขาย", 20), (":", 200), ("พนักงาน", 220), ("1", 340), ("17/10/2026", 500), ("08:05:22", 700)],
        [("สินค้า", 100), ("ราคารวม", 700)],
        [("10", 20), ("เต้าหู้ไข่อนามัย", 80), ("100.00", 700)],
        [("ราคารวม", 400), (":", 520), ("100.00", 700)],
        [("รวมทั้งหมด", 400), ("100.00", 700)],
        [("เงินทอน", 400), ("0.00", 700)],
        "13.10.69",  # expiry date printed on the pack behind the receipt
    ]
    r = parse_response(image_response(rows), TODAY)
    assert r.doc_type == "ใบเสร็จรับเงิน"
    assert r.receipt_date == date(2026, 10, 17)
    assert r.grand_total == r.subtotal == Decimal("100.00")
    assert [(i.item_name, i.qty, i.total_cost) for i in r.items] == [("เต้าหู้ไข่อนามัย", Decimal("10"), Decimal("100.00"))]


def test_handwritten_bill_uses_largest_lone_number_and_is_flagged():
    rows = [
        [("ไข่ไก่", 20), ("20", 200), ("ฟอง", 260), ("=", 340), ("180", 380)],
        [("ผัก", 20), ("2", 200), ("=", 340), ("40", 380)],
        [("16/10/69", 500)],
        [("220", 380)],
        [("0829796639", 500)],  # phone number on a shop stamp
    ]
    r = parse_response(image_response(rows, conf=0.7), TODAY)
    assert r.grand_total == Decimal("220.00")
    assert r.receipt_date == date(2026, 10, 16)
    assert r.vendor_name == "" and r.vendor_tax_id == ""
    assert "handwriting" in r.low_confidence_fields and r.min_confidence == 0.7
    assert any("low OCR confidence" in p for p in validate(r, TODAY))


def test_low_confidence_total_is_reported_by_field():
    conf = lambda text: 0.5 if text == "410.00" else 0.99  # noqa: E731
    r = parse_response(image_response(TAX_INVOICE, conf=conf), TODAY)
    assert "grand_total" in r.low_confidence_fields


def test_sideways_photo_is_turned_upright():
    r = parse_response(image_response(TAX_INVOICE, angle=90), TODAY)
    assert r.grand_total == Decimal("410.00") and r.invoice_no == "FF6910000036"
    assert [i.item_name for i in r.items] == ["หมูสับ", "หมูชิ้น"]


def test_pdf_pages_are_read_in_order():
    r = parse_response(pdf_response(TAX_INVOICE[:9], TAX_INVOICE[9:]), TODAY)
    assert r.grand_total == Decimal("410.00") and len(r.items) == 2


def test_tax_id_checksum_and_phone_numbers_are_not_tax_ids():
    assert valid_tax_id(SELLER_TAX) and not valid_tax_id("0105561234561")
    r = parse_response(image_response(["ร้านป้าแดง", "เลขประจำตัวผู้เสียภาษี 0105561234561", "รวม 50"]), TODAY)
    assert r.vendor_tax_id == "" and r.grand_total == Decimal("50.00")


def test_thai_digits_and_amount_formats():
    r = parse_response(image_response(["ร้านป้าแดง", "วันที่ ๑๕/๑๐/๒๕๖๙", "รวมทั้งสิ้น ๑,๒๔๐.๕๐ บาท"]), TODAY)
    assert r.receipt_date == date(2026, 10, 15)
    assert r.grand_total == Decimal("1240.50")


def test_error_and_empty_responses():
    with pytest.raises(OcrError):
        parse_response({"responses": [{"error": {"code": 3, "message": "Bad image data."}}]}, TODAY)
    with pytest.raises(OcrError):
        parse_response({"unexpected": True}, TODAY)
    r = parse_response({"responses": [{}]}, TODAY)  # blank image: no text at all
    assert r.grand_total is None and rows_of({"responses": [{}]}) == []


# --- client -------------------------------------------------------------------------

def client(monkeypatch, *responses):
    calls = []
    seq = iter(responses)

    def fake_post(url, **kw):
        calls.append((url, kw))
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(vision.httpx, "post", fake_post)
    c = VisionClient({}, sleep=lambda s: None)
    monkeypatch.setattr(c, "_token", lambda: "tok")
    return c, calls


REQ = httpx.Request("POST", "https://vision.test")


def test_client_image_request_and_retries(monkeypatch):
    ok = httpx.Response(200, json=image_response(TAX_INVOICE), request=REQ)
    c, calls = client(monkeypatch, httpx.ConnectError("boom"), httpx.Response(503, request=REQ), ok)
    r = c.read(b"abc", "r.jpg", "image/jpeg", TODAY)
    assert r.grand_total == Decimal("410.00") and len(calls) == 3
    url, kw = calls[-1]
    assert url.endswith("/images:annotate")
    req = kw["json"]["requests"][0]
    assert req["image"] == {"content": "YWJj"} and req["features"] == [{"type": "DOCUMENT_TEXT_DETECTION"}]
    assert kw["headers"]["Authorization"] == "Bearer tok"


def test_client_pdf_request(monkeypatch):
    c, calls = client(monkeypatch, httpx.Response(200, json=pdf_response(TAX_INVOICE), request=REQ))
    c.read(b"%PDF", "r.pdf", "application/pdf", TODAY)
    url, kw = calls[0]
    req = kw["json"]["requests"][0]
    assert url.endswith("/files:annotate")
    assert req["inputConfig"]["mimeType"] == "application/pdf" and req["pages"] == [1, 2, 3, 4, 5]


def test_client_auth_error_is_not_retried(monkeypatch):
    resp = httpx.Response(403, json={"error": {"message": "Cloud Vision API has not been used in project"}}, request=REQ)
    c, calls = client(monkeypatch, resp)
    with pytest.raises(OcrAuthError, match="has not been used"):
        c.ocr(b"abc", "r.jpg", "image/jpeg")
    assert len(calls) == 1


def test_client_gives_up_after_retries(monkeypatch):
    c, calls = client(monkeypatch, *[httpx.Response(429, request=REQ)] * 3)
    with pytest.raises(OcrError):
        c.ocr(b"abc", "r.jpg", "image/jpeg")
    assert len(calls) == 3


def test_client_rejected_file(monkeypatch):
    c, _ = client(monkeypatch, httpx.Response(400, json={"error": {"message": "Request payload size exceeds the limit"}}, request=REQ))
    with pytest.raises(OcrError, match="payload size"):
        c.ocr(b"abc", "r.jpg", "image/jpeg")
