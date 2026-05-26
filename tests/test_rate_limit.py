"""Tests for src.utils.rate_limit — the per-source error-alert cooldown."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from src.utils import rate_limit


def _now():
    return datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


def _chain(mock_db):
    """Walk the .table().select().eq().like().gte().limit() chain on a MagicMock."""
    return (
        mock_db.table.return_value
        .select.return_value
        .eq.return_value
        .like.return_value
        .gte.return_value
        .limit.return_value
    )


class TestRateLimit:
    def test_alerts_when_no_recent_alert(self, monkeypatch):
        mock_db = MagicMock()
        # SELECT returns no recent [ALERTED] rows.
        _chain(mock_db).execute.return_value.data = []
        notifier = MagicMock()

        sent = rate_limit.maybe_alert_error(mock_db, notifier, "NSE", "boom", _now())
        assert sent is True
        notifier.send_markdown.assert_called_once()
        # Marker row inserted with [ALERTED] prefix.
        mock_db.table.return_value.insert.assert_called_once()
        payload = mock_db.table.return_value.insert.call_args.args[0]
        assert payload["error_msg"].startswith("[ALERTED] ")
        assert payload["source"] == "NSE"
        assert payload["ok"] is False

    def test_suppresses_when_recent_alert_exists(self, monkeypatch):
        mock_db = MagicMock()
        # SELECT finds a recent [ALERTED] row.
        _chain(mock_db).execute.return_value.data = [{"id": 42}]
        notifier = MagicMock()

        sent = rate_limit.maybe_alert_error(mock_db, notifier, "NSE", "still down", _now())
        assert sent is False
        notifier.send_markdown.assert_not_called()
        mock_db.table.return_value.insert.assert_not_called()

    def test_dry_run_with_no_notifier(self):
        mock_db = MagicMock()
        sent = rate_limit.maybe_alert_error(mock_db, None, "NSE", "boom", _now())
        assert sent is False
        # No DB writes in dry-run.
        mock_db.table.return_value.insert.assert_not_called()
        mock_db.table.return_value.select.assert_not_called()

    def test_query_failure_does_not_suppress(self, monkeypatch):
        # On rate-limit query failure, be conservative: still send the alert.
        mock_db = MagicMock()
        _chain(mock_db).execute.side_effect = RuntimeError("db down")
        notifier = MagicMock()
        sent = rate_limit.maybe_alert_error(mock_db, notifier, "BSE", "x", _now())
        assert sent is True
        notifier.send_markdown.assert_called_once()
