"""T+0 EOD enrichment job — Phase 3 (BRD §5.2).

Runs at 20:30 IST (15:00 UTC) Mon–Fri. Processes every filing in the
14-day window that doesn't yet have a metrics row:
    1. parses headline numbers via Gemini → regex
    2. pulls yfinance OHLCV + Nifty closes
    3. joins Screener fundamentals for SUE history
    4. computes SUE / Rev_Growth / Vol_Spike / EAR / Margin_Delta
    5. writes one metrics row per filing

Idempotent — re-running picks up only filings still missing metrics.

Usage:
    python jobs/enrich_eod.py
    python jobs/enrich_eod.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from src.db.client import get_client
from src.pipeline.enricher import enrich_pending
from src.utils.logging import get_logger

log = get_logger("enrich_eod")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute PEAD metrics for pending filings.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + compute but don't write to filings/metrics tables.",
    )
    args = parser.parse_args()

    db = get_client()
    log.info(f"enrich-eod starting (dry_run={args.dry_run})")

    outcomes = enrich_pending(db, dry_run=args.dry_run)

    # Summary
    by_parser: Counter[str] = Counter()
    by_confidence: Counter[str] = Counter()
    metrics_inserted = 0
    z_tripped = 0
    errors = 0
    for o in outcomes:
        if o.parser_used:
            by_parser[o.parser_used] += 1
        if o.parser_confidence:
            by_confidence[o.parser_confidence] += 1
        if o.metrics_inserted:
            metrics_inserted += 1
        if o.z_check_tripped:
            z_tripped += 1
        if o.error:
            errors += 1

    log.info(
        f"enrich-eod done: processed={len(outcomes)} metrics_inserted={metrics_inserted} "
        f"z_check_tripped={z_tripped} errors={errors}"
    )
    log.info(f"  by parser_used:       {dict(by_parser)}")
    log.info(f"  by parser_confidence: {dict(by_confidence)}")

    # Surface any errors to stderr for CI visibility but never exit non-zero
    # (one bad filing shouldn't fail the whole job).
    if errors:
        print(f"WARN: {errors} filings hit errors — see logs above", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
