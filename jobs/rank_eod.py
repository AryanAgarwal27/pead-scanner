"""T+1 EOD ranking job — Phase 4 (BRD §3.4, §8 Phase 4).

Runs immediately after enrich_eod (20:30 IST), as a second step in the
enrich-eod workflow. Produces the daily top-N PEAD ranking:

    1. Cohort = filings in trailing 7 days with a metrics row
    2. Cross-source dedup on (NSE_ticker, quarter), NSE wins over BSE
    3. Hard filters (market cap, turnover, F&O ban, ASM/GSM, listing age,
       parser confidence, exceptional items)
    4. Composite z-score; renormalize weights across present components
    5. Top-N inserted into the rankings table

Idempotent — re-running for the same run_date deletes prior rows for that
date and re-inserts. Use --as-of YYYY-MM-DD for historical reruns.

Usage:
    python jobs/rank_eod.py
    python jobs/rank_eod.py --dry-run
    python jobs/rank_eod.py --as-of 2026-05-26

Environment:
    RANK_DEBUG=1   Print each ranked row's z components + source filing fields
                   (filing_id, parsed_at date, confidence, source, revenue, pat,
                   opm). Off by default — use for eyeball validation while the
                   daily output is still small.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from src.db.client import get_client
from src.pipeline.ranker import run_ranking
from src.utils.logging import get_logger

log = get_logger("rank_eod")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute and persist the daily top-N PEAD ranking."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the ranking but don't write to rankings/metrics/fundamentals tables.",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help=(
            "Run the ranking for a historical date (YYYY-MM-DD). Defaults to "
            "today (IST). Cohort is still the trailing 7 days FROM NOW — the "
            "as-of flag controls only the rankings.run_date written."
        ),
    )
    args = parser.parse_args()

    db = get_client()
    log.info(f"rank-eod starting (dry_run={args.dry_run}, as_of={args.as_of})")

    summary = run_ranking(db, run_date=args.as_of, dry_run=args.dry_run)

    top = f"{summary.top_score:.2f}σ" if summary.top_score is not None else "n/a"
    # End-of-run summary line — single source of "did Phase 4 run successfully"
    # for the Phase 5 trigger check.
    log.info(
        f"rank-eod done: cohort_size={summary.cohort_raw_size} "
        f"(after dedup={summary.cohort_after_dedup}, "
        f"after filters={summary.cohort_after_filters}), "
        f"ranked={summary.ranked_count}, top_score={top}, "
        f"runtime={summary.runtime_seconds:.1f}s"
    )

    # Exit 0 even when ranked=0 (empty ranking is a valid, expected outcome
    # on quiet days and during Phase 3 backfill warmup). Non-zero exit is
    # reserved for genuine job failures.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001
        log.exception(f"rank-eod failed: {e}")
        print(f"FATAL: rank-eod failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e
