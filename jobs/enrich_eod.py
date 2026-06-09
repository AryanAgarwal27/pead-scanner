"""T+0 EOD enrichment job — Phase 3 (BRD §5.2).

Runs at 20:30 IST (15:00 UTC) Mon–Fri. Processes every filing in the
14-day window that doesn't yet have a metrics row:
    1. parses headline numbers via Gemini → regex
    2. pulls yfinance OHLCV + Nifty closes
    3. joins Screener fundamentals for SUE history
    4. computes SUE / Rev_Growth / Vol_Spike / EAR / Margin_Delta
    5. writes one metrics row per filing

Idempotent — re-running picks up only filings still missing metrics.

Re-enrichment (Phase 3 rate-limit fix): when a bulk backfill exhausted the
Gemini free-tier RPD and demoted filings to regex (confidence low/failed,
excluded by Phase 4), use --reparse to re-attempt them with Gemini. Runs are
throttled and bounded by a daily call budget, so a large backlog is an
intentional multi-day grind (see README "Re-enriching rate-limited filings").

Usage:
    python jobs/enrich_eod.py
    python jobs/enrich_eod.py --dry-run
    python jobs/enrich_eod.py --reparse --window-days 14          # re-attempt regex/low/failed
    python jobs/enrich_eod.py --reparse --limit 900               # one day's throttled slice
    python jobs/enrich_eod.py --filing-id 87 --filing-id 213      # targeted re-enrich
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
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Process only the first N candidates (oldest-first). "
            "Use for incremental backfill validation before committing to a full run. "
            "Default: process all (still bounded by --max-gemini-calls)."
        ),
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help=(
            "Re-attempt filings whose parse is low-quality (parser_used='regex' "
            "OR parser_confidence in low/failed) within the window. Each is "
            "invalidated (parsed_at NULL'd, metrics row deleted) just before "
            "re-parsing — a budget trip mid-run never orphans a row."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help=(
            f"Override the selection window (default {None} -> "
            "enricher's 14-day window). Use to reach aged-out filings."
        ),
    )
    parser.add_argument(
        "--filing-id",
        type=int,
        action="append",
        dest="filing_ids",
        default=None,
        help=(
            "Re-enrich exactly this filing id, bypassing the window AND the "
            "metrics gate. Repeatable: --filing-id 87 --filing-id 213."
        ),
    )
    parser.add_argument(
        "--max-gemini-calls",
        type=int,
        default=None,
        help=(
            "Daily Gemini CALL budget (counts calls across both tiers + retries, "
            "not filings). Dispatch stops once reached; remaining candidates "
            "resume next run. Default: config.GEMINI_DAILY_CALL_BUDGET (900)."
        ),
    )
    args = parser.parse_args()

    db = get_client()
    log.info(
        f"enrich-eod starting (dry_run={args.dry_run}, limit={args.limit}, "
        f"reparse={args.reparse}, window_days={args.window_days}, "
        f"filing_ids={args.filing_ids}, max_gemini_calls={args.max_gemini_calls})"
    )

    outcomes = enrich_pending(
        db,
        dry_run=args.dry_run,
        limit=args.limit,
        reparse=args.reparse,
        window_days=args.window_days,
        filing_ids=args.filing_ids,
        max_gemini_calls=args.max_gemini_calls,
    )

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
