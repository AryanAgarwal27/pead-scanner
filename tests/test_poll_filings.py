"""Tests for the jobs.poll_filings orchestrator — focuses on --dry-run."""

import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from jobs import poll_filings
from src.sources.bse import BseFiling


def _make_filing(symbol: str) -> BseFiling:
    return BseFiling(
        symbol=symbol,
        company_name=f"Co {symbol}",
        quarter="Q1-FY27",
        quarter_source="headline",
        filing_time=datetime(2026, 5, 14, 10, tzinfo=UTC),
        filing_url=None,
        is_consolidated=None,
        raw_payload={},
    )


@pytest.fixture
def wired(monkeypatch):
    """Wire mocks for DB client, BSE fetch, and TelegramNotifier."""
    mock_db = MagicMock()
    # Dedup SELECT returns no pre-existing rows → every fetched filing would alert.
    (
        mock_db.table.return_value.select.return_value.in_.return_value.in_.return_value
        .execute.return_value.data
    ) = []
    monkeypatch.setattr(poll_filings, "get_client", lambda: mock_db)

    mock_notifier_cls = MagicMock()
    monkeypatch.setattr(poll_filings, "TelegramNotifier", mock_notifier_cls)

    fake_filings = [_make_filing("100"), _make_filing("200")]
    monkeypatch.setattr(poll_filings, "fetch_today_results", lambda d: fake_filings)

    return mock_db, mock_notifier_cls, fake_filings


def _run(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["poll_filings.py", *args])
    return poll_filings.main()


class TestDryRun:
    def test_dry_run_skips_db_writes_to_filings(self, monkeypatch, wired, capsys):
        mock_db, _, _ = wired
        rc = _run(monkeypatch, "--date", "2026-05-14", "--dry-run")
        assert rc == 0

        # The filings table must NOT be written to.
        mock_db.table.return_value.upsert.assert_not_called()
        mock_db.table.return_value.update.assert_not_called()

        # source_health row IS written, with error_msg='dry_run' marker.
        mock_db.table.return_value.insert.assert_called_once()
        payload = mock_db.table.return_value.insert.call_args.args[0]
        assert payload["error_msg"] == "dry_run"
        assert payload["ok"] is True
        assert payload["records_found"] == 2
        assert payload["source"] == "BSE"

        # Structured summary printed.
        out = capsys.readouterr().out
        assert "DRY RUN SUMMARY" in out
        assert "Date: 2026-05-14" in out
        assert "BSE filings (after SUBCATNAME filter): 2" in out
        assert "Would alert (after dedup): 2" in out
        assert "--- Message 1 ---" in out

    def test_dry_run_skips_telegram(self, monkeypatch, wired):
        _, mock_notifier_cls, _ = wired
        rc = _run(monkeypatch, "--date", "2026-05-14", "--dry-run")
        assert rc == 0

        # In dry-run we never even construct the notifier.
        mock_notifier_cls.assert_not_called()
        # Belt-and-braces: send_markdown was never called either.
        mock_notifier_cls.return_value.send_markdown.assert_not_called()
