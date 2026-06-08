"""Unit tests for src.pipeline.signaler.

Two layers:
    1. compute_levels — pure entry/stop/T1/RR math (FR-5.1), incl. the
       tighter-of-(low, -5%) stop rule and the degenerate-candle guard.
    2. run_signals — orchestration: idempotent dedup, SKIP-tier drop, C2/C4
       hard-skip → not sent, happy-path send+persist (status PENDING_ENTRY),
       dry-run writes nothing, regime-unavailable suppresses all sends.

yfinance + DB loads are monkeypatched (no network, no Supabase), mirroring
the style of tests/test_ranker.py.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from src import config
from src.pipeline import signaler as SIG
from src.sources import yfinance_adapter as yfa

# ===========================================================================
# compute_levels — pure
# ===========================================================================


class TestComputeLevels:
    def test_stop_uses_low_when_inside_5pct_cap(self) -> None:
        # low (105) is above the -5% cap (104.5) → stop = low (tighter).
        lv = SIG.compute_levels(high_t1=110.0, low_t1=105.0)
        assert lv is not None
        assert lv.entry == 110.0
        assert lv.stop == 105.0
        # T1 = entry + 1.5 × (entry - stop) = 110 + 1.5×5 = 117.5
        assert lv.target1 == pytest.approx(117.5)
        assert lv.risk_reward == pytest.approx(config.TARGET_R_MULTIPLE)

    def test_stop_capped_at_minus_5pct_when_low_too_far(self) -> None:
        # low (90) is below the -5% cap (95) → stop = cap (tighter = higher price).
        lv = SIG.compute_levels(high_t1=100.0, low_t1=90.0)
        assert lv is not None
        assert lv.stop == pytest.approx(95.0)         # 100 × (1 - 0.05)
        assert lv.target1 == pytest.approx(100.0 + 1.5 * 5.0)
        assert lv.risk_reward == pytest.approx(1.5)

    def test_degenerate_candle_returns_none(self) -> None:
        # Zero-range candle: stop == entry → no positive risk → None.
        assert SIG.compute_levels(high_t1=100.0, low_t1=100.0) is None


# ===========================================================================
# run_signals — orchestration
# ===========================================================================


class _FakeSW:
    """Stand-in for a SignalWindow; df is opaque (slicers are monkeypatched)."""

    empty = False
    df = "DF"


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_markdown(self, text: str) -> None:
        self.messages.append(text)


def _ranking_row(
    filing_id: int = 1,
    *,
    symbol_nse: str = "HDFCBANK",
    rank: int = 1,
    pead_score: float = 2.8,
) -> dict:
    return {
        "filing_id": filing_id,
        "symbol_nse": symbol_nse,
        "rank": rank,
        "pead_score": pead_score,
        "z_sue": 1.5, "z_rev": 0.4, "z_ear": 2.1, "z_vol": 0.9, "z_margin": 0.1,
        "filings": {
            "symbol": symbol_nse,
            "source": "NSE",
            "filing_time": "2026-05-25T08:53:00+00:00",
            "company_name": "HDFC Bank Ltd",
        },
    }


def _wire(
    monkeypatch,
    *,
    rows: list[dict],
    existing: set[int] | None = None,
    metrics: dict[int, dict] | None = None,
    fundamentals: dict[str, dict] | None = None,
    regime=(100.0, 90.0, True),
    candle="__default__",   # sentinel: distinguish "not given" from explicit None
    close_tm1: float | None = 100.0,
    corp_action: bool = False,
) -> None:
    """Monkeypatch every DB load + yfinance call the signaler makes."""
    metrics = metrics if metrics is not None else {
        r["filing_id"]: {"vol_spike": 3.0, "avg_30d_turnover_cr": 100.0} for r in rows
    }
    fundamentals = fundamentals if fundamentals is not None else {
        r["symbol_nse"]: {"symbol": r["symbol_nse"], "sector": "Banks"} for r in rows
    }
    if candle == "__default__":
        candle = {"high": 110.0, "low": 105.0, "close": 108.0}

    monkeypatch.setattr(SIG, "_load_ranking", lambda _db, _d: rows)
    monkeypatch.setattr(SIG, "_existing_signal_filing_ids", lambda _db, _ids: (existing or set()))
    monkeypatch.setattr(SIG, "_load_metrics", lambda _db, _ids: metrics)
    monkeypatch.setattr(SIG, "_load_fundamentals", lambda _db, _s: fundamentals)
    monkeypatch.setattr(SIG, "_load_open_signals", lambda _db: [])

    monkeypatch.setattr(yfa, "fetch_nifty_regime", lambda _as_of: regime)
    monkeypatch.setattr(yfa, "fetch_signal_window", lambda *a, **k: _FakeSW())
    monkeypatch.setattr(yfa, "candle_on_or_after", lambda _df, _t: candle)
    monkeypatch.setattr(yfa, "close_on_or_before", lambda _df, _t: close_tm1)
    monkeypatch.setattr(yfa, "corporate_action_within", lambda _df, _t, _w: corp_action)


class TestRunSignalsHappyPath:
    def test_sends_and_persists_pending_entry(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        _wire(monkeypatch, rows=[_ranking_row(1, pead_score=2.8)])

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)

        assert summary.sent_count == 1
        assert summary.by_tier == {"TAKE": 1}
        # One signal message + one summary message.
        assert len(notifier.messages) == 2
        assert "HDFCBANK" in notifier.messages[0]

        # Inserted row has the expected shape + status.
        insert_args = db.table.return_value.insert.call_args
        assert insert_args is not None
        row = insert_args.args[0]
        assert row["status"] == "PENDING_ENTRY"
        assert row["tier"] == "TAKE"
        assert row["suggested_size_r"] == 1.0          # 2.8σ & 5/5
        assert row["entry_price"] == 110.0
        assert row["stop_price"] == 105.0
        assert row["target1_price"] == pytest.approx(117.5)
        assert row["confirmations_passed"] == 5
        assert "signal_sent_at" in row
        # The message-only "_z" helper key must NOT be persisted.
        assert "_z" not in row


class TestRunSignalsSkips:
    def test_skip_tier_not_sent(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        _wire(monkeypatch, rows=[_ranking_row(1, pead_score=1.5)])  # < 2.0 → SKIP

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)

        assert summary.sent_count == 0
        assert summary.skipped_tier == 1
        db.table.return_value.insert.assert_not_called()

    def test_idempotent_existing_signal_skipped(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        _wire(monkeypatch, rows=[_ranking_row(7, pead_score=2.8)], existing={7})

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)

        assert summary.sent_count == 0
        assert summary.already_signalled == 1
        db.table.return_value.insert.assert_not_called()

    def test_c4_liquidity_failure_is_hard_skip(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        # Tiny turnover → nominal 1.0R position blows past the 10% headroom.
        _wire(
            monkeypatch,
            rows=[_ranking_row(1, pead_score=3.5)],   # STRONG
            metrics={1: {"vol_spike": 3.0, "avg_30d_turnover_cr": 0.001}},
        )
        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)
        assert summary.sent_count == 0
        assert summary.skipped_sizing == 1
        db.table.return_value.insert.assert_not_called()

    def test_c2_regime_failure_suppresses_all(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        _wire(monkeypatch, rows=[_ranking_row(1, pead_score=3.5)], regime=None)

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)

        assert summary.sent_count == 0
        assert summary.skipped_sizing == 1
        assert summary.regime_available is False
        db.table.return_value.insert.assert_not_called()
        # Summary message still goes out and notes the regime gap.
        assert any("regime unavailable" in m.lower() for m in notifier.messages)

    def test_no_t1_candle_yet_skips_as_data(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        _wire(monkeypatch, rows=[_ranking_row(1, pead_score=2.8)], candle=None)

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)
        assert summary.sent_count == 0
        assert summary.skipped_data == 1


class TestRunSignalsDryRun:
    def test_dry_run_writes_nothing_and_sends_nothing(self, monkeypatch) -> None:
        db = MagicMock()
        _wire(monkeypatch, rows=[_ranking_row(1, pead_score=2.8)])

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), dry_run=True, notifier=None)

        assert summary.sent_count == 1          # counted as "would send"
        db.table.assert_not_called()            # no DB writes at all


class TestRunSignalsEmpty:
    def test_no_rankings_returns_empty(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        _wire(monkeypatch, rows=[])

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)
        assert summary.ranked_count == 0
        assert summary.sent_count == 0


# ===========================================================================
# Concentration flags (FR-5.7)
# ===========================================================================


class TestConcentrationFlags:
    def test_flags_when_open_positions_exceed_limit(self, monkeypatch) -> None:
        db = MagicMock()
        notifier = _FakeNotifier()
        # 1 fresh send + many prior-open rows of the same sector → trips both
        # the open-count and per-sector flags.
        prior_open = [
            {
                "filing_id": 100 + i,
                "symbol": f"BANK{i}",
                "entry_price": 100.0,
                "stop_price": 95.0,
                "suggested_size_r": 0.5,
                "status": "PENDING_ENTRY",
            }
            for i in range(config.MAX_OPEN_POSITIONS + 2)
        ]
        _wire(monkeypatch, rows=[_ranking_row(1, pead_score=2.8)])
        monkeypatch.setattr(SIG, "_load_open_signals", lambda _db: prior_open)
        # All prior-open symbols resolve to one sector.
        monkeypatch.setattr(SIG, "_lookup_sector", lambda _db, _s: "Banks")

        summary = SIG.run_signals(db, run_date=date(2026, 5, 26), notifier=notifier)

        joined = " ".join(summary.flags)
        assert "Open PEAD positions" in joined
        assert "per-sector limit" in joined
