"""BSE scrip code <-> NSE ticker resolution — Phase 3.

The enricher needs to translate `filings.symbol` (which is an NSE ticker for
NSE-sourced rows, a BSE scrip code for BSE-sourced rows, and a 'TL-<slug>'
for Trendlyne rows) into a canonical NSE ticker before:
  - fetching Screener fundamentals (`src.sources.screener`)
  - fetching primary yfinance OHLCV (`src.sources.yfinance_adapter`)

Cache layout (src/data/bse_to_nse.json):
    { "<bse_scrip>": "<nse_ticker>", ... }

The cache is populated by joining BSE's equity list with NSE's EQUITY_L.csv
on ISIN — that's the only authoritative key shared by both exchanges. BSE-
only stocks (no NSE listing) produce no entry; `to_nse_ticker` returns None
for them, which puts the downstream enricher on the `on_screener=false` path
and yfinance's `.BO` fallback.

Refresh cadence: weekly, via the screener-cache job's bootstrap stage
(BRD §5.2). For manual one-off:
    python -m src.sources.symbol_map refresh

Phase 3 acceptable state: the JSON ships empty; first run of screener-cache
populates it. Until then, BSE-source filings have SUE/Margin_Delta = None
in their metrics rows — that's the documented graceful degradation.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import requests

from src.utils.logging import get_logger
from src.utils.retry import with_retries

log = get_logger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CACHE_FILE = _DATA_DIR / "bse_to_nse.json"

# ---------------------------------------------------------------------------
# Endpoints (public, no auth)
# ---------------------------------------------------------------------------

# BSE's full equity list with ISIN — JSON, ~5K rows. Includes SCRIP_CD + ISIN_NUMBER.
_BSE_EQUITY_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
_BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

# NSE's CSV master list — SYMBOL, NAME OF COMPANY, ..., ISIN NUMBER.
_NSE_EQUITY_CSV_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/market-data/securities-available-for-trading",
}


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_CACHE: dict[str, str] | None = None


def _load_cache() -> dict[str, str]:
    global _CACHE
    if _CACHE is None:
        if _CACHE_FILE.exists():
            try:
                _CACHE = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                log.warning(f"bse_to_nse.json corrupted ({e}); starting empty")
                _CACHE = {}
        else:
            _CACHE = {}
    return _CACHE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def to_nse_ticker(symbol: str, source: str) -> str | None:
    """Resolve a filing's (symbol, source) → canonical NSE ticker.

    Returns:
      - NSE ticker (uppercase) on success
      - None for BSE-only stocks (no NSE listing) or unresolved Trendlyne slugs

    Never raises; missing cache → returns None.
    """
    if source == "NSE":
        return symbol.upper().strip()
    if source == "BSE":
        cache = _load_cache()
        return cache.get(symbol.strip())
    return None  # TRENDLYNE or unknown


def refresh_from_exchanges() -> int:
    """Rebuild the BSE-to-NSE map by joining BSE's equity list ⨝ NSE's EQUITY_L
    on ISIN. Writes src/data/bse_to_nse.json. Returns count of mappings.

    Idempotent — safe to call from screener-cache nightly. If either source
    fails, the existing cache is left untouched.
    """
    log.info("Refreshing BSE↔NSE map from public exchange lists")
    try:
        bse_rows = _fetch_bse_equity_list()
        nse_rows = _fetch_nse_equity_list()
    except Exception as e:
        log.error(f"symbol_map refresh failed (cache untouched): {e}")
        raise

    isin_to_nse: dict[str, str] = {}
    for row in nse_rows:
        # NSE CSV uses ' ISIN NUMBER' (with leading space, no kidding). Match flexibly.
        isin = (
            row.get("ISIN NUMBER")
            or row.get(" ISIN NUMBER")
            or row.get("ISIN")
            or ""
        ).strip()
        symbol = (row.get("SYMBOL") or row.get(" SYMBOL") or "").strip()
        if isin and symbol:
            isin_to_nse[isin] = symbol

    mapping: dict[str, str] = {}
    for row in bse_rows:
        scrip = str(row.get("SCRIP_CD") or row.get("scrip_cd") or "").strip()
        isin = (row.get("ISIN_NUMBER") or row.get("isin_number") or "").strip()
        if not scrip or not isin:
            continue
        nse_ticker = isin_to_nse.get(isin)
        if nse_ticker:
            mapping[scrip] = nse_ticker

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(mapping, indent=0, sort_keys=True), encoding="utf-8")

    global _CACHE
    _CACHE = mapping
    log.info(
        f"BSE-to-NSE map refreshed: {len(mapping)} mappings "
        f"(BSE rows: {len(bse_rows)}, NSE rows: {len(nse_rows)})"
    )
    return len(mapping)


# ---------------------------------------------------------------------------
# Source fetchers
# ---------------------------------------------------------------------------


def _fetch_bse_equity_list() -> list[dict]:
    """BSE's full equity list as JSON. Returns the 'Table' array, ~5K rows."""

    def _do() -> requests.Response:
        return requests.get(_BSE_EQUITY_LIST_URL, headers=_BSE_HEADERS, timeout=30)

    resp = with_retries(_do)
    resp.raise_for_status()
    payload = resp.json()
    # BSE returns either a bare list or { "Table": [...] } depending on endpoint version.
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("Table") or []
    log.warning(f"BSE equity list returned unexpected shape: {type(payload).__name__}")
    return []


def _fetch_nse_equity_list() -> list[dict]:
    """NSE's EQUITY_L.csv — public CSV download. Returns parsed dict rows."""
    sess = requests.Session()
    sess.get("https://www.nseindia.com/", headers=_NSE_HEADERS, timeout=15)

    def _do() -> requests.Response:
        return sess.get(_NSE_EQUITY_CSV_URL, headers=_NSE_HEADERS, timeout=30)

    resp = with_retries(_do)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


# ---------------------------------------------------------------------------
# CLI: `python -m src.sources.symbol_map refresh`
# ---------------------------------------------------------------------------


def _cli() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "refresh":
        print("usage: python -m src.sources.symbol_map refresh", file=sys.stderr)
        return 2
    n = refresh_from_exchanges()
    print(f"OK - {n} BSE-to-NSE mappings written to {_CACHE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
