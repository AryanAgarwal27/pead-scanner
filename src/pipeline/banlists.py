"""Manual ban/surveillance list loaders — Phase 4 (BRD §3.4 FR-4.3).

NSE publishes the F&O ban list daily and the ASM/GSM surveillance lists on
their own cadence. There is no stable public JSON feed for these (the NSE
site renders them server-side and the URLs rotate). v1 takes the BRD-
approved shortcut: maintain them as committed CSVs, manually refreshed by
the operator.

The loader uses an mtime-based cache so the filterer can be called many
times in a single rank_eod run without re-reading the CSVs. mtime check is
cheap; if the operator edits the CSV mid-job (unlikely) the next call
picks up the change.

CSV formats:
    src/data/fno_ban.csv:
        symbol,effective_date,source_url
        ICICIBANK,2026-05-20,https://www.nseindia.com/...
    src/data/asm_gsm.csv:
        symbol,list_type,stage,effective_date
        SUZLON,ASM,4,2026-05-15

Symbol matching is on the canonical NSE ticker (uppercased). The filterer
calls these with the post-dedup `symbol_nse` so BSE scrip codes are never
matched directly.

Maintenance: see README §"Manual list maintenance" for source URLs and
refresh cadence.
"""

from __future__ import annotations

import csv
from pathlib import Path

from src import config
from src.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal mtime-based cache
# ---------------------------------------------------------------------------

_CACHE: dict[Path, tuple[float, frozenset[str]]] = {}


def _load(path: Path, symbol_column: str = "symbol") -> frozenset[str]:
    """Read a CSV and return the set of normalized NSE tickers in column
    `symbol_column`. Re-reads only if the file's mtime changed.

    Missing file (or empty file) → empty set + a one-time INFO log.
    Returns frozenset for cheap, hashable identity comparisons.
    """
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        log.info(f"banlists: {path} not found — treating as empty list")
        return frozenset()

    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    symbols: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = (row.get(symbol_column) or "").strip().upper()
                if sym:
                    symbols.add(sym)
    except OSError as e:
        log.warning(f"banlists: failed to read {path}: {e} — treating as empty")
        return frozenset()

    result = frozenset(symbols)
    _CACHE[path] = (mtime, result)
    log.info(f"banlists: loaded {len(result)} entries from {path.name}")
    return result


# ---------------------------------------------------------------------------
# Public API — used by src.pipeline.filterer
# ---------------------------------------------------------------------------


def fno_ban_symbols() -> frozenset[str]:
    """Return the current F&O ban list as a set of NSE tickers."""
    return _load(config.FNO_BAN_CSV)


def asm_gsm_symbols() -> frozenset[str]:
    """Return the union of ASM + GSM listed symbols as NSE tickers.

    BRD §3.4 FR-4.3 treats ASM and GSM identically — both cause exclusion.
    The CSV's `list_type` and `stage` columns are informational only (kept
    for audit / future tier-based handling); the filter is a flat membership
    check.
    """
    return _load(config.ASM_GSM_CSV)


def clear_cache() -> None:
    """Force a re-read on next access. Used by tests."""
    _CACHE.clear()
