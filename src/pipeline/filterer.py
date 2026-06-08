"""Hard filters for the daily ranking cohort — Phase 4 (BRD §3.4 FR-4.3).

Stocks failing ANY filter are dropped from the cohort *before* z-score
normalization, so they don't influence the cohort statistics for stocks
that pass.

Filter chain (order chosen so cheap in-memory checks fire before yfinance):

    1. Parser confidence floor       — in-memory, from filings row
    2. has_exceptional_items         — in-memory (BRD §3.3 FR-3.8)
    3. F&O ban list                  — in-memory CSV lookup
    4. ASM/GSM list                  — in-memory CSV lookup
    5. Market cap ≥ ₹500 Cr          — in-memory, from fundamentals row
    6. avg 30-day turnover ≥ ₹5 Cr   — yfinance (lazy, written back to metrics)
    7. Listed ≥ 2 years              — yfinance (combined with #6's fetch)

Steps 6 and 7 share a single yfinance fetch (the same 60-day OHLCV window
covers both the turnover average and the listing-age probe). The result is
cached: turnover lands in metrics.avg_30d_turnover_cr, listing-age lands in
fundamentals.listed_long_enough. The next ranker run skips yfinance for
rows already populated.

Listing-age policy (per Phase 4 decisions):
    * yfinance fails after retries → INCLUDE the filing (fail-open). The
      filing might be from a legit liquid stock and we don't want a network
      flake to silently drop it.
    * BSE-only stocks (no NSE listing) are already dropped by the market-cap
      filter (no fundamentals row); the listing-age probe is therefore
      skipped for them — they never reach this step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from src import config
from src.pipeline import banlists
from src.sources import yfinance_adapter as yfa
from src.sources.symbol_map import to_nse_ticker
from src.utils.logging import get_logger

log = get_logger(__name__)


# yfinance window: 60 calendar days covers ~40 trading days. We need 30
# trading days for the turnover average and the simple ≥730-day-history
# check for listing age. The listing-age probe uses a separate, larger fetch
# (`period="3y"`) only when the first probe is inconclusive.
_TURNOVER_WINDOW_TRADING_DAYS = 30
_TURNOVER_WINDOW_CALENDAR_DAYS = 60
_LISTING_AGE_DAYS = 365 * config.MIN_LISTING_YEARS  # 730


# Drop reasons — single source of truth so the summary log lines stay consistent.
DROP_CONFIDENCE = "parser_confidence"
DROP_EXCEPTIONAL = "exceptional_items"
DROP_FNO_BAN = "fno_ban"
DROP_ASM_GSM = "asm_gsm"
DROP_MARKET_CAP = "market_cap"
DROP_TURNOVER = "turnover"
DROP_LISTING_AGE = "listing_age"


@dataclass
class FilterOutcome:
    """One per cohort row processed."""

    filing_id: int
    symbol_nse: str | None     # None when filing has no resolvable NSE ticker
    passed: bool
    drop_reason: str | None
    # Populated when the yfinance probe ran (so the ranker can write them back to DB):
    avg_30d_turnover_cr: float | None = None
    listed_long_enough: bool | None = None
    turnover_was_computed: bool = False           # True = this run did the yfinance call
    listing_check_was_computed: bool = False


def filter_cohort(
    db, rows: list[dict[str, Any]], *, dry_run: bool = False
) -> list[FilterOutcome]:
    """Apply the hard-filter chain to every row.

    Args:
        db: supabase client. Used for lazy write-back of turnover + listing-age
            cache. Pass anything with a `.table(...).update(...).eq(...).execute()`
            chain. When `dry_run=True`, db is never written to.
        rows: cohort rows. Each must contain at minimum:
              { filing_id, symbol, source, parser_confidence,
                has_exceptional_items, filing_time,
                fundamentals: { market_cap_cr, listed_long_enough } | None,
                metrics: { avg_30d_turnover_cr } | None }
        dry_run: if True, no DB writes.

    Returns one FilterOutcome per row. The ranker reads `.passed` to pick
    survivors and `.drop_reason` for summary logging.
    """
    fno = banlists.fno_ban_symbols()
    asm = banlists.asm_gsm_symbols()
    outcomes: list[FilterOutcome] = []

    for r in rows:
        outcome = _apply_one(db, r, fno=fno, asm=asm, dry_run=dry_run)
        outcomes.append(outcome)
    return outcomes


# ---------------------------------------------------------------------------
# Per-row evaluation
# ---------------------------------------------------------------------------


def _apply_one(
    db,
    r: dict[str, Any],
    *,
    fno: frozenset[str],
    asm: frozenset[str],
    dry_run: bool,
) -> FilterOutcome:
    filing_id = int(r["filing_id"])
    symbol = str(r["symbol"])
    source = str(r["source"])
    nse_ticker = to_nse_ticker(symbol, source)

    # ---- 1. Parser confidence floor ---------------------------------------
    confidence = r.get("parser_confidence")
    if confidence not in config.PARSER_CONFIDENCE_FLOOR:
        return _drop(filing_id, nse_ticker, DROP_CONFIDENCE,
                     extra=f"confidence={confidence!r}")

    # ---- 2. Exceptional items --------------------------------------------
    if r.get("has_exceptional_items"):
        return _drop(filing_id, nse_ticker, DROP_EXCEPTIONAL)

    # ---- Ban-list filters need a canonical NSE ticker ---------------------
    if nse_ticker is None:
        # No NSE listing → no market-cap (Screener is NSE-only) → drop on cap
        # filter anyway. Skip ban-list checks to keep the trace clean.
        return _drop(filing_id, None, DROP_MARKET_CAP,
                     extra="bse-only or unresolved (no NSE ticker)")

    # ---- 3. F&O ban -------------------------------------------------------
    if nse_ticker in fno:
        return _drop(filing_id, nse_ticker, DROP_FNO_BAN)

    # ---- 4. ASM / GSM -----------------------------------------------------
    if nse_ticker in asm:
        return _drop(filing_id, nse_ticker, DROP_ASM_GSM)

    # ---- 5. Market cap (from fundamentals row) ----------------------------
    fundamentals = r.get("fundamentals") or {}
    mcap = _as_float(fundamentals.get("market_cap_cr"))
    if mcap is None or mcap < config.MIN_MARKET_CAP_CR:
        return _drop(filing_id, nse_ticker, DROP_MARKET_CAP,
                     extra=f"market_cap_cr={mcap}")

    # ---- 6 + 7. yfinance-dependent filters (turnover + listing age) ------
    metrics_row = r.get("metrics") or {}
    cached_turnover = _as_float(metrics_row.get("avg_30d_turnover_cr"))
    cached_listing = fundamentals.get("listed_long_enough")  # None | True | False
    filing_date = _parse_filing_date(r["filing_time"])

    need_probe = cached_turnover is None or cached_listing is None
    turnover_cr = cached_turnover
    listed_ok = cached_listing
    turnover_was_computed = False
    listing_check_was_computed = False

    if need_probe:
        probe = _probe_yfinance(symbol, source, filing_date)
        if probe is not None:
            if cached_turnover is None and probe["turnover_cr"] is not None:
                turnover_cr = probe["turnover_cr"]
                turnover_was_computed = True
                if not dry_run:
                    _write_turnover(db, filing_id, turnover_cr)
            if cached_listing is None:
                listed_ok = probe["listed_long_enough"]
                listing_check_was_computed = True
                if not dry_run:
                    _write_listing_age(db, nse_ticker, listed_ok)
        else:
            # Fetch failure: fail-open on listing-age (per plan), drop on
            # turnover (we can't verify the ₹5 Cr floor without data).
            log.warning(
                f"filterer: yfinance probe failed for filing_id={filing_id} "
                f"symbol={symbol} ({source}) — failing open on listing age, "
                f"dropping on turnover"
            )
            if cached_listing is None:
                listed_ok = True  # fail-open

    # 6. Turnover check (must be present and ≥ floor)
    if turnover_cr is None or turnover_cr < config.MIN_DAILY_TURNOVER_CR:
        return FilterOutcome(
            filing_id=filing_id,
            symbol_nse=nse_ticker,
            passed=False,
            drop_reason=DROP_TURNOVER,
            avg_30d_turnover_cr=turnover_cr,
            listed_long_enough=listed_ok,
            turnover_was_computed=turnover_was_computed,
            listing_check_was_computed=listing_check_was_computed,
        )

    # 7. Listing age (fail-open: None or True both pass)
    if listed_ok is False:
        return FilterOutcome(
            filing_id=filing_id,
            symbol_nse=nse_ticker,
            passed=False,
            drop_reason=DROP_LISTING_AGE,
            avg_30d_turnover_cr=turnover_cr,
            listed_long_enough=listed_ok,
            turnover_was_computed=turnover_was_computed,
            listing_check_was_computed=listing_check_was_computed,
        )

    return FilterOutcome(
        filing_id=filing_id,
        symbol_nse=nse_ticker,
        passed=True,
        drop_reason=None,
        avg_30d_turnover_cr=turnover_cr,
        listed_long_enough=listed_ok,
        turnover_was_computed=turnover_was_computed,
        listing_check_was_computed=listing_check_was_computed,
    )


# ---------------------------------------------------------------------------
# yfinance probe — single fetch powers both turnover + listing age.
# ---------------------------------------------------------------------------


def _probe_yfinance(
    symbol: str, source: str, filing_date: date
) -> dict[str, Any] | None:
    """Fetch once, compute both turnover and listing-age.

    Returns None on fetch failure (caller handles fail-open/closed per filter).
    """
    pw = yfa.fetch_ohlcv(symbol, source, filing_date)
    if pw is None or pw.empty:
        return None

    df: pd.DataFrame = pw.df

    # ---- Turnover: prior-30-trading-day mean of Close × Volume in ₹ Cr ----
    # The enricher already pulled a T-45..T+14 window for vol_spike; reuse the
    # same slicing logic so turnover is computed over the SAME days as
    # vol_spike's denominator. We slice to days strictly before filing_date.
    cutoff_ts = pd.Timestamp(filing_date - timedelta(days=1))
    prior = df[df.index <= cutoff_ts].tail(_TURNOVER_WINDOW_TRADING_DAYS)
    turnover_cr: float | None = None
    if "Close" in prior.columns and "Volume" in prior.columns and not prior.empty:
        # Drop rows missing either; need both for the product.
        slice_ = prior.dropna(subset=["Close", "Volume"])
        if len(slice_) >= _TURNOVER_WINDOW_TRADING_DAYS // 2:
            # ₹ Cr = (price × volume) / 1e7
            daily_turnover_cr = (slice_["Close"] * slice_["Volume"]) / 1e7
            avg = float(daily_turnover_cr.mean())
            turnover_cr = avg if avg > 0 else None

    # ---- Listing age: does history reach back ≥730 days? -----------------
    # The default fetch only goes T-45..T+14, which can't answer this for
    # established stocks. Issue a second, longer pull when the short window
    # is inconclusive (i.e. the earliest date in df is recent).
    earliest = df.index.min()
    earliest_dt = (
        earliest.date() if isinstance(earliest, pd.Timestamp) else None
    )
    listed_ok = _listed_long_enough(symbol, source, filing_date, earliest_dt)

    return {"turnover_cr": turnover_cr, "listed_long_enough": listed_ok}


def _listed_long_enough(
    symbol: str, source: str, filing_date: date, earliest_in_short_window: date | None
) -> bool | None:
    """Return True/False if we can decide; None on probe failure (fail-open
    handled by caller).

    Short-circuit: if the short window's earliest row is already older than
    filing_date - 730d, we know listing age is fine without a second fetch.
    """
    threshold = filing_date - timedelta(days=_LISTING_AGE_DAYS)
    if earliest_in_short_window is not None and earliest_in_short_window <= threshold:
        return True

    # Need a longer history pull. Use a 3-year window centered behind filing_date.
    long_pw = yfa.fetch_ohlcv(
        symbol, source, filing_date - timedelta(days=_LISTING_AGE_DAYS + 30)
    )
    if long_pw is None or long_pw.empty:
        return None
    long_earliest = long_pw.df.index.min()
    if isinstance(long_earliest, pd.Timestamp):
        return long_earliest.date() <= threshold
    return None


# ---------------------------------------------------------------------------
# DB write-backs (idempotent — both target existing rows by PK)
# ---------------------------------------------------------------------------


def _write_turnover(db, filing_id: int, turnover_cr: float) -> None:
    try:
        db.table("metrics").update({"avg_30d_turnover_cr": turnover_cr}).eq(
            "filing_id", filing_id
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.warning(f"filterer: failed to persist turnover for filing_id={filing_id}: {e}")


def _write_listing_age(db, nse_ticker: str, listed_ok: bool | None) -> None:
    if listed_ok is None:
        return  # nothing decisive to persist
    try:
        db.table("fundamentals").update({"listed_long_enough": listed_ok}).eq(
            "symbol", nse_ticker
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.warning(
            f"filterer: failed to persist listed_long_enough for {nse_ticker}: {e}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drop(
    filing_id: int, nse_ticker: str | None, reason: str, *, extra: str | None = None
) -> FilterOutcome:
    if extra:
        log.info(f"filterer: drop filing_id={filing_id} reason={reason} ({extra})")
    else:
        log.info(f"filterer: drop filing_id={filing_id} reason={reason}")
    return FilterOutcome(
        filing_id=filing_id,
        symbol_nse=nse_ticker,
        passed=False,
        drop_reason=reason,
    )


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_filing_date(ts: Any) -> date:
    """filing_time from Supabase comes back as an ISO string; cast to date."""
    if isinstance(ts, datetime):
        return ts.astimezone(UTC).date()
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC).date()
    raise TypeError(f"Unexpected filing_time type: {type(ts).__name__}")
