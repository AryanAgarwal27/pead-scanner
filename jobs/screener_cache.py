"""Nightly Screener.in cache refresh — Phase 3 (BRD §5.2).

Runs at 23:30 IST (18:00 UTC) every day. Two stages:

    1. Refresh the BSE↔NSE symbol map (joins BSE equity list ⨝ NSE EQUITY_L
       on ISIN). Weekly cadence — only fires if the cache file is missing or
       older than 7 days.
    2. For every NSE ticker referenced by a filing in the last 90 days,
       fetch Screener fundamentals (consolidated → standalone fallback).
       Store 8 quarters of PAT/Revenue/OPM in `fundamentals` table.

Etiquette (BRD §3.1 FR-1.6): never hits Screener during the day. One-second
sleep between symbols. Negative-cache TTL: a symbol that 404s gets a
fundamentals row with on_screener=false and last_404_at; subsequent runs
skip it for 30 days.

Idempotent — every row uses UPSERT keyed on `symbol`.

Usage:
    python jobs/screener_cache.py
    python jobs/screener_cache.py --symbols HDFCBANK,RELIANCE  # ad hoc subset
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.db.client import get_client
from src.sources import symbol_map
from src.sources.screener import ScreenerNotFound, fetch_fundamentals
from src.sources.symbol_map import to_nse_ticker
from src.utils.logging import get_logger

log = get_logger("screener_cache")

# How long to keep on_screener=false rows before re-checking.
NEG_CACHE_TTL_DAYS = 30
# Weekly cadence for the BSE↔NSE map refresh.
SYMBOL_MAP_TTL_DAYS = 7
# Politeness sleep between Screener requests.
PER_SYMBOL_SLEEP_S = 1.0
# Filings older than this aren't scraped for fundamentals (likely already enriched
# or aged out — see ENRICH_WINDOW_DAYS in enricher.py).
FILINGS_LOOKBACK_DAYS = 90

_SYMBOL_MAP_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "bse_to_nse.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Screener.in fundamentals cache.")
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated NSE tickers to refresh (overrides automatic selection).",
    )
    parser.add_argument(
        "--skip-symbol-map",
        action="store_true",
        help="Skip the BSE↔NSE map refresh stage.",
    )
    args = parser.parse_args()

    db = get_client()

    if not args.skip_symbol_map:
        _maybe_refresh_symbol_map()

    if args.symbols:
        tickers = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        tickers = _select_tickers_from_filings(db)

    log.info(f"screener-cache: {len(tickers)} tickers to consider")

    status: Counter[str] = Counter()
    for ticker in tickers:
        if _negative_cache_active(db, ticker):
            status["skipped_404_ttl"] += 1
            continue
        try:
            fundamentals = fetch_fundamentals(ticker)
        except ScreenerNotFound:
            _write_negative_cache(db, ticker)
            status["404_negative_cached"] += 1
            log.info(f"  {ticker}: 404 — negative-cache set")
        except Exception as e:  # noqa: BLE001
            status["error"] += 1
            log.warning(f"  {ticker}: fetch failed: {e}")
        else:
            _upsert_fundamentals(db, fundamentals)
            status["ok"] += 1
            log.info(
                f"  {ticker}: OK (basis={fundamentals.used_basis}, "
                f"quarters_pat={len(fundamentals.quarterly_pat)}, "
                f"market_cap_cr={fundamentals.market_cap_cr})"
            )
        time.sleep(PER_SYMBOL_SLEEP_S)

    log.info(f"screener-cache done: {dict(status)}")
    return 0


# ---------------------------------------------------------------------------
# Stage 1 — symbol map
# ---------------------------------------------------------------------------


def _maybe_refresh_symbol_map() -> None:
    """Rebuild bse_to_nse.json if missing or older than SYMBOL_MAP_TTL_DAYS."""
    if _SYMBOL_MAP_PATH.exists():
        age = datetime.now(UTC) - datetime.fromtimestamp(
            _SYMBOL_MAP_PATH.stat().st_mtime, tz=UTC
        )
        if age < timedelta(days=SYMBOL_MAP_TTL_DAYS):
            log.info(
                f"symbol map fresh (age={age.days}d, TTL={SYMBOL_MAP_TTL_DAYS}d); skipping refresh"
            )
            return
    log.info("Refreshing BSE-to-NSE symbol map")
    try:
        n = symbol_map.refresh_from_exchanges()
        log.info(f"symbol map refreshed: {n} mappings")
    except Exception as e:  # noqa: BLE001
        log.warning(f"symbol map refresh failed (continuing with stale cache): {e}")


# ---------------------------------------------------------------------------
# Stage 2 — fundamentals
# ---------------------------------------------------------------------------


def _select_tickers_from_filings(db) -> list[str]:
    """Distinct NSE tickers (resolved from BSE scrip codes) referenced by
    filings in the last FILINGS_LOOKBACK_DAYS days."""
    cutoff = (datetime.now(UTC) - timedelta(days=FILINGS_LOOKBACK_DAYS)).isoformat()
    resp = (
        db.table("filings")
        .select("symbol, source")
        .gte("filing_time", cutoff)
        .execute()
    )
    rows = resp.data or []
    tickers: set[str] = set()
    for r in rows:
        nse = to_nse_ticker(r["symbol"], r["source"])
        if nse:
            tickers.add(nse)
    return sorted(tickers)


def _negative_cache_active(db, ticker: str) -> bool:
    """Skip if we already 404'd this ticker within the TTL window."""
    cutoff = (datetime.now(UTC) - timedelta(days=NEG_CACHE_TTL_DAYS)).isoformat()
    resp = (
        db.table("fundamentals")
        .select("on_screener, last_404_at")
        .eq("symbol", ticker)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return False
    row = rows[0]
    if row.get("on_screener"):
        return False
    last_404 = row.get("last_404_at")
    if not last_404:
        return False
    return last_404 >= cutoff


def _write_negative_cache(db, ticker: str) -> None:
    now = datetime.now(UTC).isoformat()
    db.table("fundamentals").upsert(
        {
            "symbol": ticker,
            "on_screener": False,
            "last_404_at": now,
            "fetched_at": now,
            "company_name": None,
            "market_cap_cr": None,
            "sector": None,
            "quarterly_pat": None,
            "quarterly_rev": None,
            "quarterly_opm": None,
        },
        on_conflict="symbol",
    ).execute()


def _upsert_fundamentals(db, f) -> None:
    db.table("fundamentals").upsert(
        {
            "symbol": f.symbol,
            "company_name": f.company_name,
            "market_cap_cr": f.market_cap_cr,
            "sector": f.sector,
            "quarterly_pat": f.quarterly_pat,
            "quarterly_rev": f.quarterly_rev,
            "quarterly_opm": f.quarterly_opm,
            "on_screener": True,
            "last_404_at": None,
            "fetched_at": datetime.now(UTC).isoformat(),
        },
        on_conflict="symbol",
    ).execute()


if __name__ == "__main__":
    sys.exit(main())
