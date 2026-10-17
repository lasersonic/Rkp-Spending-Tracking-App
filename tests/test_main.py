import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings

SECRET = "test-secret"


@pytest.fixture
def client(monkeypatch):
    handled = []

    class FakeBot:
        def handle(self, event):
            handled.append(event)

    monkeypatch.setattr(main, "get_settings", lambda: Settings(line_channel_secret=SECRET))
    main.get_parser.cache_clear()
    monkeypatch.setattr(main, "get_bot", lambda: FakeBot())
    yield TestClient(main.app), handled
    main.get_parser.cache_clear()


def sign(body: str) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode()


BODY = json.dumps({
    "destination": "x",
    "events": [{
        "type": "message", "mode": "active", "timestamp": 0, "webhookEventId": "w",
        "deliveryContext": {"isRedelivery": False}, "replyToken": "rt",
        "source": {"type": "user", "userId": "U1"},
        "message": {"type": "text", "id": "1", "text": "help", "quoteToken": "q"},
    }],
})


def test_valid_signature_is_accepted_and_processed(client):
    c, handled = client
    resp = c.post("/callback", content=BODY, headers={"X-Line-Signature": sign(BODY)})
    assert resp.status_code == 200
    assert len(handled) == 1


def test_bad_signature_is_rejected(client):
    c, handled = client
    resp = c.post("/callback", content=BODY, headers={"X-Line-Signature": "bogus"})
    assert resp.status_code == 400
    assert handled == []


def test_webhook_verify_with_no_events(client):
    c, handled = client
    body = json.dumps({"destination": "x", "events": []})
    assert c.post("/callback", content=body, headers={"X-Line-Signature": sign(body)}).status_code == 200


def test_health(client):
    c, _ = client
    assert c.get("/").json() == {"status": "ok"}
