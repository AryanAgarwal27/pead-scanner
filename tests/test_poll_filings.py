"""Tests for the jobs.poll_filings orchestrator — focuses on --dry-run."""

import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from jobs import poll_filings
from src.sources.base import Filing


def _make_filing(source: str, symbol: str) -> Filing:
    return Filing(
        source=source,
        symbol=symbol,
        company_name=f"{source}-{symbol}",
        quarter="Q1-FY27",
        quarter_source="headline",
        filing_time=datetime(2026, 5, 14, 10, tzinfo=UTC),
        filing_url=None,
        is_consolidated=None,
        raw_payload={},
    )


@pytest.fixture
def wired(monkeypatch):
    """Wire mocks for DB client, detector, and TelegramNotifier.

    Phase 2 changed the orchestrator to call src.pipeline.detector.detect_filings
    (which fans out to NSE + BSE + Trendlyne). We stub that out to return
    controlled filings so the dry-run path is exercised without network.
    """
    mock_db = MagicMock()
    # Dedup SELECT returns no pre-existing rows → every fetched filing would alert.
    sel_chain = (
        mock_db.table.return_value
        .select.return_value
        .in_.return_value
        .in_.return_value
    )
    sel_chain.execute.return_value.data = []
    monkeypatch.setattr(poll_filings, "get_client", lambda: mock_db)

    mock_notifier_cls = MagicMock()
    monkeypatch.setattr(poll_filings, "TelegramNotifier", mock_notifier_cls)

    fake_filings = [_make_filing("NSE", "FOO"), _make_filing("BSE", "100")]
    # detect_filings signature: (db, notifier, target_date) -> list[Filing]
    monkeypatch.setattr(
        poll_filings, "detect_filings", lambda db, notifier, d: fake_filings
    )

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

        # source_health row IS written by the orchestrator with error_msg='dry_run'
        # marker. (Detector writes per-source rows separately; we stub that out.)
        insert_calls = mock_db.table.return_value.insert.call_args_list
        dry_run_inserts = [
            c for c in insert_calls
            if c.args and c.args[0].get("error_msg") == "dry_run"
        ]
        assert len(dry_run_inserts) == 1
        payload = dry_run_inserts[0].args[0]
        assert payload["ok"] is True
        assert payload["records_found"] == 2
        # Phase 2: orchestrator row uses source="POLL" to distinguish from
        # per-source rows that the detector writes.
        assert payload["source"] == "POLL"

        # Structured summary printed.
        out = capsys.readouterr().out
        assert "DRY RUN SUMMARY" in out
        assert "Date: 2026-05-14" in out
        assert "Filings (all sources, post-detector): 2" in out
        assert "NSE" in out and "BSE" in out
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
