from datetime import date
from decimal import Decimal

import pytest

from app.normalize import LineItem, ReceiptData, parse_amount, parse_date, parse_number, validate

TODAY = date(2026, 10, 17)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("27/11/2566", date(2023, 11, 27)),  # spec example
        ("27-11-2566", date(2023, 11, 27)),
        ("27.11.2566", date(2023, 11, 27)),
        ("2566-11-27", date(2023, 11, 27)),
        ("2023-11-27", date(2023, 11, 27)),
        ("27/11/2023", date(2023, 11, 27)),
        ("๒๗/๑๑/๒๕๖๖", date(2023, 11, 27)),  # Thai digits
        ("27 พ.ย. 2566", date(2023, 11, 27)),
        ("27 พฤศจิกายน 2566", date(2023, 11, 27)),
        ("27 พ.ย. 66", date(2023, 11, 27)),
        ("27 Nov 2023", date(2023, 11, 27)),
        ("15/10/69", date(2026, 10, 15)),  # two-digit BE year
        ("15/10/26", date(2026, 10, 15)),  # two-digit CE year
        ("วันที่ 15/10/2569 เวลา 10:22", date(2026, 10, 15)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw, TODAY) == expected


@pytest.mark.parametrize("raw", [None, "", "no date", "31/02/2566"])
def test_parse_date_invalid(raw):
    assert parse_date(raw, TODAY) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1,240.50", Decimal("1240.50")),
        ("฿1,240.5", Decimal("1240.50")),
        ("1240 บาท", Decimal("1240.00")),
        (1240.555, Decimal("1240.56")),
        (1240, Decimal("1240.00")),
        ("(50.00)", Decimal("-50.00")),
        ("๑,๒๔๐.๐๐", Decimal("1240.00")),
        ("", None),
        ("-", None),
        (None, None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_parse_number_keeps_three_decimals():
    assert parse_number("1.525") == Decimal("1.525")
    assert parse_number("2") == Decimal("2")


def good_receipt(**kw) -> ReceiptData:
    base = dict(
        receipt_date=date(2026, 10, 15), vendor_name="Makro", subtotal=Decimal("1240.00"),
        discount=Decimal("0"), after_discount=Decimal("1240.00"), vat=Decimal("81.12"),
        grand_total=Decimal("1240.00"),
        items=[LineItem(1, total_cost=Decimal("600")), LineItem(2, total_cost=Decimal("640"))],
        min_confidence=0.95,
    )
    base.update(kw)
    return ReceiptData(**base)


def test_valid_receipt_has_no_problems():
    assert validate(good_receipt(), TODAY) == []


def test_net_before_vat():
    assert good_receipt().net_before_vat == Decimal("1158.88")


def test_vat_on_net_is_accepted():
    # VAT added on top: net 1000 + 70 VAT = 1070
    r = good_receipt(subtotal=Decimal("1000"), after_discount=Decimal("1000"), vat=Decimal("70"),
                     grand_total=Decimal("1070"), items=[])
    assert validate(r, TODAY) == []


def test_no_vat_receipt_is_fine():
    assert validate(good_receipt(vat=None), TODAY) == []


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"items": [LineItem(1, total_cost=Decimal("100"))]}, "line items sum"),
        ({"after_discount": Decimal("1100")}, "subtotal − discount"),
        ({"vat": Decimal("50")}, "VAT"),
        ({"receipt_date": date(2026, 11, 1)}, "future"),
        ({"receipt_date": date(2025, 9, 1)}, "over 12 months"),
        ({"receipt_date": None}, "missing date"),
        ({"grand_total": None}, "missing grand total"),
        ({"vendor_name": ""}, "missing vendor"),
        ({"min_confidence": 0.6, "low_confidence_fields": ["grandTotal"]}, "low OCR confidence"),
    ],
)
def test_validation_failures(overrides, fragment):
    problems = validate(good_receipt(**overrides), TODAY)
    assert any(fragment in p for p in problems), problems


def test_one_baht_tolerance_on_items():
    r = good_receipt(items=[LineItem(1, total_cost=Decimal("600.50")), LineItem(2, total_cost=Decimal("640.40"))])
    assert validate(r, TODAY) == []
