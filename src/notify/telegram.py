"""Telegram bot sender — Phase 1.

Minimal: POST sendMessage with parse_mode=Markdown (legacy). Raises on non-2xx
so the caller can leave alerted_at unset and retry next run (idempotency).
"""

import requests

from src import config
from src.utils.logging import get_logger
from src.utils.retry import with_retries

log = get_logger(__name__)

TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        token = bot_token or config.TELEGRAM_BOT_TOKEN
        chat = chat_id or config.TELEGRAM_CHAT_ID
        # Strip surrounding whitespace/newlines: a trailing '\n' on the secret
        # (common when a token is pasted into a GitHub secret) is URL-encoded to
        # '%0A' in the request path, which Telegram answers with a 404. Coerce
        # to str first so a non-str chat_id from the environment can't break
        # .strip(); both are placed into a request (token in the URL path, chat
        # in the JSON body) and must be clean.
        token = str(token).strip() if token else token
        chat = str(chat).strip() if chat else chat
        if not token or not chat:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        self._url = TELEGRAM_API_TEMPLATE.format(token=token)
        self._chat_id = chat

    def send_markdown(self, text: str) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        def _do_post() -> requests.Response:
            return requests.post(self._url, json=payload, timeout=15)

        resp = with_retries(_do_post)
        if resp.status_code >= 400:
            log.error(f"Telegram send failed status={resp.status_code} body={resp.text[:200]}")
            resp.raise_for_status()
