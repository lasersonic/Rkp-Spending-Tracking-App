"""FastAPI webhook for the LINE Official Account.

LINE expects a fast 200, so events are processed in a background task after
the response is sent. On Cloud Run, deploy with --no-cpu-throttling so that
background work keeps its CPU (see README).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError

from .config import get_settings
from .iapp import IappClient
from .line_client import LineClient
from .pipeline import Bot
from .storage import DriveStore, SheetStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Restaurant Expense Bot", docs_url=None, redoc_url=None)


@lru_cache
def get_parser() -> WebhookParser:
    return WebhookParser(get_settings().line_channel_secret)


@lru_cache
def get_bot() -> Bot:
    s = get_settings()
    sheets = SheetStore.from_service_account(s.google_sa_info, s.sheet_id)
    sheets.ensure_schema()
    return Bot(
        settings=s,
        line=LineClient(s.line_channel_access_token),
        sheets=sheets,
        drive=DriveStore.from_settings(s.google_sa_info, s.google_drive_token_info, s.drive_folder_id),
        ocr=IappClient(s.iapp_api_key, s.iapp_url),
    )


def process_events(events: list) -> None:
    bot = get_bot()
    for event in events:
        try:
            bot.handle(event)
        except Exception:
            log.exception("unhandled error for %s event", getattr(event, "type", "?"))


@app.get("/")
def health() -> dict:
    return {"status": "ok"}


@app.post("/callback")
async def callback(
    request: Request,
    background: BackgroundTasks,
    x_line_signature: str = Header(default=""),
) -> dict:
    body = (await request.body()).decode("utf-8")
    try:
        events = get_parser().parse(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="invalid signature")
    if events:
        background.add_task(process_events, events)
    return {"status": "ok"}
