from urllib.parse import parse_qs

from linebot.v3.webhooks import MessageEvent, PostbackEvent

from app.categorize import Vendor, vendor_key
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


def _base(user, reply_token="rt", group=None):
    source = {"type": "group", "groupId": group, "userId": user} if group else {"type": "user", "userId": user}
    return {"source": source, "timestamp": 0, "mode": "active",
            "webhookEventId": "w", "deliveryContext": {"isRedelivery": False}, "replyToken": reply_token}


def image_event(user=STAFF, msg_id="m1", group=None):
    return MessageEvent.from_dict({**_base(user, group=group), "type": "message",
                                   "message": {"type": "image", "id": msg_id, "quoteToken": "q", "contentProvider": {"type": "line"}}})


def text_event(text, user=STAFF, group=None):
    return MessageEvent.from_dict({**_base(user, group=group), "type": "message",
                                   "message": {"type": "text", "id": "t1", "text": text, "quoteToken": "q"}})


def sticker_event(user=STAFF, group=None):
    return MessageEvent.from_dict({**_base(user, group=group), "type": "message",
                                   "message": {"type": "sticker", "id": "s1", "quoteToken": "q", "packageId": "1",
                                               "stickerId": "1", "stickerResourceType": "STATIC"}})


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
    assert detect_type(b"\x00\x00\x00\x18ftypheic....") == ("image/heic", "heic")
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
    assert row["vendor_name"].startswith("บริษัท สยามแม็คโคร")
    for dropped in ("vendor_tax_id", "vendor_phone", "vendor_address", "customer_name", "customer_tax_id",
                    "submitted_by", "doc_type", "invoice_book", "po_no"):
        assert dropped not in row
    assert row["category"] == ""
    assert row["review_status"] == "ok"
    assert row["source"] == "line"
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


def test_vendor_without_tax_id_learns_it():
    bot, line = make_bot()
    bot.sheets.save_vendor(Vendor("name:makro", "", "บริษัท สยามแม็คโคร จำกัด (มหาชน)", category="FOOD"))
    bot.handle(image_event())
    [v] = bot.sheets.vendors()
    assert v.tax_id == "0107537000521" and v.receipt_count == 1
    assert bot.sheets.rows[0]["category"] == "FOOD"


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
    bot.handle(text_event("Summary"))
    text = last_reply(line).text
    assert "10/2026" in text and "Food cost: ฿1,240" in text and "Total: ฿1,240" in text

    bot.handle(text_event("help"))
    assert "Forward a receipt" in last_reply(line).text


def test_other_messages_are_ignored():
    bot, line = make_bot()
    for event in (text_event("hello"), text_event("สรุป"), text_event("summary please"), sticker_event(),
                  text_event("hello", group="Cgroup"), sticker_event(group="Cgroup")):
        bot.handle(event)
    assert line.sent == []


def test_group_chat_receipt_and_commands_reply_to_group():
    bot, line = make_bot()
    bot.handle(image_event(group="Cgroup"))
    assert bot.sheets.rows and line.sent[-1][0] == "Cgroup"
    bot.handle(text_event("summary", group="Cgroup"))
    assert line.sent[-1][0] == "Cgroup" and "Total:" in last_reply(line).text
    sent = len(line.sent)
    bot.handle(text_event("hi", user="Ustranger", group="Cgroup"))
    assert len(line.sent) == sent  # strangers are ignored in groups


def test_postback_payload_fits_line_limit():
    bot, line = make_bot()
    bot.handle(image_event())
    for b in last_reply(line).buttons + last_reply(line).quick_replies:
        assert len(b.data) <= 300 and len(b.label) <= 20
        assert parse_qs(b.data)["r"] == [bot.sheets.rows[0]["receipt_id"]]


SHOPS = ["ร้านผักสมบัติโน๊ต", "ร้านไข่นายร้อย", "ร้านหมูอนามัย BETAGRO", "ร้านหมูเจ๊จิ๋ว", "ร้านกาญ กุ้งสด"]


def bot_with_shops():
    bot, line = make_bot(ocr=FakeOcr(iapp_response(issuerName="", issuerTaxID="")))
    for name in SHOPS:
        bot.sheets.save_vendor(Vendor(vendor_key("", name), "", name, category="FOOD" if "BETAGRO" in name else ""))
    return bot, line


def test_unknown_vendor_asks_which_shop():
    bot, line = bot_with_shops()
    bot.handle(image_event())
    msg = last_reply(line)
    assert "Which shop is this?" in msg.text
    labels = [b.label for b in msg.quick_replies]
    assert "หมูอนามัย BETAGRO" in labels and "ร้านผักสมบัติโน๊ต" in labels and len(labels) == 5
    assert all(len(b.label) <= 20 and len(b.data) <= 300 for b in msg.quick_replies)
    assert bot.sheets.rows[0]["review_status"] == "needs_review"


def test_picking_a_shop_with_a_category_fills_the_row():
    bot, line = bot_with_shops()
    bot.handle(image_event())
    bot.handle(postback_event(button_data(last_reply(line), "หมูอนามัย BETAGRO")))
    row = bot.sheets.rows[0]
    assert row["vendor_name"] == "ร้านหมูอนามัย BETAGRO" and row["category"] == "FOOD"
    assert row["review_status"] == "ok" and row["review_note"] == ""
    assert {i["category"] for i in bot.sheets.items} == {"FOOD"}
    [f] = bot.drive.files.values()
    assert f["name"].startswith("2026-10-15_ร้านหมูอนามัย-BETAGRO_") and f["name"].endswith(".jpg")
    assert "✅ ร้านหมูอนามัย BETAGRO" in last_reply(line).text
    assert next(v for v in bot.sheets.vendors() if "BETAGRO" in v.name).receipt_count == 1


def test_picking_a_shop_without_a_category_then_asks_for_one():
    bot, line = bot_with_shops()
    bot.handle(image_event())
    bot.handle(postback_event(button_data(last_reply(line), "ร้านไข่นายร้อย")))
    assert "pick a category" in last_reply(line).text
    bot.handle(postback_event(button_data(last_reply(line), "Food cost")))
    assert bot.sheets.rows[0]["category"] == "FOOD"
    assert next(v for v in bot.sheets.vendors() if v.name == "ร้านไข่นายร้อย").category == "FOOD"
