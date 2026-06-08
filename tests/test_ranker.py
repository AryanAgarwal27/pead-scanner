"""Unit tests for src.pipeline.ranker — Phase 4 orchestration.

Focus areas:
    1. Cross-source dedup tiebreaker (NSE > BSE > TRENDLYNE)
    2. Cohort < RANK_MIN_COHORT_SIZE → no ranking written
    3. End-to-end happy path (smoke) → top row written with expected shape
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

from src import config
from src.pipeline import filterer as F
from src.pipeline import ranker as R
from src.utils.time_utils import IST


def _cohort_row(filing_id: int, *, source: str, symbol: str,
                quarter: str = "Q3-FY26", nse_ticker: str | None = None) -> dict:
    """A cohort row in the shape `_dedup_cross_source` expects.

    `nse_ticker` is normally set by `_select_cohort`. Tests stub it directly.
    """
    return {
        "filing_id": filing_id,
        "symbol": symbol,
        "source": source,
        "quarter": quarter,
        "filing_time": datetime(2026, 5, 26, 14, 0, tzinfo=UTC).isoformat(),
        "parser_confidence": "high",
        "has_exceptional_items": False,
        "nse_ticker": nse_ticker,
        "metrics": {
            "sue_proxy": 1.0, "rev_growth_yoy": 0.1, "ear": 0.01,
            "vol_spike": 1.5, "margin_delta": 1.0,
            "avg_30d_turnover_cr": 100.0,
        },
        "fundamentals": {"market_cap_cr": 5000.0, "listed_long_enough": True},
    }


# ---------------------------------------------------------------------------
# Cross-source dedup
# ---------------------------------------------------------------------------


class TestDedup:
    def test_nse_wins_over_bse(self) -> None:
        rows = [
            _cohort_row(1, source="NSE", symbol="SETL", nse_ticker="SETL"),
            _cohort_row(2, source="BSE", symbol="500407", nse_ticker="SETL"),
        ]
        out = R._dedup_cross_source(rows)
        assert len(out) == 1
        assert out[0]["filing_id"] == 1
        assert out[0]["source"] == "NSE"

    def test_bse_wins_when_no_nse_competitor(self) -> None:
        rows = [
            _cohort_row(2, source="BSE", symbol="500407", nse_ticker="SETL"),
        ]
        out = R._dedup_cross_source(rows)
        assert len(out) == 1
        assert out[0]["filing_id"] == 2

    def test_bse_beats_trendlyne(self) -> None:
        rows = [
            _cohort_row(2, source="BSE", symbol="500407", nse_ticker="SETL"),
            _cohort_row(3, source="TRENDLYNE", symbol="TL-setl", nse_ticker="SETL"),
        ]
        out = R._dedup_cross_source(rows)
        assert len(out) == 1
        assert out[0]["filing_id"] == 2

    def test_bse_only_passes_through_when_no_nse_ticker(self) -> None:
        rows = [
            _cohort_row(2, source="BSE", symbol="999999", nse_ticker=None),
            _cohort_row(3, source="BSE", symbol="888888", nse_ticker=None),
        ]
        out = R._dedup_cross_source(rows)
        # Both pass through — no collision possible without an NSE ticker.
        assert len(out) == 2

    def test_different_quarters_not_deduped(self) -> None:
        rows = [
            _cohort_row(1, source="NSE", symbol="HDFCBANK", nse_ticker="HDFCBANK",
                        quarter="Q3-FY26"),
            _cohort_row(2, source="BSE", symbol="500180", nse_ticker="HDFCBANK",
                        quarter="Q2-FY26"),
        ]
        out = R._dedup_cross_source(rows)
        assert len(out) == 2

    def test_order_of_input_does_not_change_winner(self) -> None:
        """NSE wins regardless of which exchange's row came first in the input."""
        a = [
            _cohort_row(1, source="NSE", symbol="SETL", nse_ticker="SETL"),
            _cohort_row(2, source="BSE", symbol="500407", nse_ticker="SETL"),
        ]
        b = [
            _cohort_row(2, source="BSE", symbol="500407", nse_ticker="SETL"),
            _cohort_row(1, source="NSE", symbol="SETL", nse_ticker="SETL"),
        ]
        assert R._dedup_cross_source(a)[0]["filing_id"] == 1
        assert R._dedup_cross_source(b)[0]["filing_id"] == 1


# ---------------------------------------------------------------------------
# Cohort window anchoring (regression: --as-of must not use now())
# ---------------------------------------------------------------------------


class TestSelectCohortWindow:
    def _chain_capturing_db(self) -> tuple[MagicMock, MagicMock]:
        """A db whose filings query chain records its gte/lt bounds."""
        db = MagicMock()
        chain = MagicMock()
        db.table.return_value = chain
        chain.select.return_value = chain
        chain.gte.return_value = chain
        chain.lt.return_value = chain
        chain.order.return_value = chain
        chain.execute.return_value = MagicMock(data=[])
        return db, chain

    def test_window_anchored_on_past_run_date_not_now(self) -> None:
        """_select_cohort for a historical run_date must query the 7 IST
        calendar days ENDING ON that date — with both a lower and an upper
        bound — never a now()-anchored window."""
        db, chain = self._chain_capturing_db()
        run_date = date(2026, 1, 15)

        out = R._select_cohort(db, run_date)
        assert out == []  # empty data → empty cohort, fundamentals branch skipped

        # Upper (exclusive) = IST midnight starting 2026-01-16 = 2026-01-15 18:30 UTC.
        # Lower (inclusive) = upper - 7 days       = 2026-01-08 18:30 UTC.
        expected_upper = datetime(2026, 1, 16, 0, 0, tzinfo=IST).astimezone(UTC)
        expected_lower = expected_upper - timedelta(days=config.COHORT_WINDOW_DAYS)

        gte_arg = chain.gte.call_args.args
        lt_arg = chain.lt.call_args.args
        assert gte_arg[0] == "filing_time"
        assert lt_arg[0] == "filing_time"
        assert gte_arg[1] == expected_lower.isoformat()
        assert lt_arg[1] == expected_upper.isoformat()

        # Both bounds present (the bug had no upper bound at all).
        chain.lt.assert_called_once()
        # The bounds reflect run_date, not the current wall clock.
        assert "2026-01-08" in gte_arg[1]
        assert "2026-01-15" in lt_arg[1]


# ---------------------------------------------------------------------------
# Cohort-too-small short-circuit
# ---------------------------------------------------------------------------


class TestSmallCohort:
    def test_returns_empty_ranking_when_below_min(self, monkeypatch) -> None:
        """Single-filing cohort → no ranking written, summary reports zero."""
        db = MagicMock()
        monkeypatch.setattr(R, "_select_cohort", lambda _db, _d: [
            _cohort_row(1, source="NSE", symbol="HDFCBANK", nse_ticker="HDFCBANK"),
        ])
        monkeypatch.setattr(
            F, "filter_cohort",
            lambda _db, rows, dry_run=False: [
                F.FilterOutcome(filing_id=int(r["filing_id"]),
                                symbol_nse="HDFCBANK", passed=True, drop_reason=None,
                                avg_30d_turnover_cr=100.0, listed_long_enough=True)
                for r in rows
            ],
        )
        summary = R.run_ranking(db, run_date=date(2026, 5, 26))
        assert summary.ranked_count == 0
        assert summary.top_score is None
        # DB.delete not called (no ranking persisted)
        db.table.return_value.delete.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end happy path (smoke)
# ---------------------------------------------------------------------------


class TestRunRanking:
    def test_writes_top_rows_to_db(self, monkeypatch) -> None:
        """5 filings, all pass filters, scoring + dedup + write all run."""
        db = MagicMock()
        # Build a 5-row cohort with varying metric values so z is well-defined.
        cohort = []
        for i in range(5):
            r = _cohort_row(i + 1, source="NSE", symbol=f"SYM{i+1}",
                            nse_ticker=f"SYM{i+1}")
            r["metrics"] = {
                "sue_proxy": float(i),
                "rev_growth_yoy": 0.1 * i,
                "ear": 0.01 * i,
                "vol_spike": 1.0 + 0.5 * i,
                "margin_delta": float(i),
                "avg_30d_turnover_cr": 100.0,
            }
            cohort.append(r)

        monkeypatch.setattr(R, "_select_cohort", lambda _db, _d: cohort)
        monkeypatch.setattr(
            F, "filter_cohort",
            lambda _db, rows, dry_run=False: [
                F.FilterOutcome(filing_id=int(r["filing_id"]),
                                symbol_nse=r["nse_ticker"], passed=True,
                                drop_reason=None, avg_30d_turnover_cr=100.0,
                                listed_long_enough=True)
                for r in rows
            ],
        )

        # DB chain mocks: delete().eq().execute() and insert().execute().
        chain = MagicMock()
        db.table.return_value = chain
        chain.delete.return_value = chain
        chain.eq.return_value = chain
        chain.insert.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        summary = R.run_ranking(db, run_date=date(2026, 5, 26))

        assert summary.ranked_count == 5
        assert summary.top_score is not None
        assert summary.cohort_raw_size == 5
        assert summary.cohort_after_dedup == 5
        assert summary.cohort_after_filters == 5

        # Verify insert payload shape — rank ascending 1..5, top first.
        insert_call = chain.insert.call_args
        assert insert_call is not None
        payload = insert_call.args[0]
        assert len(payload) == 5
        assert [p["rank"] for p in payload] == [1, 2, 3, 4, 5]
        # Top-ranked = SYM5 (highest values).
        assert payload[0]["symbol_nse"] == "SYM5"
        assert payload[0]["pead_score"] > payload[-1]["pead_score"]
        # cohort_size denormalized into every row
        assert all(p["cohort_size"] == 5 for p in payload)

    def test_rank_debug_env_var_emits_per_row_lines(
        self, monkeypatch, capsys
    ) -> None:
        """RANK_DEBUG=1 → per-row debug block lands on stdout in the
        documented format (rank=, score=, z_*, filing_id=, revenue/pat/opm).
        Uses capsys (not caplog) because the project logger has
        propagate=False and writes directly to stdout."""
        db = MagicMock()
        chain = MagicMock()
        db.table.return_value = chain
        chain.delete.return_value = chain
        chain.eq.return_value = chain
        chain.insert.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        cohort = []
        for i in range(3):
            r = _cohort_row(i + 1, source="NSE", symbol=f"SYM{i+1}",
                            nse_ticker=f"SYM{i+1}")
            r["metrics"]["sue_proxy"] = float(i)
            r["metrics"]["ear"] = 0.01 * i
            r["metrics"]["vol_spike"] = 1.0 + 0.5 * i
            r["parsed_at"] = "2026-05-26T14:00:00+00:00"
            r["revenue_cr"] = 100.0 + i
            r["pat_cr"] = 20.0 + i
            r["opm_pct"] = 18.0 + i
            cohort.append(r)

        monkeypatch.setattr(R, "_select_cohort", lambda _db, _d: cohort)
        monkeypatch.setattr(
            F, "filter_cohort",
            lambda _db, rows, dry_run=False: [
                F.FilterOutcome(filing_id=int(r["filing_id"]),
                                symbol_nse=r["nse_ticker"], passed=True,
                                drop_reason=None, avg_30d_turnover_cr=100.0,
                                listed_long_enough=True)
                for r in rows
            ],
        )

        monkeypatch.setenv("RANK_DEBUG", "1")
        # Rebind the ranker logger's handler to the pytest-captured stdout —
        # the handler was bound to the original sys.stdout at import time and
        # can't see capsys's swapped stream otherwise.
        import logging
        import sys
        for h in logging.getLogger("src.pipeline.ranker").handlers:
            if isinstance(h, logging.StreamHandler):
                h.stream = sys.stdout

        R.run_ranking(db, run_date=date(2026, 5, 26), dry_run=True)

        text = capsys.readouterr().out
        # Top row should be SYM3 (highest values).
        assert "rank=1 symbol=SYM3" in text
        # Z-component format
        assert "z_sue=" in text and "z_rev=" in text and "z_margin=" in text
        # Filing-source detail line
        assert "filing_id=3" in text
        assert "parsed=2026-05-26" in text
        assert "confidence=high" in text
        assert "source=NSE" in text
        # Headline-number line — revenue/pat/opm for top row are 102.0/22.0/20.0
        assert "revenue=102.00" in text
        assert "pat=22.00" in text
        assert "opm=20.00" in text

    def test_dry_run_skips_db_writes(self, monkeypatch) -> None:
        db = MagicMock()
        chain = MagicMock()
        db.table.return_value = chain

        cohort = [
            _cohort_row(i + 1, source="NSE", symbol=f"SYM{i+1}",
                        nse_ticker=f"SYM{i+1}")
            for i in range(3)
        ]
        # Vary metric values so we actually get a composite score.
        for i, r in enumerate(cohort):
            r["metrics"]["sue_proxy"] = float(i)
            r["metrics"]["ear"] = 0.01 * i
            r["metrics"]["vol_spike"] = 1.0 + 0.5 * i

        monkeypatch.setattr(R, "_select_cohort", lambda _db, _d: cohort)
        monkeypatch.setattr(
            F, "filter_cohort",
            lambda _db, rows, dry_run=False: [
                F.FilterOutcome(filing_id=int(r["filing_id"]),
                                symbol_nse=r["nse_ticker"], passed=True,
                                drop_reason=None, avg_30d_turnover_cr=100.0,
                                listed_long_enough=True)
                for r in rows
            ],
        )

        summary = R.run_ranking(db, run_date=date(2026, 5, 26), dry_run=True)
        assert summary.ranked_count == 3
        # No delete/insert in dry-run mode.
        chain.delete.assert_not_called()
        chain.insert.assert_not_called()
