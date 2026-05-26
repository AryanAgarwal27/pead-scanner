"""Tests for the multi-source detector — failover, dedup, latency logging."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from src.pipeline import detector
from src.sources.base import Filing


def _f(source, symbol, quarter="Q4-FY26"):
    return Filing(
        source=source,
        symbol=symbol,
        company_name=f"{source}-{symbol}",
        quarter=quarter,
        quarter_source="headline",
        filing_time=datetime(2026, 5, 14, 10, tzinfo=UTC),
        filing_url=None,
        is_consolidated=None,
        raw_payload={},
    )


@pytest.fixture
def mocks(monkeypatch):
    mock_db = MagicMock()
    monkeypatch.setattr(detector, "maybe_alert_error", MagicMock())
    return mock_db, detector.maybe_alert_error


class _StubSource:
    def __init__(self, name, filings=None, exc=None):
        self.name = name
        self._filings = filings or []
        self._exc = exc

    def fetch(self, target_date):
        if self._exc is not None:
            raise self._exc
        return list(self._filings)


def _patch_sources(monkeypatch, nse_src, bse_src, tl_src):
    monkeypatch.setattr(detector, "NseSource", lambda: nse_src)
    monkeypatch.setattr(detector, "BseSource", lambda: bse_src)
    monkeypatch.setattr(detector, "TrendlyneSource", lambda: tl_src)


class TestParallelPrimaries:
    def test_both_primaries_succeed_no_trendlyne(self, monkeypatch, mocks):
        mock_db, alert_mock = mocks
        nse = _StubSource("NSE", [_f("NSE", "HDFCBANK"), _f("NSE", "RELIANCE")])
        bse = _StubSource("BSE", [_f("BSE", "500180")])
        tl = _StubSource("TRENDLYNE", [_f("TRENDLYNE", "TL-foo")])
        _patch_sources(monkeypatch, nse, bse, tl)

        out = detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        symbols = sorted((f.source, f.symbol) for f in out)
        assert symbols == [("BSE", "500180"), ("NSE", "HDFCBANK"), ("NSE", "RELIANCE")]
        # Trendlyne NOT called when both primaries succeeded.
        # source_health writes happen for NSE + BSE only (2 inserts), no trendlyne.
        sh_inserts = [
            c for c in mock_db.table.return_value.insert.call_args_list
            if c.args and c.args[0].get("source") in ("NSE", "BSE", "TRENDLYNE")
        ]
        assert {c.args[0]["source"] for c in sh_inserts} == {"NSE", "BSE"}
        # No alerts triggered (everyone succeeded).
        alert_mock.assert_not_called()


class TestFailoverChain:
    def test_nse_fails_bse_succeeds_no_trendlyne(self, monkeypatch, mocks):
        mock_db, alert_mock = mocks
        nse = _StubSource("NSE", exc=ConnectionError("boom"))
        bse = _StubSource("BSE", [_f("BSE", "500325")])
        tl = _StubSource("TRENDLYNE", [_f("TRENDLYNE", "TL-x")])
        _patch_sources(monkeypatch, nse, bse, tl)

        out = detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        # BSE result returned; trendlyne NOT consulted (at least one primary worked).
        assert [(f.source, f.symbol) for f in out] == [("BSE", "500325")]
        # Alert fired for NSE.
        alert_mock.assert_called_once()
        assert alert_mock.call_args.args[2] == "NSE"

    def test_both_primaries_fail_trendlyne_serves(self, monkeypatch, mocks):
        mock_db, alert_mock = mocks
        nse = _StubSource("NSE", exc=ConnectionError("nse down"))
        bse = _StubSource("BSE", exc=TimeoutError("bse slow"))
        tl = _StubSource("TRENDLYNE", [_f("TRENDLYNE", "TL-y")])
        _patch_sources(monkeypatch, nse, bse, tl)

        out = detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        assert [(f.source, f.symbol) for f in out] == [("TRENDLYNE", "TL-y")]
        # Alerts fired for both NSE and BSE.
        assert alert_mock.call_count == 2

    def test_all_three_fail_returns_empty(self, monkeypatch, mocks):
        mock_db, alert_mock = mocks
        nse = _StubSource("NSE", exc=ConnectionError("a"))
        bse = _StubSource("BSE", exc=ConnectionError("b"))
        tl = _StubSource("TRENDLYNE", exc=ConnectionError("c"))
        _patch_sources(monkeypatch, nse, bse, tl)

        out = detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        assert out == []
        assert alert_mock.call_count == 3


class TestDedup:
    def test_within_source_dedup_collapses(self, monkeypatch, mocks):
        mock_db, _ = mocks
        # NSE returns the same (symbol, quarter) twice — should collapse to one.
        nse = _StubSource("NSE", [_f("NSE", "FOO"), _f("NSE", "FOO")])
        bse = _StubSource("BSE", [])
        tl = _StubSource("TRENDLYNE", [])
        _patch_sources(monkeypatch, nse, bse, tl)

        out = detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        assert len(out) == 1

    def test_cross_source_duplicates_kept(self, monkeypatch, mocks):
        # NSE "HDFCBANK" and BSE "500180" are the same company. Detector does NOT
        # dedup across sources (Phase 2 Q3 decision).
        mock_db, _ = mocks
        nse = _StubSource("NSE", [_f("NSE", "HDFCBANK")])
        bse = _StubSource("BSE", [_f("BSE", "500180")])
        tl = _StubSource("TRENDLYNE", [])
        _patch_sources(monkeypatch, nse, bse, tl)

        out = detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        assert len(out) == 2


class TestLatencyLogging:
    def test_source_health_carries_latency_marker(self, monkeypatch, mocks):
        mock_db, _ = mocks
        nse = _StubSource("NSE", [_f("NSE", "X")])
        bse = _StubSource("BSE", [])
        tl = _StubSource("TRENDLYNE", [])
        _patch_sources(monkeypatch, nse, bse, tl)

        detector.detect_filings(mock_db, MagicMock(), date(2026, 5, 14))
        # Find the NSE source_health insert and confirm error_msg has latency_ms.
        nse_insert = next(
            c for c in mock_db.table.return_value.insert.call_args_list
            if c.args and c.args[0].get("source") == "NSE"
        )
        payload = nse_insert.args[0]
        assert payload["ok"] is True
        assert "latency_ms=" in payload["error_msg"]
