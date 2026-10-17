from datetime import date
from decimal import Decimal

import httpx
import pytest

from app import iapp
from app.iapp import IappAuthError, IappClient, IappError, parse_response
from tests.fakes import iapp_response

TODAY = date(2026, 10, 17)


def test_maps_fields_to_columns():
    r = parse_response(iapp_response(), TODAY)
    assert r.invoice_no == "INV0012345"
    assert r.receipt_date == date(2026, 10, 15)  # 15/10/2569 BE
    assert r.vendor_name.startswith("บริษัท สยามแม็คโคร")
    assert r.vendor_tax_id == "0107537000521"
    assert r.subtotal == Decimal("1240.00")
    assert r.vat == Decimal("81.12")
    assert r.grand_total == Decimal("1240.00")
    assert r.net_before_vat == Decimal("1158.88")
    assert [(i.item_no, i.item_name, i.qty, i.total_cost) for i in r.items] == [
        (1, "หมูสามชั้น", Decimal("2"), Decimal("600.00")),
        (2, "ผักกาดขาว", Decimal("4"), Decimal("640.00")),
    ]
    assert r.min_confidence == 0.93


def test_plain_values_and_confidence_map():
    data = {
        "invoiceDate": "27/11/2566",
        "issuerName": "Makro",
        "grandTotal": 535,
        "vat": 35,
        "confidence": {"grandTotal": 0.8, "issuerName": 0.99},
        "items": [],
    }
    r = parse_response(data, TODAY)
    assert r.receipt_date == date(2023, 11, 27)
    assert r.grand_total == Decimal("535.00")
    assert r.min_confidence == 0.8
    assert r.low_confidence_fields == ["grandTotal"]


def test_percentage_confidence_is_scaled():
    r = parse_response({"grandTotal": {"value": "10", "confidence": 72}}, TODAY)
    assert r.min_confidence == 0.72


def test_multi_page_pdf_merges_items():
    pages = [
        {"issuerName": "Makro", "items": [{"itemName": "A", "totalCost": "10"}]},
        {"issuerName": "Makro", "grandTotal": "30", "items": [{"itemName": "B", "totalCost": "20"}]},
    ]
    r = parse_response({"result": pages}, TODAY)
    assert r.grand_total == Decimal("30.00")
    assert [i.item_name for i in r.items] == ["A", "B"]
    assert [i.item_no for i in r.items] == [1, 2]


def test_bad_shape_raises():
    with pytest.raises(IappError):
        parse_response("oops", TODAY)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


def _client(monkeypatch, statuses):
    calls = []

    def fake_post(url, headers, files, timeout):
        calls.append(headers)
        status = statuses.pop(0)
        if isinstance(status, Exception):
            raise status
        return _Resp(status, {"ok": True})

    monkeypatch.setattr(iapp.httpx, "post", fake_post)
    return IappClient("key", "https://example.test/ocr", sleep=lambda s: None), calls


def test_sends_apikey_header(monkeypatch):
    client, calls = _client(monkeypatch, [200])
    assert client.ocr(b"x", "a.jpg", "image/jpeg") == {"ok": True}
    assert calls == [{"apikey": "key"}]


def test_retries_twice_on_429_and_5xx(monkeypatch):
    client, calls = _client(monkeypatch, [429, httpx.ConnectError("boom"), 200])
    assert client.ocr(b"x", "a.jpg", "image/jpeg") == {"ok": True}
    assert len(calls) == 3


def test_gives_up_after_two_retries(monkeypatch):
    client, calls = _client(monkeypatch, [503, 503, 503])
    with pytest.raises(IappError):
        client.ocr(b"x", "a.jpg", "image/jpeg")
    assert len(calls) == 3


@pytest.mark.parametrize("status", [401, 402])
def test_auth_errors_are_not_retried(monkeypatch, status):
    client, calls = _client(monkeypatch, [status])
    with pytest.raises(IappAuthError):
        client.ocr(b"x", "a.jpg", "image/jpeg")
    assert len(calls) == 1
