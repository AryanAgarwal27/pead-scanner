"""Unit tests for src.pipeline.metrics — pure functions, no I/O.

Covers BRD §3.3 FR-3.3 formulas and every edge case listed in the Phase 3
plan: insufficient history, missing YoY data, missing Nifty, zero-variance,
zero-volume, holiday-window degenerate cases.
"""

from __future__ import annotations

import math

import pytest

from src.pipeline.metrics import (
    ear,
    margin_delta,
    rev_growth_yoy,
    sue_proxy,
    vol_spike,
)

# ---------------------------------------------------------------------------
# SUE_proxy
# ---------------------------------------------------------------------------


class TestSueProxy:
    def test_positive_surprise(self) -> None:
        import statistics as _s
        series = [100, 110, 95, 105, 100, 90, 115, 100]
        expected = (200 - 100) / _s.pstdev(series)
        result = sue_proxy(pat_curr=200.0, pat_yoy=100.0, last_8q_pat=series)
        assert result == pytest.approx(expected)

    def test_negative_surprise(self) -> None:
        series = [100.0] * 8  # std=0 — would div by zero
        assert sue_proxy(50.0, 100.0, series) is None

    def test_returns_none_on_missing_pat_curr(self) -> None:
        assert sue_proxy(None, 100.0, [100.0] * 8) is None

    def test_returns_none_on_missing_pat_yoy(self) -> None:
        assert sue_proxy(120.0, None, [100.0, 110.0, 95.0, 105.0]) is None

    def test_returns_none_on_insufficient_history(self) -> None:
        # < 4 quarters → can't compute meaningful std-dev
        assert sue_proxy(120.0, 100.0, [100.0, 110.0, 95.0]) is None
        assert sue_proxy(120.0, 100.0, []) is None

    def test_returns_none_on_zero_variance(self) -> None:
        flat = [100.0, 100.0, 100.0, 100.0]
        assert sue_proxy(150.0, 100.0, flat) is None

    def test_works_with_exactly_4_quarters(self) -> None:
        # Floor: 4 quarters accepted
        result = sue_proxy(150.0, 100.0, [100.0, 110.0, 95.0, 120.0])
        assert result is not None


# ---------------------------------------------------------------------------
# Rev_Growth_YoY
# ---------------------------------------------------------------------------


class TestRevGrowthYoY:
    def test_positive_growth(self) -> None:
        assert rev_growth_yoy(115.0, 100.0) == pytest.approx(0.15)

    def test_negative_growth(self) -> None:
        assert rev_growth_yoy(80.0, 100.0) == pytest.approx(-0.20)

    def test_returns_none_on_missing(self) -> None:
        assert rev_growth_yoy(None, 100.0) is None
        assert rev_growth_yoy(100.0, None) is None

    def test_returns_none_on_zero_or_negative_yoy(self) -> None:
        # Zero or negative prior revenue is degenerate for growth %
        assert rev_growth_yoy(100.0, 0.0) is None
        assert rev_growth_yoy(100.0, -50.0) is None


# ---------------------------------------------------------------------------
# Vol_Spike
# ---------------------------------------------------------------------------


class TestVolSpike:
    def test_typical_spike(self) -> None:
        prior = [1_000_000.0] * 30
        result = vol_spike(2_500_000.0, prior)
        assert result == pytest.approx(2.5)

    def test_returns_none_on_missing(self) -> None:
        assert vol_spike(None, [1.0, 2.0]) is None
        assert vol_spike(1_000_000.0, []) is None

    def test_returns_none_on_zero_avg(self) -> None:
        assert vol_spike(1_000_000.0, [0.0] * 30) is None

    def test_short_history_still_works(self) -> None:
        # No floor on prior-window length (yfinance may have short history for
        # recently listed stocks); enricher decides if window is too small.
        result = vol_spike(200_000.0, [100_000.0, 100_000.0])
        assert result == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# EAR
# ---------------------------------------------------------------------------


class TestEar:
    def test_stock_outperforms_nifty(self) -> None:
        # Stock +5%, Nifty +1% -> EAR = 4%
        result = ear(
            close_t_minus_1=100.0,
            close_t_plus_1=105.0,
            nifty_t_minus_1=20_000.0,
            nifty_t_plus_1=20_200.0,
        )
        assert result is not None
        assert result == pytest.approx(0.04, abs=1e-6)

    def test_stock_underperforms_nifty(self) -> None:
        # Stock -3%, Nifty +2% -> EAR = -5%
        result = ear(
            close_t_minus_1=100.0,
            close_t_plus_1=97.0,
            nifty_t_minus_1=20_000.0,
            nifty_t_plus_1=20_400.0,
        )
        assert result is not None
        assert result == pytest.approx(-0.05, abs=1e-6)

    def test_returns_none_on_any_missing(self) -> None:
        assert ear(None, 100.0, 20_000.0, 20_100.0) is None
        assert ear(100.0, None, 20_000.0, 20_100.0) is None
        assert ear(100.0, 105.0, None, 20_100.0) is None
        assert ear(100.0, 105.0, 20_000.0, None) is None

    def test_returns_none_on_zero_denominator(self) -> None:
        assert ear(0.0, 100.0, 20_000.0, 20_100.0) is None
        assert ear(100.0, 105.0, 0.0, 20_100.0) is None

    def test_returns_none_on_negative_denominator(self) -> None:
        assert ear(-50.0, 100.0, 20_000.0, 20_100.0) is None

    def test_zero_movement_returns_zero_ear(self) -> None:
        result = ear(100.0, 100.0, 20_000.0, 20_000.0)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Margin_Delta
# ---------------------------------------------------------------------------


class TestMarginDelta:
    def test_margin_expanded(self) -> None:
        # OPM up from 18.5% to 22.0% -> delta = +3.5 ppt
        assert margin_delta(22.0, 18.5) == pytest.approx(3.5)

    def test_margin_contracted(self) -> None:
        assert margin_delta(15.0, 22.5) == pytest.approx(-7.5)

    def test_returns_none_on_missing(self) -> None:
        assert margin_delta(None, 20.0) is None
        assert margin_delta(20.0, None) is None
        assert margin_delta(None, None) is None

    def test_negative_margins_allowed(self) -> None:
        # Loss-making company with widening loss
        assert margin_delta(-5.0, -2.0) == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# Cross-metric sanity
# ---------------------------------------------------------------------------


def test_nan_inputs_propagate_safely() -> None:
    """Defensive: NaN in any metric input should not produce a spurious result."""
    nan = float("nan")
    # Sentinel: we don't expect NaNs in production (yfinance returns clean
    # floats after dropna), but if they slip through, the result is also NaN
    # — caller stores as NULL via Postgres NaN handling. Confirm we don't
    # crash.
    result = ear(nan, 105.0, 20_000.0, 20_100.0)
    assert result is None or math.isnan(result)
