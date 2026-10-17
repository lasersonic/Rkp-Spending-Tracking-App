from datetime import date, datetime
from decimal import Decimal

from app.categorize import Vendor, find_vendor, normalize_vendor_name, short_name, DEFAULT_CATEGORIES
from app.dashboard import BLOCKS
from app.storage import RECEIPT_COLUMNS, col_letter, drive_file_id, to_cell


def test_vendor_name_normalization_ignores_company_forms():
    a = normalize_vendor_name("บริษัท สยามแม็คโคร จำกัด (มหาชน)")
    b = normalize_vendor_name("สยามแม็คโคร จำกัด (มหาชน) สาขาลาดพร้าว")
    assert a == b == "สยามแม็คโคร"
    assert normalize_vendor_name("Siam Makro Public Co., Ltd.") == normalize_vendor_name("SIAM MAKRO PUBLIC")


def test_find_vendor_by_tax_id_then_name():
    vendors = [
        Vendor("tax:0107537000521", "0107537000521", "Makro", category="FOOD"),
        Vendor("name:ตลาดไท", "", "ตลาดไท", aliases=["Talaad Thai"], category="FOOD"),
    ]
    assert find_vendor(vendors, "0107537000521", "anything").name == "Makro"
    assert find_vendor(vendors, "", "talaad thai").name == "ตลาดไท"
    assert find_vendor(vendors, "", "Unknown shop") is None


def test_category_labels_fit_line_limit():
    assert all(len(short_name(c)) <= 20 for c in DEFAULT_CATEGORIES)
    assert len(DEFAULT_CATEGORIES) <= 13  # LINE quick reply maximum


def test_to_cell_keeps_text_as_text():
    assert to_cell("vendor_tax_id", "0107537000521") == "'0107537000521"
    assert to_cell("vendor_name", "=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert to_cell("receipt_date", date(2023, 11, 27)) == "2023-11-27"
    assert to_cell("received_at", datetime(2026, 10, 17, 9, 5)) == "2026-10-17 09:05:00"
    assert to_cell("grand_total", Decimal("1240.50")) == 1240.5
    assert to_cell("grand_total", None) == ""


def test_col_letter():
    assert [col_letter(i) for i in (0, 25, 26, 28)] == ["A", "Z", "AA", "AC"]


def test_drive_file_id():
    assert drive_file_id("https://drive.google.com/file/d/abc-123_X/view?usp=drivesdk") == "abc-123_X"
    assert drive_file_id("https://drive.google.com/open?id=xyz") == "xyz"
    assert drive_file_id("") is None


def test_dashboard_formulas_reference_the_right_columns():
    formulas = {title: f for _, title, f in BLOCKS}
    spend = formulas["Monthly spend by category (฿)"]
    def col(name):
        letter = col_letter(RECEIPT_COLUMNS.index(name))
        return f"Receipts!{letter}2:{letter}"

    for name in ("receipt_date", "category", "grand_total"):
        assert col(name) in spend
    vat = formulas["Input VAT by month (for ภ.พ.30)"]
    assert col("net_before_vat") in vat and col("vat") in vat
    assert all(f.startswith("=QUERY(") and f.count("(") == f.count(")") for f in formulas.values())
