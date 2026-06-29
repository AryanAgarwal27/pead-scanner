"""Position-tracker job — Phase 6 (BRD §3.6, §8 Phase 6).

Runs daily at 16:00 IST (10:30 UTC), after the cash session closes, and updates
every open signal's position by replaying it against real daily price bars:
    1. PENDING_ENTRY → ACTIVE when the T+1-high breakout triggers (else EXPIRED
       after ENTRY_WINDOW_DAYS).
    2. ACTIVE → CLOSED_STOP / CLOSED_TRAIL / CLOSED_TIME_EXPIRY per FR-5.1
       (book 50% at T1, trail the rest on the 20-EMA, max 60 trading days).
    3. Upserts the positions row + updates signals.status.
    4. Sends a daily summary (open count, today's & MTD P&L, hit rate over the
       last 50 closed) to Telegram (FR-6.4) at ~4:30 PM IST.

Idempotent — each signal is re-simulated from full price history, so a re-run
reproduces the same state (positions upsert on signal_id).

Usage:
    python jobs/track_positions.py
    python jobs/track_positions.py --dry-run
    python jobs/track_positions.py --as-of 2026-06-15
"""

from __future__ import annotations

import sys
from datetime import date

from src.db.client import get_client
from src.notify.telegram import TelegramNotifier
from src.pipeline.tracker import run_tracker
from src.utils.logging import get_logger

log = get_logger("track_positions")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Update PEAD positions and send the daily summary."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate + log, but write nothing and send no Telegram message.",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="Track as of a historical date (YYYY-MM-DD). Defaults to today (IST).",
    )
    args = parser.parse_args()

    db = get_client()
    notifier = None if args.dry_run else TelegramNotifier()
    log.info(f"track-positions starting (dry_run={args.dry_run}, as_of={args.as_of})")

    summary = run_tracker(db, run_date=args.as_of, dry_run=args.dry_run, notifier=notifier)

    log.info(
        f"track-positions done: open={summary.open_count} updated={summary.updated} "
        f"closed={summary.newly_closed} expired={summary.newly_expired} "
        f"errors={summary.errors} hit_rate={summary.hit_rate} "
        f"runtime={summary.runtime_seconds:.1f}s"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001
        log.exception(f"track-positions failed: {e}")
        print(f"FATAL: track-positions failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e
