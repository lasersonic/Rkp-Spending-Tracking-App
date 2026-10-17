"""Thin wrapper over line-bot-sdk v3: download content, reply, push."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from linebot.v3.messaging import (
    ApiClient,
    ApiException,
    ButtonsTemplate,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PostbackAction,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TemplateMessage,
    TextMessage,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Button:
    label: str  # max 20 chars
    data: str  # postback payload, max 300 chars


@dataclass(frozen=True)
class OutMessage:
    """What the pipeline wants to say, independent of the LINE SDK."""

    text: str
    buttons: tuple[Button, ...] = ()  # persistent buttons under the message (max 4)
    quick_replies: tuple[Button, ...] = ()  # chips above the keyboard (max 13)


def _to_sdk(msg: OutMessage):
    quick = None
    if msg.quick_replies:
        quick = QuickReply(
            items=[
                QuickReplyItem(action=PostbackAction(label=b.label[:20], data=b.data, display_text=b.label))
                for b in msg.quick_replies[:13]
            ]
        )
    if msg.buttons:
        return TemplateMessage(
            alt_text=msg.text[:400],
            template=ButtonsTemplate(
                text=msg.text[:160],
                actions=[
                    PostbackAction(label=b.label[:20], data=b.data, display_text=b.label)
                    for b in msg.buttons[:4]
                ],
            ),
            quick_reply=quick,
        )
    return TextMessage(text=msg.text[:5000], quick_reply=quick)


class LineClient:
    def __init__(self, access_token: str):
        self._config = Configuration(access_token=access_token)

    def download(self, message_id: str) -> bytes:
        with ApiClient(self._config) as api:
            return bytes(MessagingApiBlob(api).get_message_content(message_id))

    def show_loading(self, user_id: str, seconds: int = 30) -> None:
        """Typing indicator in 1:1 chats while OCR runs. Best effort."""
        try:
            with ApiClient(self._config) as api:
                MessagingApi(api).show_loading_animation(
                    ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=seconds)
                )
        except Exception as exc:
            log.debug("loading animation failed: %s", exc)

    def send(self, to: str, msg: OutMessage, reply_token: str | None = None) -> None:
        """Reply with the reply token; push if it has expired or was used."""
        sdk_msg = _to_sdk(msg)
        with ApiClient(self._config) as api:
            messaging = MessagingApi(api)
            if reply_token:
                try:
                    messaging.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=[sdk_msg]))
                    return
                except ApiException as exc:
                    log.info("reply failed (%s), falling back to push", exc.status)
            if to:
                messaging.push_message(PushMessageRequest(to=to, messages=[sdk_msg]))
