from urllib.parse import parse_qs

from linebot.v3.webhooks import MessageEvent, PostbackEvent

from app.config import Settings
from app.iapp import IappAuthError, IappError
from app.line_client import OutMessage
from app.pipeline import Bot, detect_type, file_slug, fmt_baht, pdf_page_count
from tests.fakes import JPEG, NOW, TOTAL, FakeDrive, FakeLine, FakeOcr, FakeSheets, iapp_response

OWNER = "Uowner"
STAFF = "Ustaff"


def make_bot(ocr=None, drive=None, content=JPEG):
    settings = Settings(allowed_user_ids=frozenset({OWNER, STAFF}), owner_user_id=OWNER)
    line = FakeLine(content)
    bot = Bot(settings, line, FakeSheets(), drive or FakeDrive(), ocr or FakeOcr(iapp_response()), now=lambda: NOW)
    return bot, line


def _base(user, reply_token="rt"):
    return {"source": {"type": "user", "userId": user}, "timestamp": 0, "mode": "active",
            "webhookEventId": "w", "deliveryContext": {"isRedelivery": False}, "replyToken": reply_token}


def image_event(user=STAFF, msg_id="m1"):
    return MessageEvent.from_dict({**_base(user), "type": "message",
                                   "message": {"type": "image", "id": msg_id, "quoteToken": "q", "contentProvider": {"type": "line"}}})


def text_event(text, user=STAFF):
    return MessageEvent.from_dict({**_base(user), "type": "message",
                                   "message": {"type": "text", "id": "t1", "text": text, "quoteToken": "q"}})


def postback_event(data, user=STAFF):
    return PostbackEvent.from_dict({**_base(user), "type": "postback", "postback": {"data": data}})


def last_reply(line) -> OutMessage:
    return line.sent[-1][1]


def button_data(msg: OutMessage, label: str) -> str:
    return next(b.data for b in msg.buttons + msg.quick_replies if b.label == label)


# -- helpers ---------------------------------------------------------------------

def test_detect_type_and_pages():
    assert detect_type(JPEG) == ("image/jpeg", "jpg")
    assert detect_type(b"%PDF-1.7 ...") == ("application/pdf", "pdf")
    assert detect_type(b"GIF89a") is None
    assert pdf_page_count(b"%PDF /Type /Pages /Type /Page x /Type/Page") == 2


def test_formatting():
    assert fmt_baht(TOTAL) == "฿1,240"
    assert fmt_baht(TOTAL + 0) == "฿1,240"
    assert file_slug("Makro / Lat Phrao") == "Makro-Lat-Phrao"


# -- the main flow -----------------------------------------------------------------

def test_new_vendor_receipt_is_archived_logged_and_asks_for_category():
    bot, line = make_bot()
    bot.handle(image_event())

    [row] = bot.sheets.rows
    assert row["receipt_date"] == "2026-10-15"
    assert row["grand_total"] == "1,240.00"
    assert row["vat"] == "81.12"
    assert row["net_before_vat"] == "1,158.88"
    assert row["vendor_tax_id"] == "0107537000521"
    assert row["category"] == ""
    assert row["review_status"] == "ok"
    assert row["source"] == "line" and row["submitted_by"] == STAFF
    assert len(bot.sheets.items) == 2

    [f] = bot.drive.files.values()
    assert f["folder"] == "2026/10"
    assert f["name"] == "2026-10-15_บริษัท-สยามแม็คโคร-จำกัด-(มหาชน)_1240.00.jpg"

    to, msg, token = line.sent[-1]
    assert (to, token) == (STAFF, "rt")
    assert msg.text.startswith("฿1,240 · บริษัท สยามแม็คโคร")
    assert "❓ category" in msg.text and "15/10" in msg.text
    assert [b.label for b in msg.buttons] == ["OK", "Change category", "Flag"]
    assert "Food cost" in [b.label for b in msg.quick_replies]


def test_choosing_a_category_is_remembered_for_the_vendor():
    bot, line = make_bot()
    bot.handle(image_event())
    bot.handle(postback_event(button_data(last_reply(line), "Food cost")))

    assert bot.sheets.rows[0]["category"] == "FOOD"
    assert {i["category"] for i in bot.sheets.items} == {"FOOD"}
    [vendor] = bot.sheets.vendor_rows.values()
    assert vendor.tax_id == "0107537000521" and vendor.category == "FOOD"
    assert "I'll remember" in last_reply(line).text

    # Next receipt from the same vendor is categorized automatically.
    bot.ocr.response = iapp_response(invoiceID="INV0099999", issuerName="SIAM MAKRO")
    line.content = JPEG + b"second"
    bot.handle(image_event(msg_id="m2"))
    assert bot.sheets.rows[1]["category"] == "FOOD"
    assert last_reply(line).text == "฿1,240 · SIAM MAKRO · Food cost · 15/10 ✅"
    assert not last_reply(line).quick_replies
    [vendor] = bot.sheets.vendor_rows.values()
    assert vendor.receipt_count == 2 and "SIAM MAKRO" in vendor.aliases


def test_change_category_ok_and_flag_buttons():
    bot, line = make_bot()
    bot.handle(image_event())
    summary = last_reply(line)

    bot.handle(postback_event(button_data(summary, "Change category")))
    assert len(last_reply(line).quick_replies) == 11
    bot.handle(postback_event(button_data(last_reply(line), "Rent")))
    assert bot.sheets.rows[0]["category"] == "RENT"

    bot.handle(postback_event(button_data(summary, "Flag")))
    assert bot.sheets.rows[0]["review_status"] == "flagged"
    bot.handle(postback_event(button_data(summary, "OK")))
    assert bot.sheets.rows[0]["review_status"] == "confirmed"


def test_validation_problems_set_review_flag():
    bot, line = make_bot(ocr=FakeOcr(iapp_response(vat="10.00")))
    bot.handle(image_event())
    row = bot.sheets.rows[0]
    assert row["review_status"] == "needs_review"
    assert "VAT" in row["review_note"]
    assert "⚠️" in last_reply(line).text


def test_same_file_twice_is_skipped():
    bot, line = make_bot()
    bot.handle(image_event())
    bot.handle(image_event(msg_id="m2"))
    assert len(bot.sheets.rows) == 1 and len(bot.drive.files) == 1
    assert "Already logged" in last_reply(line).text
    assert bot.ocr.calls == 1


def test_same_invoice_different_photo_is_skipped():
    bot, line = make_bot()
    bot.handle(image_event())
    line.content = JPEG + b"another photo of the same bill"
    bot.handle(image_event(msg_id="m2"))
    assert len(bot.sheets.rows) == 1
    assert [f["trashed"] for f in bot.drive.files.values()] == [False, True]
    assert "already logged" in last_reply(line).text


def test_out_of_credits_keeps_file_and_alerts_owner_then_retry_works():
    ocr = FakeOcr(error=IappAuthError("iApp returned 402 (bad key or out of credits)"))
    bot, line = make_bot(ocr=ocr)
    bot.handle(image_event())

    [row] = bot.sheets.rows
    assert row["review_status"] == "pending_ocr"
    assert len(bot.drive.files) == 1
    assert any(to == OWNER and "402" in m.text for to, m, _ in line.sent)
    assert "/retry" in last_reply(line).text

    ocr.error = None
    ocr.response = iapp_response()
    bot.handle(text_event("/retry"))
    assert "1 logged" in last_reply(line).text
    row = bot.sheets.rows[0]
    assert row["review_status"] == "ok" and row["grand_total"] == "1,240.00"
    assert len(bot.sheets.items) == 2
    assert next(iter(bot.drive.files.values()))["name"].startswith("2026-10-15_")


def test_transient_ocr_failure_is_pending():
    bot, line = make_bot(ocr=FakeOcr(error=IappError("iApp returned 503")))
    bot.handle(image_event())
    assert bot.sheets.rows[0]["review_status"] == "pending_ocr"
    assert not any(to == OWNER for to, _, _ in line.sent)  # only auth errors alert the owner


def test_drive_failure_tells_user_and_owner():
    bot, line = make_bot(drive=FakeDrive(fail=True))
    bot.handle(image_event())
    assert bot.sheets.rows == []
    assert "couldn't save" in last_reply(line).text
    assert any(to == OWNER for to, _, _ in line.sent)


def test_unsupported_file_type():
    bot, line = make_bot(content=b"GIF89a....")
    bot.handle(image_event())
    assert "JPG/PNG" in last_reply(line).text
    assert bot.drive.files == {}


def test_pdf_over_page_limit_is_archived_for_manual_entry():
    pdf = b"%PDF-1.4 " + b"/Type /Page " * 11
    bot, line = make_bot(content=pdf)
    bot.handle(image_event())
    assert bot.ocr.calls == 0
    assert len(bot.drive.files) == 1
    assert bot.sheets.rows[0]["review_status"] == "needs_review"
    assert "11 pages" in last_reply(line).text


def test_stranger_is_rejected_with_their_id():
    bot, line = make_bot()
    bot.handle(image_event(user="Ustranger"))
    assert bot.sheets.rows == []
    assert "Ustranger" in last_reply(line).text


def test_month_summary_and_help():
    bot, line = make_bot()
    bot.handle(image_event())
    bot.handle(postback_event(button_data(last_reply(line), "Food cost")))
    bot.handle(text_event("สรุป"))
    text = last_reply(line).text
    assert "10/2026" in text and "Food cost: ฿1,240" in text and "Total: ฿1,240" in text

    bot.handle(text_event("hello"))
    assert "Forward a receipt" in last_reply(line).text


def test_postback_payload_fits_line_limit():
    bot, line = make_bot()
    bot.handle(image_event())
    for b in last_reply(line).buttons + last_reply(line).quick_replies:
        assert len(b.data) <= 300 and len(b.label) <= 20
        assert parse_qs(b.data)["r"] == [bot.sheets.rows[0]["receipt_id"]]
