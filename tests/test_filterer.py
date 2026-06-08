"""Unit tests for src.pipeline.filterer — Phase 4.

DB and yfinance are mocked. Test focus is the filter-chain logic, the
fail-open behavior on listing-age yfinance failures, and the lazy
write-back semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.pipeline import banlists
from src.pipeline import filterer as F

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_banlist_cache() -> None:
    banlists.clear_cache()


@pytest.fixture
def fake_db() -> MagicMock:
    """A MagicMock that supports the .table(x).update(y).eq(a,b).execute() chain."""
    db = MagicMock()
    chain = MagicMock()
    db.table.return_value = chain
    chain.update.return_value = chain
    chain.eq.return_value = chain
    chain.execute.return_value = MagicMock(data=[])
    return db


def _good_row(**overrides) -> dict:
    """Cohort row that passes all in-memory filters by default. yfinance still
    runs because metrics.avg_30d_turnover_cr starts as None."""
    base = {
        "filing_id": 100,
        "symbol": "HDFCBANK",
        "source": "NSE",
        "quarter": "Q3-FY26",
        "filing_time": datetime(2026, 5, 26, 14, 0, tzinfo=UTC).isoformat(),
        "parser_confidence": "high",
        "has_exceptional_items": False,
        "metrics": {"avg_30d_turnover_cr": 100.0},
        "fundamentals": {"market_cap_cr": 1500.0, "listed_long_enough": True},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# In-memory filters — no yfinance contact
# ---------------------------------------------------------------------------


class TestInMemoryFilters:
    def test_passes_when_all_filters_satisfied(self, fake_db: MagicMock) -> None:
        outcomes = F.filter_cohort(fake_db, [_good_row()])
        assert outcomes[0].passed is True
        assert outcomes[0].drop_reason is None
        # Cached values already populated → no yfinance write attempts.
        assert outcomes[0].turnover_was_computed is False
        assert outcomes[0].listing_check_was_computed is False

    @pytest.mark.parametrize("conf", ["low", "failed", None])
    def test_drops_on_confidence_floor(self, fake_db: MagicMock, conf) -> None:
        outcomes = F.filter_cohort(fake_db, [_good_row(parser_confidence=conf)])
        assert outcomes[0].passed is False
        assert outcomes[0].drop_reason == F.DROP_CONFIDENCE

    def test_admits_medium_confidence(self, fake_db: MagicMock) -> None:
        outcomes = F.filter_cohort(fake_db, [_good_row(parser_confidence="medium")])
        assert outcomes[0].passed is True

    def test_drops_on_exceptional_items(self, fake_db: MagicMock) -> None:
        outcomes = F.filter_cohort(fake_db, [_good_row(has_exceptional_items=True)])
        assert outcomes[0].drop_reason == F.DROP_EXCEPTIONAL

    def test_drops_on_low_market_cap(self, fake_db: MagicMock) -> None:
        row = _good_row()
        row["fundamentals"]["market_cap_cr"] = 100.0  # below ₹500 Cr
        outcomes = F.filter_cohort(fake_db, [row])
        assert outcomes[0].drop_reason == F.DROP_MARKET_CAP

    def test_drops_when_fundamentals_missing(self, fake_db: MagicMock) -> None:
        outcomes = F.filter_cohort(fake_db, [_good_row(fundamentals=None)])
        assert outcomes[0].drop_reason == F.DROP_MARKET_CAP

    def test_drops_bse_only_on_market_cap_path(self, fake_db: MagicMock) -> None:
        """BSE filing with no NSE-ticker resolution → can't be in ban lists,
        gets dropped on the market-cap step instead."""
        row = _good_row(source="BSE", symbol="999999", fundamentals=None)
        outcomes = F.filter_cohort(fake_db, [row])
        assert outcomes[0].drop_reason == F.DROP_MARKET_CAP
        assert outcomes[0].symbol_nse is None

    def test_drops_on_low_turnover(self, fake_db: MagicMock) -> None:
        row = _good_row()
        row["metrics"]["avg_30d_turnover_cr"] = 1.0  # below ₹5 Cr floor
        outcomes = F.filter_cohort(fake_db, [row])
        assert outcomes[0].drop_reason == F.DROP_TURNOVER

    def test_drops_when_listing_age_false_cached(self, fake_db: MagicMock) -> None:
        row = _good_row()
        row["fundamentals"]["listed_long_enough"] = False
        outcomes = F.filter_cohort(fake_db, [row])
        assert outcomes[0].drop_reason == F.DROP_LISTING_AGE


# ---------------------------------------------------------------------------
# Ban-list filters with CSV-backed sets
# ---------------------------------------------------------------------------


class TestBanlists:
    def test_drops_on_fno_ban(self, fake_db: MagicMock, monkeypatch) -> None:
        monkeypatch.setattr(banlists, "fno_ban_symbols", lambda: frozenset({"HDFCBANK"}))
        monkeypatch.setattr(banlists, "asm_gsm_symbols", lambda: frozenset())
        outcomes = F.filter_cohort(fake_db, [_good_row()])
        assert outcomes[0].drop_reason == F.DROP_FNO_BAN

    def test_drops_on_asm_gsm(self, fake_db: MagicMock, monkeypatch) -> None:
        monkeypatch.setattr(banlists, "fno_ban_symbols", lambda: frozenset())
        monkeypatch.setattr(banlists, "asm_gsm_symbols", lambda: frozenset({"HDFCBANK"}))
        outcomes = F.filter_cohort(fake_db, [_good_row()])
        assert outcomes[0].drop_reason == F.DROP_ASM_GSM


# ---------------------------------------------------------------------------
# yfinance probe — turnover + listing age
# ---------------------------------------------------------------------------


class TestYFinanceProbe:
    def test_probe_runs_when_turnover_missing(
        self, fake_db: MagicMock, monkeypatch
    ) -> None:
        """When metrics.avg_30d_turnover_cr is NULL, filterer must call yfinance,
        compute the value, and write it back to the metrics row."""
        # Build a 60-day OHLCV frame with constant Close=100, Volume=1e6.
        # Daily turnover = 100*1e6/1e7 = 10 Cr → above the ₹5 Cr floor.
        idx = pd.date_range("2026-03-01", periods=60, freq="B")
        df = pd.DataFrame({"Close": [100.0] * 60, "Volume": [1_000_000] * 60}, index=idx)
        from src.sources import yfinance_adapter as yfa
        monkeypatch.setattr(
            yfa, "fetch_ohlcv",
            lambda *a, **kw: yfa.PriceWindow(symbol_used="HDFCBANK.NS", df=df),
        )

        row = _good_row()
        row["metrics"]["avg_30d_turnover_cr"] = None
        row["fundamentals"]["listed_long_enough"] = None
        outcomes = F.filter_cohort(fake_db, [row])

        # The 60-day window of 2026-Mar..May cannot reach 2 years back, so the
        # listing-age probe returns False and the row is dropped — but the
        # turnover probe still ran first and its value was persisted.
        assert outcomes[0].turnover_was_computed is True
        # 100 * 1_000_000 / 1e7 = 10.0
        assert outcomes[0].avg_30d_turnover_cr == pytest.approx(10.0)
        assert outcomes[0].listed_long_enough is False
        assert outcomes[0].drop_reason == F.DROP_LISTING_AGE

        # Confirm DB writes attempted: 1 turnover + 1 listing-age update.
        update_calls = [
            call for call in fake_db.table.return_value.update.call_args_list
        ]
        assert {"avg_30d_turnover_cr": 10.0} in [c.args[0] for c in update_calls]
        assert {"listed_long_enough": False} in [c.args[0] for c in update_calls]

    def test_probe_fail_open_on_listing_age(
        self, fake_db: MagicMock, monkeypatch
    ) -> None:
        """yfinance fetch fails → listing-age fails open (filing passes)."""
        from src.sources import yfinance_adapter as yfa
        monkeypatch.setattr(yfa, "fetch_ohlcv", lambda *a, **kw: None)

        row = _good_row()
        # Turnover already cached → no need to drop on turnover.
        row["fundamentals"]["listed_long_enough"] = None
        outcomes = F.filter_cohort(fake_db, [row])

        # Turnover cached, listing-age fail-open → row passes.
        assert outcomes[0].passed is True
        assert outcomes[0].listed_long_enough is True   # fail-open value
        assert outcomes[0].listing_check_was_computed is False  # nothing persisted

    def test_probe_failure_drops_when_turnover_missing(
        self, fake_db: MagicMock, monkeypatch
    ) -> None:
        """yfinance fails AND turnover cache is empty → drop on turnover."""
        from src.sources import yfinance_adapter as yfa
        monkeypatch.setattr(yfa, "fetch_ohlcv", lambda *a, **kw: None)

        row = _good_row()
        row["metrics"]["avg_30d_turnover_cr"] = None
        row["fundamentals"]["listed_long_enough"] = None
        outcomes = F.filter_cohort(fake_db, [row])

        assert outcomes[0].drop_reason == F.DROP_TURNOVER


# ---------------------------------------------------------------------------
# Dry-run: no DB writes
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_skips_db_writes(
        self, fake_db: MagicMock, monkeypatch
    ) -> None:
        idx = pd.date_range("2020-01-01", periods=500, freq="B")
        df = pd.DataFrame({"Close": [100.0] * 500, "Volume": [1_000_000] * 500}, index=idx)
        from src.sources import yfinance_adapter as yfa
        monkeypatch.setattr(
            yfa, "fetch_ohlcv",
            lambda *a, **kw: yfa.PriceWindow(symbol_used="HDFCBANK.NS", df=df),
        )

        row = _good_row()
        row["metrics"]["avg_30d_turnover_cr"] = None
        row["fundamentals"]["listed_long_enough"] = None
        F.filter_cohort(fake_db, [row], dry_run=True)

        # No .update() call should reach the DB chain.
        fake_db.table.return_value.update.assert_not_called()
