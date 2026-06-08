"""Signal-generation job — Phase 5 (BRD §3.5, §8 Phase 5).

Runs at 20:45 IST (15:15 UTC) Mon–Fri, after enrich-eod (20:30) has written
today's metrics + rankings. Reads the day's top-N ranking, computes tiered
trade signals with the 5-point confirmation checklist and position sizing, and
sends them to Telegram (status PENDING_ENTRY), followed by a run summary with
concentration flags.

Idempotent — UNIQUE(filing_id) on signals + a pre-send existing-signal check
means a re-run never double-sends. Use --as-of YYYY-MM-DD to (re)generate for a
historical ranking; note the Nifty regime (C2) is taken as-of that date for the
whole cohort, which is approximate for a multi-date replay.

Usage:
    python jobs/generate_signals.py
    python jobs/generate_signals.py --dry-run
    python jobs/generate_signals.py --as-of 2026-05-26
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from src.db.client import get_client
from src.notify.telegram import TelegramNotifier
from src.pipeline.signaler import run_signals
from src.utils.logging import get_logger

log = get_logger("generate_signals")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate, send, and persist tiered PEAD trade signals."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute + log signals but send no Telegram messages and write nothing.",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help=(
            "Generate signals for a historical ranking date (YYYY-MM-DD). "
            "Defaults to today (IST). The Nifty regime confirmation (C2) is "
            "evaluated as-of this date for the whole cohort (approximate replay)."
        ),
    )
    args = parser.parse_args()

    db = get_client()
    # In dry-run, never construct the notifier (no creds needed for a dry run).
    notifier = None if args.dry_run else TelegramNotifier()
    log.info(f"generate-signals starting (dry_run={args.dry_run}, as_of={args.as_of})")

    summary = run_signals(
        db, run_date=args.as_of, dry_run=args.dry_run, notifier=notifier
    )

    by_tier = "  ".join(f"{t}={n}" for t, n in sorted(summary.by_tier.items())) or "none"
    log.info(
        f"generate-signals done: ranked={summary.ranked_count} sent={summary.sent_count} "
        f"({by_tier}); skipped tier={summary.skipped_tier} sizing={summary.skipped_sizing} "
        f"data={summary.skipped_data} already={summary.already_signalled}; "
        f"regime_available={summary.regime_available} flags={len(summary.flags)} "
        f"runtime={summary.runtime_seconds:.1f}s"
    )
    # Exit 0 even when sent=0 — an empty signal set is a valid, expected outcome
    # on quiet days, in a down regime (C2 fails all), or during backfill warmup.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001
        log.exception(f"generate-signals failed: {e}")
        print(f"FATAL: generate-signals failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e
