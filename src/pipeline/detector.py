"""Multi-source detector — Phase 2.

Failover semantics (per Phase 2 plan Q1):
    - NSE and BSE are BOTH polled every run (parallel primaries — FR-1.2's
      "supplementary primary" means NSE complements BSE, not replaces it).
    - Trendlyne is invoked ONLY when BOTH primaries errored. A primary that
      returns zero rows on a quiet day is NOT a failure.
    - Cross-source duplicates (NSE ticker "HDFCBANK" + BSE scrip "500180" for
      the same filing) are NOT deduplicated — accepted Phase 2 limitation;
      see Phase 2 Q3 decision. Within-source duplicates (same row twice from a
      single source's pagination/race) ARE deduplicated by (source, symbol,
      quarter).

Side effects per run:
    - One source_health row per source actually contacted (with `error_msg`
      formatted as either `latency_ms=N` on success or the raw exception on
      failure). Per Q5 decision, latency is captured here even on success so
      degradation can be diagnosed before NSE/BSE actually errors out.
    - Up to N+1 error alerts (one per failed source), each respecting the
      1-per-hour rate limit (src.utils.rate_limit).
"""

import time
from datetime import UTC, date, datetime

from src import config
from src.sources.base import Filing, FilingsSource
from src.sources.bse import BseSource
from src.sources.nse import NseSource
from src.sources.trendlyne import TrendlyneSource
from src.utils.logging import get_logger
from src.utils.rate_limit import maybe_alert_error

log = get_logger(__name__)


def detect_filings(db, notifier, target_date: date) -> list[Filing]:
    """Orchestrate NSE + BSE primaries, fall back to Trendlyne on dual failure.

    `notifier` may be None (e.g. during --dry-run) — error alerts are then
    logged to stdout via rate_limit but not sent to Telegram.
    """
    run_at = datetime.now(UTC)
    primaries: list[FilingsSource] = [NseSource(), BseSource()]
    fallback: FilingsSource = TrendlyneSource()

    merged: list[Filing] = []
    primary_any_success = False

    for source in primaries:
        filings, latency_ms, error = _run_source(source, target_date)
        _write_source_health(
            db, run_at, source.name, error is None, error, len(filings), latency_ms
        )
        if error is None:
            primary_any_success = True
            merged.extend(filings)
            log.info(f"[{source.name}] OK: {len(filings)} filings in {latency_ms}ms")
        else:
            log.warning(f"[{source.name}] FAIL: {error} (took {latency_ms}ms)")
            maybe_alert_error(db, notifier, source.name, error, run_at)

    if not primary_any_success:
        log.warning("Both primaries failed; invoking Trendlyne fallback")
        filings, latency_ms, error = _run_source(fallback, target_date)
        _write_source_health(
            db, run_at, fallback.name, error is None, error, len(filings), latency_ms
        )
        if error is None:
            merged.extend(filings)
            log.info(f"[{fallback.name}] OK (fallback): {len(filings)} filings in {latency_ms}ms")
        else:
            log.error(f"[{fallback.name}] FAIL (fallback): {error} (took {latency_ms}ms)")
            maybe_alert_error(db, notifier, fallback.name, error, run_at)

    deduped = _dedup_within_source(merged)
    if len(deduped) != len(merged):
        log.info(f"Dedup: {len(merged)} -> {len(deduped)} after within-source key collapse")
    return deduped


def _run_source(
    source: FilingsSource, target_date: date
) -> tuple[list[Filing], int, str | None]:
    """Execute one source's fetch with latency measurement.

    Returns (filings, latency_ms, error_msg). On exception, returns
    (empty list, latency-up-to-exception, formatted error message).
    """
    t0 = time.perf_counter()
    try:
        filings = source.fetch(target_date)
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return [], latency_ms, f"{type(e).__name__}: {e}"
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return filings, latency_ms, None


def _write_source_health(
    db, run_at: datetime, source: str, ok: bool, error: str | None, records: int, latency_ms: int
) -> None:
    """Insert one source_health row. Latency is logged in error_msg even on
    success (`latency_ms=412` style) so degradation can be diagnosed before
    failures start — no schema change needed (Phase 2 Q5)."""
    if ok:
        marker = f"latency_ms={latency_ms}"
    else:
        marker = f"latency_ms={latency_ms} {error or ''}"[:500]
    try:
        db.table("source_health").insert(
            {
                "run_at": run_at.isoformat(),
                "source": source,
                "ok": ok,
                "error_msg": marker,
                "records_found": records,
            }
        ).execute()
    except Exception as e:
        log.warning(f"source_health write failed for {source}: {e}")


def _dedup_within_source(filings: list[Filing]) -> list[Filing]:
    """Drop duplicates with the same (source, symbol, quarter) key.

    Cross-source duplicates are NOT deduplicated — see module docstring.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[Filing] = []
    for f in filings:
        key = (f.source, f.symbol, f.quarter)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# Convenience re-exports so test modules can patch the detector's view of sources.
__all__ = [
    "detect_filings",
    "NseSource",
    "BseSource",
    "TrendlyneSource",
    "config",
]
