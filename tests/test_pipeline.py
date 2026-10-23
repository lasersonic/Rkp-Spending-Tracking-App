from urllib.parse import parse_qs

from linebot.v3.webhooks import MessageEvent, PostbackEvent

from app.categorize import Vendor, vendor_key
from app.config import Settings
from app.line_client import OutMessage
from app.pipeline import Bot, detect_type, file_slug, fmt_baht, pdf_page_count
from app.vision import OcrAuthError, OcrError
from tests.fakes import JPEG, NOW, TOTAL, FakeDrive, FakeLine, FakeOcr, FakeSheets, receipt

OWNER = "Uowner"
STAFF = "Ustaff"
SHOPS = ["ร้านผักสมบัติโน๊ต", "ร้านไข่นายร้อย", "ร้านหมูอนามัย BETAGRO", "ร้านหมูเจ๊จิ๋ว", "ร้านกาญ กุ้งสด"]
BETAGRO = "ร้านหมูอนามัย BETAGRO"


def make_bot(ocr=None, drive=None, content=JPEG):
    """The LINE bot as deployed: no OCR, people type the details."""
    settings = Settings(allowed_user_ids=frozenset({OWNER, STAFF}), owner_user_id=OWNER)
    line = FakeLine(content)
    bot = Bot(settings, line, FakeSheets(), drive or FakeDrive(), ocr, now=lambda: NOW)
    return bot, line


def shop_bot(**kw):
    """Suppliers tab filled in; Betagro already has a category."""
    bot, line = make_bot(**kw)
    bot.sheets.supplier_names = list(SHOPS)
    bot.sheets.save_vendor(Vendor(vendor_key("", BETAGRO), "", BETAGRO, category="FOOD"))
    return bot, line


def ocr_bot(**kw):
    """The backfill's bot: reads receipts with OCR instead of asking."""
    return make_bot(ocr=kw.pop("ocr", None) or FakeOcr(receipt()), **kw)


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


def postback_event(data, user=STAFF, group=None):
    return PostbackEvent.from_dict({**_base(user, group=group), "type": "postback", "postback": {"data": data}})


def last_reply(line) -> OutMessage:
    return line.sent[-1][1]


def button_data(msg: OutMessage, label: str) -> str:
    return next(b.data for b in msg.buttons + msg.quick_replies if b.label == label)


def tap(bot, line, label, **kw):
    bot.handle(postback_event(button_data(last_reply(line), label), **kw))


def labels(msg: OutMessage, quick=True) -> list[str]:
    return [b.label for b in (msg.quick_replies if quick else msg.buttons)]


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


# -- the LINE flow: photo -> supplier -> price -> saved ---------------------------

def test_photo_is_archived_and_asks_for_the_supplier():
    bot, line = shop_bot()
    bot.handle(image_event())

    [row] = bot.sheets.rows
    rid = row["receipt_id"]
    assert row["receipt_date"] == "2026-10-17" and row["source"] == "line"
    assert row["review_status"] == "incomplete" and row["review_note"] == "waiting for supplier and price"
    assert row["vendor_name"] == "" and row["grand_total"] == ""
    [f] = bot.drive.files.values()
    assert f["name"] == f"2026-10-17_{rid}.jpg" and f["folder"] == "2026/10"

    msg = last_reply(line)
    assert msg.text.startswith(f"📁 Saved to Drive ({rid}).") and "Which supplier?" in msg.text
    assert labels(msg) == ["ร้านผักสมบัติโน๊ต", "ร้านไข่นายร้อย", "หมูอนามัย BETAGRO", "ร้านหมูเจ๊จิ๋ว",
                           "ร้านกาญ กุ้งสด", "✏️ Type name"]
    assert all(len(b.label) <= 20 and len(b.data) <= 300 for b in msg.quick_replies)


def test_pick_supplier_then_type_price_saves_and_shows_the_result():
    bot, line = shop_bot()
    bot.handle(image_event())
    tap(bot, line, "หมูอนามัย BETAGRO")
    assert last_reply(line).text.startswith(f"Supplier: {BETAGRO}\nType the total price for {BETAGRO}")
    assert bot.sheets.rows[0]["review_note"] == "waiting for price"

    bot.handle(text_event("1,250"))
    row = bot.sheets.rows[0]
    assert row["vendor_name"] == BETAGRO and row["category"] == "FOOD"
    assert row["grand_total"] == "1,250.00" and row["net_before_vat"] == "1,250.00"
    assert row["review_status"] == "ok" and row["review_note"] == ""
    [f] = bot.drive.files.values()
    assert f["name"] == "2026-10-17_ร้านหมูอนามัย-BETAGRO_1250.00.jpg"
    assert next(v for v in bot.sheets.vendors() if v.name == BETAGRO).receipt_count == 1

    msg = last_reply(line)
    assert msg.text == f"Saved: ฿1,250 · {BETAGRO} · Food cost · 17/10 ✅"
    assert labels(msg, quick=False) == ["Change price", "Change category", "Change supplier"]
    assert not msg.quick_replies

    sent = len(line.sent)
    bot.handle(text_event("1300"))  # not asked any more: ordinary chat is ignored
    assert len(line.sent) == sent


def test_typed_new_supplier_then_price_then_category_is_remembered():
    bot, line = shop_bot()
    bot.handle(image_event())
    tap(bot, line, "✏️ Type name")
    assert last_reply(line).text.startswith("Type the supplier name.")
    bot.handle(text_event("  ร้านป้าแดง  "))
    assert "Type the total price for ร้านป้าแดง" in last_reply(line).text
    bot.handle(text_event("300"))

    msg = last_reply(line)
    assert msg.text == "Saved: ฿300 · ร้านป้าแดง · ❓ category · 17/10\nNew supplier: pick a category below."
    assert "Food cost" in labels(msg)
    tap(bot, line, "Food cost")
    assert last_reply(line).text == "Saved: ฿300 · ร้านป้าแดง · Food cost · 17/10 ✅"
    assert bot.sheets.rows[0]["category"] == "FOOD"
    assert next(v for v in bot.sheets.vendors() if v.name == "ร้านป้าแดง").category == "FOOD"

    # Next time the typed name (any spacing/case) matches and is categorized by itself.
    line.content = JPEG + b"second"
    bot.handle(image_event(msg_id="m2"))
    tap(bot, line, "✏️ Type name")
    bot.handle(text_event("ร้านป้าแดง"))
    bot.handle(text_event("120"))
    assert last_reply(line).text == "Saved: ฿120 · ร้านป้าแดง · Food cost · 17/10 ✅"


def test_in_a_1to1_chat_the_name_can_be_typed_without_the_button():
    bot, line = shop_bot()
    bot.handle(image_event())
    bot.handle(text_event("Makro"))
    assert "Type the total price for Makro" in last_reply(line).text


def test_in_a_group_chat_needs_the_button_before_typing_a_name():
    bot, line = shop_bot()
    bot.handle(image_event(group="Cgroup"))
    assert line.sent[-1][0] == "Cgroup"
    sent = len(line.sent)
    bot.handle(text_event("ok thanks", group="Cgroup"))  # ordinary chat isn't taken as a name
    assert len(line.sent) == sent and bot.sheets.rows[0]["vendor_name"] == ""
    tap(bot, line, "✏️ Type name", group="Cgroup")
    bot.handle(text_event("Makro", group="Cgroup"))
    bot.handle(text_event("99", group="Cgroup"))
    assert bot.sheets.rows[0]["vendor_name"] == "Makro" and bot.sheets.rows[0]["grand_total"] == "99.00"
    assert line.sent[-1][0] == "Cgroup"


def test_no_suppliers_on_the_tab_asks_to_type_the_name():
    bot, line = make_bot()
    bot.handle(image_event())
    msg = last_reply(line)
    assert msg.text.endswith("Type the supplier name.") and labels(msg) == ["✏️ Type name"]
    bot.handle(text_event("Makro"))
    assert bot.sheets.rows[0]["vendor_name"] == "Makro"


def test_bad_answers_are_asked_again_and_cancel_stops():
    bot, line = shop_bot()
    bot.handle(image_event())
    tap(bot, line, "✏️ Type name")
    bot.handle(text_event("1250"))  # a number isn't a supplier name
    assert "Type the supplier's name" in last_reply(line).text
    bot.handle(text_event("Makro"))
    bot.handle(text_event("twelve fifty"))
    assert "isn't a price" in last_reply(line).text
    bot.handle(text_event("summary"))  # commands still work while waiting
    assert "No receipts logged" in last_reply(line).text
    bot.handle(text_event("cancel"))
    assert "stopped" in last_reply(line).text
    bot.handle(text_event("1250"))
    assert bot.sheets.rows[0]["grand_total"] == ""
    assert bot.sheets.rows[0]["review_status"] == "incomplete"


def test_other_people_cannot_answer_for_you():
    bot, line = shop_bot()
    bot.handle(image_event())
    tap(bot, line, "หมูอนามัย BETAGRO")
    bot.handle(text_event("999", user=OWNER))
    assert bot.sheets.rows[0]["grand_total"] == ""
    bot.handle(text_event("50"))
    assert bot.sheets.rows[0]["grand_total"] == "50.00"


def test_after_the_wait_expires_buttons_still_work(monkeypatch):
    bot, line = shop_bot()
    bot.handle(image_event())
    first = last_reply(line)
    import app.pipeline as pipeline
    real = pipeline.time.monotonic
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: real() + pipeline.WAIT_SECONDS + 1)
    sent = len(line.sent)
    bot.handle(text_event("Makro"))  # too late to count as the answer
    assert len(line.sent) == sent
    bot.handle(postback_event(button_data(first, "หมูอนามัย BETAGRO")))  # the chips on the old message work
    bot.handle(text_event("75"))
    assert bot.sheets.rows[0]["grand_total"] == "75.00"


def complete_bot():
    bot, line = shop_bot()
    bot.handle(image_event())
    tap(bot, line, "หมูอนามัย BETAGRO")
    bot.handle(text_event("1250"))
    return bot, line


def test_change_price_from_the_result():
    bot, line = complete_bot()
    tap(bot, line, "Change price")
    assert last_reply(line).text.startswith(f"Type the correct total for {BETAGRO} (now ฿1,250)")
    bot.handle(text_event("฿1,300.50"))
    row = bot.sheets.rows[0]
    assert row["grand_total"] == "1,300.50" and row["review_status"] == "ok"
    assert row["review_note"] == "total edited by hand (was ฿1,250)"
    assert last_reply(line).text == f"Saved: ฿1,300.50 · {BETAGRO} · Food cost · 17/10 ✅"
    [f] = bot.drive.files.values()
    assert f["name"].endswith("_1300.50.jpg")


def test_change_category_from_the_result():
    bot, line = complete_bot()
    tap(bot, line, "Change category")
    assert len(last_reply(line).quick_replies) == 11
    tap(bot, line, "Rent")
    assert bot.sheets.rows[0]["category"] == "RENT"
    assert last_reply(line).text == f"Saved: ฿1,250 · {BETAGRO} · Rent · 17/10 ✅"


def test_change_supplier_from_the_result_keeps_the_price():
    bot, line = complete_bot()
    tap(bot, line, "Change supplier")
    assert "✏️ Type name" in labels(last_reply(line))
    tap(bot, line, "ร้านไข่นายร้อย")
    row = bot.sheets.rows[0]
    assert row["vendor_name"] == "ร้านไข่นายร้อย" and row["grand_total"] == "1,250.00"
    # The new shop has no category yet, so the receipt keeps the one it had.
    assert last_reply(line).text == "Saved: ฿1,250 · ร้านไข่นายร้อย · Food cost · 17/10 ✅"
    betagro = next(v for v in bot.sheets.vendors() if v.name == BETAGRO)
    assert "ร้านไข่นายร้อย" not in betagro.aliases and BETAGRO not in next(
        v for v in bot.sheets.vendors() if v.name == "ร้านไข่นายร้อย").aliases  # a wrong tap isn't a misreading


def test_same_file_twice_is_skipped():
    bot, line = shop_bot()
    bot.handle(image_event())
    bot.handle(image_event(msg_id="m2"))
    assert len(bot.sheets.rows) == 1 and len(bot.drive.files) == 1
    assert "Already logged" in last_reply(line).text


def test_heic_and_large_files_are_simply_archived():
    bot, line = shop_bot(content=b"\x00\x00\x00\x18ftypheic....")
    bot.handle(image_event())
    assert len(bot.drive.files) == 1 and "Which supplier?" in last_reply(line).text


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


def test_stranger_is_rejected_with_their_id():
    bot, line = make_bot()
    bot.handle(image_event(user="Ustranger"))
    assert bot.sheets.rows == []
    assert "Ustranger" in last_reply(line).text


def test_month_summary_and_help():
    bot, line = complete_bot()
    bot.handle(text_event("Summary"))
    text = last_reply(line).text
    assert "10/2026" in text and "Food cost: ฿1,250" in text and "Total: ฿1,250" in text

    bot.handle(text_event("help"))
    assert "ask for the supplier and the price" in last_reply(line).text
    assert "/retry" not in last_reply(line).text
    sent = len(line.sent)
    bot.handle(text_event("/retry"))  # only the OCR backfill has anything to retry
    assert len(line.sent) == sent


def test_daily_lists_receipts_logged_today():
    bot, line = shop_bot()
    bot.handle(text_event("daily"))
    assert last_reply(line).text == "No receipts logged today (17/10) yet."

    bot.handle(image_event())
    tap(bot, line, "หมูอนามัย BETAGRO")
    bot.handle(text_event("1240"))
    line.content = JPEG + b"second"
    bot.handle(image_event(msg_id="m2"))  # not answered yet
    # Logged yesterday (even though dated today) and a pending OCR one from today.
    bot.sheets.rows.append({"receipt_id": "R261016-aaaaaa", "received_at": "2026-10-16 09:00:00",
                            "receipt_date": "2026-10-17", "grand_total": "99.00", "review_status": "ok"})
    bot.sheets.rows.append({"receipt_id": "R261017-bbbbbb", "received_at": "2026-10-17 15:00:00",
                            "review_status": "pending_ocr"})
    bot.handle(text_event("/Daily"))
    rid2 = bot.sheets.rows[1]["receipt_id"]
    assert last_reply(line).text.splitlines() == [
        "🧾 Today 17/10 (3 receipts)",
        f"• ฿1,240 · {BETAGRO} · Food cost ✅",
        f"• ⏳ {rid2} · waiting for supplier and price",
        "• ⏳ R261017-bbbbbb · waiting for OCR",
        "Total: ฿1,240",
    ]

    bot.handle(text_event("help"))
    assert "daily – receipts logged today" in last_reply(line).text


def test_daily_caps_a_long_list():
    bot, line = make_bot()
    bot.sheets.rows.extend(
        {"receipt_id": f"R{n}", "received_at": "2026-10-17 08:00:00", "vendor_name": f"Shop {n}",
         "grand_total": "10.00", "review_status": "needs_review"}
        for n in range(35)
    )
    bot.handle(text_event("daily"))
    lines = last_reply(line).text.splitlines()
    assert lines[0] == "🧾 Today 17/10 (35 receipts)"
    assert lines[1] == "• ฿10 · Shop 0 · ❓ category ⚠️"
    assert lines[-2:] == ["…and 5 more (see the Sheet)", "Total: ฿350"]
    assert len(lines) == 33


def test_other_messages_are_ignored():
    bot, line = make_bot()
    for event in (text_event("hello"), text_event("สรุป"), text_event("summary please"), sticker_event(),
                  text_event("hello", group="Cgroup"), sticker_event(group="Cgroup")):
        bot.handle(event)
    assert line.sent == []


def test_group_chat_commands_reply_to_group_and_strangers_are_ignored():
    bot, line = complete_bot()
    bot.handle(text_event("summary", group="Cgroup"))
    assert line.sent[-1][0] == "Cgroup" and "Total:" in last_reply(line).text
    sent = len(line.sent)
    bot.handle(text_event("hi", user="Ustranger", group="Cgroup"))
    assert len(line.sent) == sent


def test_postback_payload_fits_line_limit():
    bot, line = complete_bot()
    for b in last_reply(line).buttons + last_reply(line).quick_replies:
        assert len(b.data) <= 300 and len(b.label) <= 20
        assert parse_qs(b.data)["r"] == [bot.sheets.rows[0]["receipt_id"]]


def test_old_ok_and_flag_buttons_still_work():
    bot, line = complete_bot()
    rid = bot.sheets.rows[0]["receipt_id"]
    bot.handle(postback_event(f"a=flag&r={rid}"))
    assert bot.sheets.rows[0]["review_status"] == "flagged"
    bot.handle(postback_event(f"a=ok&r={rid}"))
    assert bot.sheets.rows[0]["review_status"] == "confirmed"


# -- the backfill's OCR flow ----------------------------------------------------------

def ocr_file(bot, content=JPEG):
    return bot.process_file(content, "r.jpg", submitted_by="telegram:x", source="telegram_backfill")


def test_ocr_receipt_is_archived_logged_and_asks_for_category():
    bot, line = ocr_bot()
    out = ocr_file(bot)

    [row] = bot.sheets.rows
    assert row["receipt_date"] == "2026-10-15"
    assert row["grand_total"] == "1,240.00"
    assert row["vat"] == "81.12"
    assert row["net_before_vat"] == "1,158.88"
    assert row["vendor_name"].startswith("บริษัท สยามแม็คโคร")
    for dropped in ("vendor_tax_id", "vendor_phone", "vendor_address", "customer_name", "customer_tax_id",
                    "submitted_by", "doc_type", "invoice_book", "po_no"):
        assert dropped not in row
    assert row["category"] == "" and row["review_status"] == "ok"
    assert len(bot.sheets.items) == 2

    [f] = bot.drive.files.values()
    assert f["folder"] == "2026/10"
    assert f["name"] == "2026-10-15_บริษัท-สยามแม็คโคร-จำกัด-(มหาชน)_1240.00.jpg"
    assert out.status == "logged" and out.needs_category
    assert out.message.text.startswith("฿1,240 · บริษัท สยามแม็คโคร")


def test_ocr_category_is_remembered_for_the_vendor():
    bot, line = ocr_bot()
    rid = ocr_file(bot).receipt_id
    bot.handle(postback_event(f"a=cat&r={rid}&c=FOOD"))
    assert bot.sheets.rows[0]["category"] == "FOOD"
    assert {i["category"] for i in bot.sheets.items} == {"FOOD"}
    [vendor] = bot.sheets.vendor_rows.values()
    assert vendor.tax_id == "0107537000521" and vendor.category == "FOOD"

    bot.ocr.response = receipt(invoice_no="INV0099999", vendor_name="SIAM MAKRO")
    out = ocr_file(bot, JPEG + b"second")
    assert bot.sheets.rows[1]["category"] == "FOOD"
    assert out.message.text == "฿1,240 · SIAM MAKRO · Food cost · 15/10 ✅"
    [vendor] = bot.sheets.vendor_rows.values()
    assert vendor.receipt_count == 2 and "SIAM MAKRO" in vendor.aliases


def test_ocr_validation_problems_set_review_flag():
    bot, line = ocr_bot(ocr=FakeOcr(receipt(vat="10.00")))
    out = ocr_file(bot)
    row = bot.sheets.rows[0]
    assert row["review_status"] == "needs_review" and "VAT" in row["review_note"]
    assert "⚠️" in out.message.text


def test_ocr_same_invoice_different_photo_is_skipped():
    bot, line = ocr_bot()
    ocr_file(bot)
    out = ocr_file(bot, JPEG + b"another photo of the same bill")
    assert len(bot.sheets.rows) == 1 and out.status == "duplicate"
    assert [f["trashed"] for f in bot.drive.files.values()] == [False, True]


def test_ocr_vendor_without_tax_id_learns_it():
    bot, line = ocr_bot()
    bot.sheets.save_vendor(Vendor("name:makro", "", "บริษัท สยามแม็คโคร จำกัด (มหาชน)", category="FOOD"))
    ocr_file(bot)
    [v] = bot.sheets.vendors()
    assert v.tax_id == "0107537000521" and v.receipt_count == 1
    assert bot.sheets.rows[0]["category"] == "FOOD"


def test_ocr_auth_error_keeps_file_and_alerts_owner_then_retry_works():
    ocr = FakeOcr(error=OcrAuthError("Vision API returned 403: billing is disabled"))
    bot, line = ocr_bot(ocr=ocr)
    out = ocr_file(bot)
    assert bot.sheets.rows[0]["review_status"] == "pending_ocr" and len(bot.drive.files) == 1
    assert any(to == OWNER and "403" in m.text for to, m, _ in line.sent)
    assert "/retry" in out.message.text

    ocr.error = None
    ocr.response = receipt()
    bot.handle(text_event("/retry"))
    assert "1 logged" in last_reply(line).text
    row = bot.sheets.rows[0]
    assert row["review_status"] == "ok" and row["grand_total"] == "1,240.00"
    assert next(iter(bot.drive.files.values()))["name"].startswith("2026-10-15_")


def test_ocr_transient_failure_is_pending():
    bot, line = ocr_bot(ocr=FakeOcr(error=OcrError("Vision API returned 503")))
    ocr_file(bot)
    assert bot.sheets.rows[0]["review_status"] == "pending_ocr"
    assert not any(to == OWNER for to, _, _ in line.sent)  # only auth errors alert the owner


def test_ocr_limits_archive_for_manual_entry():
    bot, line = ocr_bot()
    ocr_file(bot, b"%PDF-1.4 " + b"/Type /Page " * 11)
    ocr_file(bot, b"\x00\x00\x00\x18ftypheic....")
    assert bot.ocr.calls == 0 and len(bot.drive.files) == 2
    assert [r["review_status"] for r in bot.sheets.rows] == ["needs_review", "needs_review"]


def test_ocr_misread_supplier_is_fixed_and_learned():
    bot, line = ocr_bot(ocr=FakeOcr(receipt(vendor_name="ร้านหมูอนามัย BETAGR0", vendor_tax_id="0105555000001")))
    bot.sheets.supplier_names = list(SHOPS)
    bot.sheets.save_vendor(Vendor(vendor_key("", BETAGRO), "", BETAGRO, category="FOOD"))
    rid = ocr_file(bot).receipt_id
    bot.handle(postback_event(f"a=chven&r={rid}"))
    tap(bot, line, "หมูอนามัย BETAGRO")

    row = bot.sheets.rows[0]
    assert row["vendor_name"] == BETAGRO and row["category"] == "FOOD"
    assert last_reply(line).text.startswith(f"Saved: ฿1,240 · {BETAGRO} · Food cost")
    [v] = bot.sheets.vendors()  # the entry made from the misreading is gone
    assert v.name == BETAGRO and "ร้านหมูอนามัย BETAGR0" in v.aliases

    bot.ocr.response = receipt(vendor_name="ร้านหมูอนามัย BETAGR0", vendor_tax_id="", invoice_no="INV2")
    ocr_file(bot, JPEG + b"next")
    assert bot.sheets.rows[1]["category"] == "FOOD"
