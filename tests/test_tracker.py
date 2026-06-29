"""Phase 6 — position tracker.

The pure state machine (simulate_position) is exhaustively unit-tested with
synthetic daily bars; run_tracker is covered with a mocked db + monkeypatched
yfinance fetch. All decisions trace to BRD §3.5 FR-5.1 / §3.6 FR-6.x:
  * entry = T+1-high breakout within ENTRY_WINDOW_DAYS, else EXPIRED
  * stop pre-T1; book 50% at T1; trail the rest on the 20-EMA; 60-day max hold
  * straddling-bar convention: STOP fills before TARGET (conservative)
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.pipeline import tracker as T
from src.pipeline.tracker import PositionResult, simulate_position


def _bars(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """rows = [(date, high, low, close), ...] → OHLC DataFrame (Open=Close)."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[3] for r in rows],
            "High": [r[1] for r in rows],
            "Low": [r[2] for r in rows],
            "Close": [r[3] for r in rows],
        },
        index=idx,
    )


# Common levels for the synthetic stock: entry 100, stop 95, T1 110.
ENTRY, STOP, T1 = 100.0, 95.0, 110.0
REF = date(2026, 1, 1)   # T+1 reference; entry scanned strictly after this


def _sim(rows, *, as_of, **kw) -> PositionResult:
    return simulate_position(
        entry_price=ENTRY, stop_price=STOP, target1_price=T1,
        signal_ref_date=REF, bars=_bars(rows), as_of=as_of,
        entry_window_days=kw.pop("entry_window_days", 5),
        max_hold_days=kw.pop("max_hold_days", 60),
        ema_span=kw.pop("ema_span", 20),
    )


class TestEntry:
    def test_pending_when_window_not_yet_elapsed(self) -> None:
        # Two bars, neither triggers, window is 5 → still PENDING (window open).
        rows = [("2026-01-02", 99, 96, 98), ("2026-01-05", 99, 97, 98)]
        r = _sim(rows, as_of=date(2026, 1, 5))
        assert r.status == "PENDING_ENTRY"

    def test_expired_when_window_elapsed_without_trigger(self) -> None:
        rows = [
            ("2026-01-02", 99, 96, 98), ("2026-01-05", 99, 97, 98),
            ("2026-01-06", 99, 97, 98), ("2026-01-07", 99, 97, 98),
            ("2026-01-08", 99, 97, 98),
        ]
        r = _sim(rows, as_of=date(2026, 1, 8))
        assert r.status == "EXPIRED"
        assert r.entry_filled_at is None

    def test_entry_triggers_on_breakout(self) -> None:
        rows = [("2026-01-02", 99, 97, 98), ("2026-01-05", 101, 99, 100.5)]
        r = _sim(rows, as_of=date(2026, 1, 5))
        assert r.status == "ACTIVE"
        assert r.entry_filled_at == date(2026, 1, 5)

    def test_no_entry_on_t1_reference_bar_itself(self) -> None:
        # A bar ON the ref date must NOT count as entry (high==entry there is
        # by construction); only strictly-later bars can trigger.
        rows = [("2026-01-01", 105, 99, 100), ("2026-01-02", 99, 97, 98)]
        r = _sim(rows, as_of=date(2026, 1, 2))
        assert r.status == "PENDING_ENTRY"


class TestStop:
    def test_stop_before_t1_full_loss(self) -> None:
        rows = [("2026-01-02", 101, 99, 100), ("2026-01-05", 102, 94, 96)]
        r = _sim(rows, as_of=date(2026, 1, 5))
        assert r.status == "CLOSED_STOP"
        assert r.exit_reason == "STOP"
        assert r.exit_price == 95.0
        assert r.pnl_pct == pytest.approx(95.0 / 100.0 - 1.0)   # −5%

    def test_straddle_bar_resolves_to_stop_first(self) -> None:
        # Same bar prints high ≥ T1 AND low ≤ stop → conservative STOP wins.
        rows = [("2026-01-02", 111, 94, 108)]
        r = _sim(rows, as_of=date(2026, 1, 2))
        assert r.status == "CLOSED_STOP"
        assert r.t1_hit_at is None


class TestTargetAndTrail:
    def test_t1_then_trail_exit_blended_pnl(self) -> None:
        # Bar1 entry+T1 (high 112). Then price rises, then closes below the EMA.
        rows = [
            ("2026-01-02", 112, 100, 111),   # entry + T1 booked
            ("2026-01-05", 120, 110, 118),
            ("2026-01-06", 122, 112, 120),
            ("2026-01-07", 121, 90, 92),     # sharp close below 20-EMA → TRAIL
        ]
        r = _sim(rows, as_of=date(2026, 1, 7))
        assert r.status == "CLOSED_TRAIL"
        assert r.exit_reason == "TRAIL"
        assert r.t1_hit_at == date(2026, 1, 2)
        expected = 0.5 * (T1 / ENTRY - 1.0) + 0.5 * (92.0 / ENTRY - 1.0)
        assert r.pnl_pct == pytest.approx(expected)

    def test_t1_booked_does_not_trail_on_same_bar(self) -> None:
        # Single bar hits T1 but closes (108) below where a naive EMA<close test
        # might trip; the trail must not fire on the T1 bar itself → ACTIVE.
        rows = [("2026-01-02", 112, 100, 108)]
        r = _sim(rows, as_of=date(2026, 1, 2))
        assert r.status == "ACTIVE"
        assert r.t1_hit_at == date(2026, 1, 2)

    def test_time_expiry_after_max_hold(self) -> None:
        # Entry+T1 on day 0, then a long gentle uptrend (never closes below EMA)
        # until the max-hold cap forces a TIME_EXPIRY exit.
        rows = [("2026-01-02", 112, 100, 111)]
        px = 112.0
        # 6 trading days, max_hold_days=5 → exit on the 6th (days_held==5).
        for d in range(3, 9):
            px += 1.0
            rows.append((f"2026-01-{d:02d}", px + 1, px - 0.5, px))
        r = _sim(rows, as_of=date(2026, 1, 8), max_hold_days=5)
        assert r.status == "CLOSED_TIME_EXPIRY"
        assert r.exit_reason == "TIME_EXPIRY"
        assert r.days_held == 5


class TestActiveTracking:
    def test_active_unrealized_pnl_and_extremes(self) -> None:
        rows = [
            ("2026-01-02", 101, 99, 100),    # entry
            ("2026-01-05", 106, 98, 104),
        ]
        r = _sim(rows, as_of=date(2026, 1, 5))
        assert r.status == "ACTIVE"
        assert r.pnl_pct == pytest.approx(104.0 / 100.0 - 1.0)
        assert r.max_favorable == pytest.approx(106.0 / 100.0 - 1.0)
        assert r.max_adverse == pytest.approx(98.0 / 100.0 - 1.0)

    def test_as_of_truncates_future_bars(self) -> None:
        rows = [
            ("2026-01-02", 101, 99, 100),
            ("2026-01-09", 102, 94, 96),     # this stop is AFTER as_of → ignored
        ]
        r = _sim(rows, as_of=date(2026, 1, 5))
        assert r.status == "ACTIVE"   # the later stop bar is not yet visible


def _signal_row() -> dict:
    return {
        "id": 1, "filing_id": 10, "symbol": "FOO", "status": "PENDING_ENTRY",
        "entry_price": 100.0, "stop_price": 95.0, "target1_price": 110.0,
        "signal_sent_at": "2026-01-01T10:00:00+00:00",
        "filings": {
            "symbol": "FOO", "source": "NSE",
            "filing_time": "2025-12-31T10:00:00+00:00",
        },
    }


def _stub_fetch(monkeypatch, bars) -> None:
    """Stub the tracker's yfinance fetch + ref-candle lookup with `bars`."""
    win = T.yfa.SignalWindow("FOO.NS", bars)
    monkeypatch.setattr(T.yfa, "fetch_ohlc_range", lambda *a, **k: win)
    f = bars.iloc[0]
    ref = {"high": float(f["High"]), "low": float(f["Low"])}
    monkeypatch.setattr(T.yfa, "candle_on_or_after", lambda df, d: ref)
    monkeypatch.setattr(T, "_fill_summary_stats", lambda *a, **k: None)


class TestRunTracker:
    def test_run_tracker_persists_and_updates_status(self, monkeypatch) -> None:
        db = MagicMock()
        monkeypatch.setattr(T, "_load_open_signals", lambda _db: [_signal_row()])
        # filing_time 2025-12-31 → T+1 ref bar = first bar on/after 2026-01-01
        # (2026-01-02). Entry is scanned on the bars AFTER that ref bar.
        _stub_fetch(monkeypatch, _bars([
            ("2026-01-02", 105, 100, 104),   # T+1 reference candle
            ("2026-01-05", 112, 100, 111),   # entry + T1 booked
            ("2026-01-06", 121, 90, 92),     # close below 20-EMA → TRAIL
        ]))
        captured: dict = {}
        monkeypatch.setattr(
            T, "_persist",
            lambda _db, sid, res: captured.update(status=res.status, reason=res.exit_reason),
        )

        summary = T.run_tracker(db, run_date=date(2026, 1, 6), dry_run=False, notifier=None)
        assert captured["status"] == "CLOSED_TRAIL"
        assert captured["reason"] == "TRAIL"
        assert summary.newly_closed == 1

    def test_dry_run_writes_nothing(self, monkeypatch) -> None:
        db = MagicMock()
        monkeypatch.setattr(T, "_load_open_signals", lambda _db: [_signal_row()])
        _stub_fetch(monkeypatch, _bars([("2026-01-02", 101, 99, 100)]))
        persisted = []
        monkeypatch.setattr(T, "_persist", lambda *a, **k: persisted.append(a))

        T.run_tracker(db, run_date=date(2026, 1, 5), dry_run=True, notifier=None)
        assert persisted == []   # dry-run never persists
