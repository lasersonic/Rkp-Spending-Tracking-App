"""LineClient through the real line-bot-sdk, with only the HTTP layer replaced.

Checks the exact URLs and JSON bodies we would send to LINE.
"""

import json

import pytest
import urllib3

from app.line_client import Button, LineClient, OutMessage


class _Resp:
    """Stands in for a urllib3 response."""

    def __init__(self, status, data=b'{"sentMessages": [{"id": "1", "quoteToken": "q"}]}'):
        self.status = status
        self.reason = "OK" if status < 400 else "Bad Request"
        self.data = data
        self.headers = {"content-type": "application/json"}


@pytest.fixture
def http(monkeypatch):
    """Replace only urllib3, so the SDK's own serialization and error handling run."""
    calls, responses = [], []

    def fake_request(self, method, url, body=None, headers=None, **kw):
        calls.append({"method": method, "url": url, "headers": headers or {},
                      "body": json.loads(body) if body else None})
        return responses.pop(0) if responses else _Resp(200)

    monkeypatch.setattr(urllib3.PoolManager, "request", fake_request)
    return calls, responses


def test_reply_with_buttons_and_quick_replies(http):
    calls, _ = http
    msg = OutMessage(
        "฿1,240 · Makro · Food cost · 15/10 ✅",
        buttons=(Button("OK", "a=ok&r=R1"), Button("Change category", "a=chcat&r=R1"), Button("Flag", "a=flag&r=R1")),
        quick_replies=(Button("Food cost", "a=cat&r=R1&c=FOOD"),),
    )
    LineClient("token").send("U1", msg, reply_token="rt")

    [call] = calls
    assert (call["method"], call["url"]) == ("POST", "https://api.line.me/v2/bot/message/reply")
    assert call["headers"]["Authorization"] == "Bearer token"
    body = call["body"]
    [m] = body["messages"]
    assert body["replyToken"] == "rt"
    assert m["type"] == "template" and m["altText"].startswith("฿1,240")
    assert m["template"]["type"] == "buttons"
    assert [a["label"] for a in m["template"]["actions"]] == ["OK", "Change category", "Flag"]
    assert m["template"]["actions"][0] == {"type": "postback", "label": "OK", "data": "a=ok&r=R1", "displayText": "OK"}
    assert m["quickReply"]["items"][0]["action"]["data"] == "a=cat&r=R1&c=FOOD"


def test_expired_reply_token_falls_back_to_push(http):
    calls, responses = http
    responses.append(_Resp(400, b'{"message":"Invalid reply token"}'))
    LineClient("token").send("U1", OutMessage("hello"), reply_token="old")
    assert [c["url"] for c in calls] == [
        "https://api.line.me/v2/bot/message/reply",
        "https://api.line.me/v2/bot/message/push",
    ]
    body = calls[1]["body"]
    assert body["to"] == "U1" and body["messages"] == [{"type": "text", "text": "hello"}]


def test_download_uses_content_api(http):
    calls, responses = http
    responses.append(_Resp(200, b"\xff\xd8\xffJPEG"))
    assert LineClient("token").download("12345") == b"\xff\xd8\xffJPEG"
    assert calls[0]["url"] == "https://api-data.line.me/v2/bot/message/12345/content"


def test_loading_animation(http):
    calls, _ = http
    LineClient("token").show_loading("U1")
    body = calls[0]["body"]
    assert calls[0]["url"] == "https://api.line.me/v2/bot/chat/loading/start"
    assert body == {"chatId": "U1", "loadingSeconds": 30}  # must be a multiple of 5, max 60
