"""TelegramNotifier — URL construction + secret hygiene.

Regression coverage for the CI 404: a trailing newline on TELEGRAM_BOT_TOKEN
(common when a token is pasted into a GitHub secret) was URL-encoded to '%0A'
in the request path, which Telegram answers with HTTP 404.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.notify.telegram import TelegramNotifier


class TestTokenHygiene:
    def test_trailing_newline_stripped_from_url(self) -> None:
        n = TelegramNotifier(bot_token="123:ABC\n", chat_id="555")
        assert "%0A" not in n._url
        assert "\n" not in n._url
        assert n._url == "https://api.telegram.org/bot123:ABC/sendMessage"

    def test_surrounding_whitespace_stripped(self) -> None:
        n = TelegramNotifier(bot_token="  123:ABC  ", chat_id="  555\n")
        assert n._url == "https://api.telegram.org/bot123:ABC/sendMessage"
        assert n._chat_id == "555"

    def test_non_str_chat_id_coerced(self) -> None:
        # supabase / env can hand back a non-str; .strip() must not explode.
        n = TelegramNotifier(bot_token="123:ABC", chat_id=555)  # type: ignore[arg-type]
        assert n._chat_id == "555"

    def test_missing_token_raises(self, monkeypatch) -> None:
        # Empty arg falls back to config; force config empty too so the guard fires.
        monkeypatch.setattr("src.notify.telegram.config.TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            TelegramNotifier(bot_token="", chat_id="555")

    def test_clean_token_sends_to_stripped_url(self) -> None:
        n = TelegramNotifier(bot_token="123:ABC\n", chat_id="555")
        with patch("src.notify.telegram.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            n.send_markdown("hello")
        called_url = post.call_args.args[0]
        assert "%0A" not in called_url
        assert called_url == "https://api.telegram.org/bot123:ABC/sendMessage"
