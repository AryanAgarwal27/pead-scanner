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


# ===========================================================================
# Phase 5 — signal-generation price helpers.
#
# These are ADDITIVE. They do not touch _download / fetch_ohlcv / fetch_nifty
# (Phase 3's tested path keeps its exact signature + Close/Volume-only shape).
# Phase 5 needs strictly more than Phase 3:
#   * High/Low of the T+1 candle for entry/stop levels (FR-5.1)
#   * corporate-action ex-dates around T+1 for confirmation C5 (FR-5.5)
#   * Nifty 50 vs its 50-DMA for the market-regime confirmation C2 (FR-5.5)
# so they ride on a separate, OHLC+actions download (`_download_full`).
# ===========================================================================

# 50-DMA needs ~50 trading days ≈ ~75 calendar days; pad generously.
_REGIME_LOOKBACK_DAYS = 110
_NIFTY_DMA_WINDOW = 50


@dataclass
class SignalWindow:
    """OHLC + corporate-actions slice around a filing's filing_date.

    Like PriceWindow but carries the full OHLC (entry = T+1 high, stop = T+1
    low) plus Dividends / Stock Splits columns (confirmation C5). Indexed by
    the NSE trading calendar — non-trading days are absent from df.index.
    """

    symbol_used: str
    df: pd.DataFrame           # Open, High, Low, Close, Volume [, Dividends, Stock Splits]

    @property
    def empty(self) -> bool:
        return self.df.empty


def fetch_signal_window(
    filing_symbol: str, source: str, filing_date: date
) -> SignalWindow | None:
    """Fetch OHLC + corporate actions around filing_date for one stock.

    Symbol resolution and the .NS↔.BO fallback exactly mirror fetch_ohlcv —
    we reuse resolve_yf_symbol so Phase 5 stays consistent with Phase 3's
    canonicalization. Returns None if no symbol resolves or both candidates
    return empty data.
    """
    preferred, fallback = resolve_yf_symbol(filing_symbol, source)
    if preferred is None:
        log.warning(
            f"yfinance(signal): no symbol for filing_symbol={filing_symbol!r} source={source}"
        )
        return None

    for candidate in (preferred, fallback):
        if candidate is None:
            continue
        df = _download_full(candidate, filing_date)
        if df is None or df.empty:
            log.info(f"yfinance(signal): {candidate} returned empty for {filing_date}")
            continue
        return SignalWindow(symbol_used=candidate, df=df)

    log.warning(
        f"yfinance(signal): no OHLC for filing_symbol={filing_symbol!r} source={source} "
        f"(tried {preferred}, {fallback})"
    )
    return None


def candle_on_or_after(df: pd.DataFrame, target: date) -> dict[str, float] | None:
    """OHLC of the first trading day on or after `target` (the T+1 candle).

    Returns {'open','high','low','close'} or None if there's no trading day in
    the window or High/Low are missing.
    """
    if df.empty:
        return None
    target_ts = pd.Timestamp(target)
    after = df[df.index >= target_ts]
    if after.empty:
        return None
    row = after.iloc[0]
    try:
        high = float(row["High"])
        low = float(row["Low"])
    except (KeyError, TypeError, ValueError):
        return None
    if pd.isna(high) or pd.isna(low):
        return None
    out = {"high": high, "low": low}
    for col in ("Open", "Close"):
        if col in after.columns:
            v = row[col]
            out[col.lower()] = float(v) if not pd.isna(v) else None
    return out


def corporate_action_within(
    df: pd.DataFrame, center: date, window_trading_days: int
) -> bool:
    """True if a dividend / split ex-date falls within ±window_trading_days of
    the first trading day on or after `center` (confirmation C5, FR-5.5).

    Conservative on missing data: if the actions columns are absent (some
    yfinance responses omit them when there were none), returns False — i.e.
    "no corporate action seen", which lets C5 PASS. The signaler treats an
    inconclusive C5 as a soft pass, never a hard skip (only C2/C4 are hard).
    """
    if df.empty:
        return False
    action_cols = [c for c in ("Dividends", "Stock Splits") if c in df.columns]
    if not action_cols:
        return False

    target_ts = pd.Timestamp(center)
    on_or_after = df[df.index >= target_ts]
    if on_or_after.empty:
        return False
    center_pos = df.index.get_loc(on_or_after.index[0])
    lo = max(0, center_pos - window_trading_days)
    hi = min(len(df), center_pos + window_trading_days + 1)
    window = df.iloc[lo:hi]
    for col in action_cols:
        series = window[col].fillna(0.0)
        if (series != 0).any():
            return True
    return False


def fetch_nifty_regime(as_of: date) -> tuple[float, float, bool] | None:
    """Nifty 50 close vs its 50-DMA as of `as_of` (confirmation C2, FR-5.5).

    Returns (close, dma50, is_above) where is_above = close > dma50, or None
    if the fetch fails or there aren't 50 trading days of history. Computed
    ONCE per signal run and applied to every signal — the market regime is a
    single property of the run date. (For an --as-of replay over a multi-date
    cohort this is approximate: it uses the as-of date's regime for all rows
    rather than each filing's own T+1 regime. Acceptable for replays.)
    """
    start = as_of - timedelta(days=_REGIME_LOOKBACK_DAYS)
    end = as_of + timedelta(days=3)
    try:
        df = yf.download(
            NIFTY_SYMBOL,
            start=start.isoformat(),
            end=end.isoformat(),
            progress=False,
            auto_adjust=True,
            actions=False,
            threads=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"yfinance: Nifty regime download exception: {e}")
        return None
    if df is None or df.empty:
        log.warning(f"yfinance: Nifty regime returned empty for as_of={as_of}")
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return None
    closes = df["Close"].dropna()
    # Only consider trading days up to as_of (don't peek past the run date).
    closes = closes[closes.index <= pd.Timestamp(as_of)]
    if len(closes) < _NIFTY_DMA_WINDOW:
        log.warning(
            f"yfinance: Nifty regime has {len(closes)} closes < {_NIFTY_DMA_WINDOW} "
            f"needed for the 50-DMA (as_of={as_of})"
        )
        return None
    dma50 = float(closes.tail(_NIFTY_DMA_WINDOW).mean())
    close = float(closes.iloc[-1])
    return close, dma50, close > dma50


def _download_full(symbol: str, filing_date: date) -> pd.DataFrame | None:
    """OHLC + actions download for the signal window. Separate from _download
    so Phase 3's Close/Volume-only behavior is never perturbed."""
    start = filing_date - timedelta(days=_PRE_BUFFER_DAYS)
    end = filing_date + timedelta(days=_POST_BUFFER_DAYS)
    try:
        df = yf.download(
            symbol,
            start=start.isoformat(),
            end=end.isoformat(),
            progress=False,
            auto_adjust=False,   # raw OHLC — entry/stop are actual candle levels
            actions=True,        # Dividends + Stock Splits columns for C5
            threads=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"yfinance(signal): {symbol} download exception: {e}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    keep = [
        c
        for c in ("Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits")
        if c in df.columns
    ]
    if "High" not in keep or "Low" not in keep:
        return None
    df = df[keep].dropna(subset=["High", "Low"])
    return df
