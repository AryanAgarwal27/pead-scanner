"""PEAD component metric calculations — Phase 3.

Pure functions. No I/O. No global state. Every metric returns None when its
inputs are insufficient — the enricher persists NULL in the metrics table
and Phase 4's hard filters drop those rows from the top-25 ranking.

Formulas (BRD §3.3 FR-3.3):

    SUE_proxy      = (PAT_curr - PAT_yoy) / pstdev(last_8_qtr_PAT)
    Rev_Growth_YoY = (Rev_curr / Rev_yoy) - 1
    Vol_Spike      = Vol_T+1 / mean(Vol_T-30 .. T-1)
    EAR            = (Close_T+1/Close_T-1 - 1) - (Nifty_T+1/Nifty_T-1 - 1)
    Margin_Delta   = OPM_curr - OPM_yoy
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence


def sue_proxy(
    pat_curr: float | None,
    pat_yoy: float | None,
    last_8q_pat: Sequence[float],
) -> float | None:
    """Standardized Unexpected Earnings (proxy).

    Returns None if:
      - either current or YoY PAT missing
      - fewer than 4 historical PAT values available (insufficient for a
        meaningful std-dev; BRD asks for 8 but 4 is the floor we'll accept)
      - historical PAT series has zero variance (division by zero)
    """
    if pat_curr is None or pat_yoy is None:
        return None
    if len(last_8q_pat) < 4:
        return None
    std = statistics.pstdev(last_8q_pat)
    if std == 0:
        return None
    return (pat_curr - pat_yoy) / std


def rev_growth_yoy(rev_curr: float | None, rev_yoy: float | None) -> float | None:
    """Year-over-year revenue growth, expressed as a fraction (0.15 = +15%).

    Returns None if rev_yoy is missing or <= 0 (negative revenue is
    nonsensical for this metric; zero would divide-by-zero).
    """
    if rev_curr is None or rev_yoy is None or rev_yoy <= 0:
        return None
    return (rev_curr / rev_yoy) - 1.0


def vol_spike(
    vol_t_plus_1: float | None, prior_30d_vols: Sequence[float]
) -> float | None:
    """Ratio of T+1 trading volume to mean of the prior-30 trading-day window.

    Returns None if T+1 volume missing OR prior window is empty OR mean is 0.
    """
    if vol_t_plus_1 is None or not prior_30d_vols:
        return None
    avg = sum(prior_30d_vols) / len(prior_30d_vols)
    if avg <= 0:
        return None
    return vol_t_plus_1 / avg


def ear(
    close_t_minus_1: float | None,
    close_t_plus_1: float | None,
    nifty_t_minus_1: float | None,
    nifty_t_plus_1: float | None,
) -> float | None:
    """Earnings Announcement Return — 3-day stock return excess over Nifty.

    EAR = (Close_T+1/Close_T-1 - 1) - (Nifty_T+1/Nifty_T-1 - 1)

    Returns None on any missing input or non-positive denominator.
    """
    if None in (close_t_minus_1, close_t_plus_1, nifty_t_minus_1, nifty_t_plus_1):
        return None
    assert close_t_minus_1 is not None  # narrow for mypy
    assert nifty_t_minus_1 is not None
    if close_t_minus_1 <= 0 or nifty_t_minus_1 <= 0:
        return None
    stock_return = close_t_plus_1 / close_t_minus_1 - 1  # type: ignore[operator]
    nifty_return = nifty_t_plus_1 / nifty_t_minus_1 - 1  # type: ignore[operator]
    return stock_return - nifty_return


def margin_delta(opm_curr: float | None, opm_yoy: float | None) -> float | None:
    """Change in operating profit margin (percentage points) versus YoY.

    Both inputs are percentages already (e.g. 22.5 means 22.5%); the output
    is the simple percentage-point difference (curr - yoy).
    """
    if opm_curr is None or opm_yoy is None:
        return None
    return opm_curr - opm_yoy
