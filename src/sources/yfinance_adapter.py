"""yfinance adapter — Phase 3.

Pulls OHLCV for the T-30...T+1 window needed by Vol_Spike and EAR metrics
(BRD §3.3 FR-3.2). Also pulls Nifty 50 (^NSEI) closes for the EAR benchmark.

Symbol resolution (matches src.sources.symbol_map's NSE-canonical convention):
    - NSE filing symbol         → '<symbol>.NS'
    - BSE filing scrip resolved → '<nse_ticker>.NS'  (via symbol_map)
    - BSE-only (unresolved)     → '<scrip>.BO'
    - Trendlyne slug            → None (no yfinance support)

Fallback: if the preferred symbol returns an empty history (rare — e.g.
NSE delisted, BSE-only stock missing from yfinance), retry with the OTHER
exchange suffix. Still empty → return None to the caller; the enricher's
metric functions will produce None for price-derived metrics, and Phase 4's
hard filters will drop the row.

Calendar awareness: yfinance returns ONLY trading days, so T-1 and T+1 are
the most recent trading days before/after the filing date — holidays and
weekends are skipped automatically. Callers do NOT need to compute their
own NSE calendar.

Test/CI note: yfinance fetches over network — caller should mock this in
unit tests via `tests/fixtures/yfinance_*.csv` or by monkeypatching
`fetch_ohlcv`. There is no in-process cache here (per-run, per-symbol).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from src.sources.symbol_map import to_nse_ticker
from src.utils.logging import get_logger

log = get_logger(__name__)


NIFTY_SYMBOL = "^NSEI"

# yfinance returns OHLCV with columns: Open, High, Low, Close, Adj Close, Volume.
# We only need Close and Volume for Phase 3 metrics.

_PRE_BUFFER_DAYS = 45        # 30 trading days ≈ ~45 calendar days
_POST_BUFFER_DAYS = 14       # T+1 + holidays; 14 is plenty


@dataclass
class PriceWindow:
    """OHLCV slice around a filing's filing_date.

    Indexed by trading date (the actual NSE trading calendar — non-trading
    days are simply absent from `df.index`).
    """

    symbol_used: str           # the yfinance symbol that returned data (e.g. 'HDFCBANK.NS')
    df: pd.DataFrame           # DataFrame indexed by date, with Close + Volume columns

    @property
    def empty(self) -> bool:
        return self.df.empty


def resolve_yf_symbol(filing_symbol: str, source: str) -> tuple[str | None, str | None]:
    """Return (preferred_symbol, fallback_symbol) for a filing.

    preferred is what we try first; fallback is what we retry on empty data.
    Either may be None if no candidate exists.
    """
    if source == "TRENDLYNE":
        return None, None
    nse_ticker = to_nse_ticker(filing_symbol, source)
    if source == "NSE":
        # NSE filing: NSE ticker is always known. .NS preferred, .BO as fallback
        # (some illiquid NSE-listed stocks have richer BSE data).
        return f"{filing_symbol.upper()}.NS", f"{filing_symbol.upper()}.BO"
    if source == "BSE":
        if nse_ticker:
            return f"{nse_ticker}.NS", f"{filing_symbol}.BO"
        return f"{filing_symbol}.BO", None
    return None, None


def fetch_ohlcv(filing_symbol: str, source: str, filing_date: date) -> PriceWindow | None:
    """Fetch OHLCV around filing_date for a single stock.

    Returns:
        PriceWindow indexed by trading date covering T-45 calendar days to
        T+14 calendar days. Callers slice to T-30..T-1 / T+1 as needed.
        None if BOTH the preferred and fallback yfinance symbols returned
        empty data, OR if no symbol can be derived.
    """
    preferred, fallback = resolve_yf_symbol(filing_symbol, source)
    if preferred is None:
        log.warning(
            f"yfinance: no symbol for filing_symbol={filing_symbol!r} source={source}"
        )
        return None

    for candidate in (preferred, fallback):
        if candidate is None:
            continue
        df = _download(candidate, filing_date)
        if df is None or df.empty:
            log.info(f"yfinance: {candidate} returned empty for {filing_date}")
            continue
        return PriceWindow(symbol_used=candidate, df=df)

    log.warning(
        f"yfinance: no OHLCV for filing_symbol={filing_symbol!r} source={source} "
        f"(tried {preferred}, {fallback})"
    )
    return None


def fetch_nifty(filing_date: date) -> pd.DataFrame | None:
    """Nifty 50 closes around filing_date. None on fetch failure."""
    df = _download(NIFTY_SYMBOL, filing_date)
    if df is None or df.empty:
        log.warning(f"yfinance: Nifty (^NSEI) returned empty for {filing_date}")
        return None
    return df


# ---------------------------------------------------------------------------
# Window slicing helpers (used by enricher; pure functions)
# ---------------------------------------------------------------------------


def close_on_or_before(df: pd.DataFrame, target: date) -> float | None:
    """Most recent Close on or before `target`. None if no trading day in window."""
    if df.empty:
        return None
    target_ts = pd.Timestamp(target)
    before = df[df.index <= target_ts]
    if before.empty:
        return None
    return float(before["Close"].iloc[-1])


def close_on_or_after(df: pd.DataFrame, target: date) -> float | None:
    """First Close on or after `target`. None if no trading day in window."""
    if df.empty:
        return None
    target_ts = pd.Timestamp(target)
    after = df[df.index >= target_ts]
    if after.empty:
        return None
    return float(after["Close"].iloc[0])


def volume_on_or_after(df: pd.DataFrame, target: date) -> float | None:
    """First Volume on or after `target`."""
    if df.empty:
        return None
    target_ts = pd.Timestamp(target)
    after = df[df.index >= target_ts]
    if after.empty:
        return None
    return float(after["Volume"].iloc[0])


def avg_volume_window(
    df: pd.DataFrame, end_inclusive: date, window_trading_days: int
) -> float | None:
    """Average Volume over the most recent `window_trading_days` trading days
    ending on or before `end_inclusive`. None if window is incomplete (<50%
    of expected days)."""
    if df.empty:
        return None
    target_ts = pd.Timestamp(end_inclusive)
    before = df[df.index <= target_ts]
    if before.empty:
        return None
    slice_ = before.tail(window_trading_days)
    # Soft completeness check — yfinance can have gaps for recently listed
    # stocks. Require >= 50% coverage of the requested window.
    if len(slice_) < window_trading_days // 2:
        return None
    avg = float(slice_["Volume"].mean())
    return avg if avg > 0 else None


# ---------------------------------------------------------------------------
# Internal — yfinance call (one place for monkeypatching in tests)
# ---------------------------------------------------------------------------


def _download(symbol: str, filing_date: date) -> pd.DataFrame | None:
    start = filing_date - timedelta(days=_PRE_BUFFER_DAYS)
    end = filing_date + timedelta(days=_POST_BUFFER_DAYS)
    try:
        df = yf.download(
            symbol,
            start=start.isoformat(),
            end=end.isoformat(),
            progress=False,
            auto_adjust=True,
            actions=False,
            threads=False,
        )
    except Exception as e:  # noqa: BLE001 — yfinance raises a variety
        log.warning(f"yfinance: {symbol} download exception: {e}")
        return None
    if df is None or df.empty:
        return None
    # yfinance 0.2.x may return a multi-level column index when threads=True or
    # multiple tickers. Flatten if so.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # Keep only what we need; drop NaN rows defensively.
    keep = [c for c in ("Close", "Volume") if c in df.columns]
    if not keep:
        return None
    df = df[keep].dropna(how="all")
    return df
