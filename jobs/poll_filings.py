"""Phase 2 orchestrator: poll NSE + BSE (and Trendlyne fallback) for today's
quarterly result filings, upsert, alert via Telegram, log source health.

Idempotent — re-runs do not duplicate alerts. A row whose insert succeeded but
whose Telegram send failed on a previous run will be re-alerted on the next
run, because the retry path keys off `alerted_at IS NULL`.

Per-source source_health rows are written by the detector (`src.pipeline.detector`);
this file writes one ORCHESTRATOR-level summary row tagged source="POLL" so
poll-time aggregates (records_found = total post-detector) stay separable from
per-source rows.

Usage:
    python jobs/poll_filings.py
    python jobs/poll_filings.py --date 2026-05-23
    python jobs/poll_filings.py --date 2026-05-14 --dry-run
"""

import argparse
from datetime import UTC, date, datetime

from src import config
from src.db.client import get_client
from src.notify.formatters import format_batched, format_single_filing
from src.notify.telegram import TelegramNotifier
from src.pipeline.detector import detect_filings
from src.sources.base import Filing
from src.utils.logging import get_logger
from src.utils.time_utils import today_ist

log = get_logger("poll_filings")


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll BSE quarterly result filings.")
    parser.add_argument(
        "--date",
        type=_parse_iso_date,
        default=None,
        help="IST date (YYYY-MM-DD) to poll. Default: today (IST).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the full pipeline (fetch, dedup, format) WITHOUT writing to the "
            "filings table or sending Telegram messages. A source_health row is "
            "still written with error_msg='dry_run' so the run is identifiable. "
            "Prints a structured summary on stdout."
        ),
    )
    args = parser.parse_args()
    target_date = args.date or today_ist()
    dry_run = args.dry_run
    suffix = " [DRY RUN]" if dry_run else ""
    log.info(f"Polling all sources for IST date={target_date.isoformat()}{suffix}")

    db = get_client()
    # In dry-run, pass notifier=None so rate_limit logs but doesn't send.
    notifier_for_detector = None if dry_run else TelegramNotifier()
    run_at = datetime.now(UTC)
    try:
        # Detector polls NSE + BSE in parallel (both primary), falls back to
        # Trendlyne if BOTH error. Per-source source_health rows are written
        # by the detector itself.
        filings = detect_filings(db, notifier_for_detector, target_date)
    except Exception as e:
        log.exception("Detector failed")
        _log_source_health(db, run_at, ok=False, error=str(e), records=0)
        raise

    to_insert, to_alert = _classify_filings(db, filings)
    log.info(f"{len(to_alert)} filings need alerts (out of {len(filings)} fetched)")

    if dry_run:
        _print_dry_run_summary(target_date, filings, to_alert)
        _log_source_health(db, run_at, ok=True, error="dry_run", records=len(filings))
        log.info("done [DRY RUN]")
        return 0

    if to_insert:
        _perform_upsert(db, to_insert)
    if to_alert:
        # Reuse the detector's notifier — already constructed above.
        _send_alerts(notifier_for_detector, to_alert)
        _mark_alerted(db, to_alert)

    _log_source_health(db, run_at, ok=True, error=None, records=len(filings))
    log.info("done")
    return 0


def _parse_iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid date {s!r}, expected YYYY-MM-DD") from e


def _classify_filings(
    db, filings: list[Filing]
) -> tuple[list[Filing], list[Filing]]:
    """Read-only classification of fetched filings against existing DB state.

    Returns (to_insert, to_alert). A filing needs alerting when either it's new
    (to_insert) OR it was previously inserted but alerted_at IS NULL (retry path
    after a failed send).
    """
    if not filings:
        return [], []

    symbols = list({f.symbol for f in filings})
    quarters = list({f.quarter for f in filings})
    existing_resp = (
        db.table("filings")
        .select("symbol, quarter, alerted_at")
        .in_("symbol", symbols)
        .in_("quarter", quarters)
        .execute()
    )
    existing_alerted: dict[tuple[str, str], bool] = {
        (r["symbol"], r["quarter"]): r.get("alerted_at") is not None
        for r in (existing_resp.data or [])
    }

    to_insert: list[Filing] = []
    to_alert: list[Filing] = []
    for f in filings:
        key = (f.symbol, f.quarter)
        if key not in existing_alerted:
            to_insert.append(f)
            to_alert.append(f)
        elif not existing_alerted[key]:
            # Previously inserted but alert never went out — retry just the alert.
            to_alert.append(f)
    return to_insert, to_alert


def _perform_upsert(db, to_insert: list[Filing]) -> None:
    payload = [
        {
            "symbol": f.symbol,
            "company_name": f.company_name,
            "quarter": f.quarter,
            "filing_time": f.filing_time.isoformat(),
            "source": f.source,  # Phase 2: detector provides actual source per filing
            "filing_url": f.filing_url,
            "is_consolidated": f.is_consolidated,
            "raw_payload": f.raw_payload,
        }
        for f in to_insert
    ]
    # upsert (not insert) protects against a concurrent run inserting the
    # same key between our SELECT and INSERT.
    db.table("filings").upsert(payload, on_conflict="symbol,quarter").execute()


def _send_alerts(notifier: TelegramNotifier, to_alert: list[Filing]) -> None:
    if len(to_alert) > config.POLL_BATCH_THRESHOLD:
        for body in format_batched(to_alert):
            notifier.send_markdown(body)
    else:
        for f in to_alert:
            notifier.send_markdown(format_single_filing(f))


def _mark_alerted(db, to_alert: list[Filing]) -> None:
    now = datetime.now(UTC).isoformat()
    for f in to_alert:
        db.table("filings").update({"alerted_at": now}).eq("symbol", f.symbol).eq(
            "quarter", f.quarter
        ).execute()


def _log_source_health(
    db, run_at: datetime, ok: bool, error: str | None, records: int
) -> None:
    """Orchestrator-level summary row. Per-source rows are written by the detector."""
    try:
        db.table("source_health").insert(
            {
                "run_at": run_at.isoformat(),
                "source": "POLL",
                "ok": ok,
                "error_msg": error,
                "records_found": records,
            }
        ).execute()
    except Exception as e:
        log.warning(f"source_health write failed: {e}")


def _print_dry_run_summary(
    target_date: date, filings: list[Filing], to_alert: list[Filing]
) -> None:
    print()
    print("DRY RUN SUMMARY")
    print(f"  Date: {target_date.isoformat()}")
    # Phase 2: filings is the post-detector merged set across NSE+BSE (+Trendlyne if both errored).
    print(f"  Filings (all sources, post-detector): {len(filings)}")
    by_source: dict[str, int] = {}
    for f in filings:
        by_source[f.source] = by_source.get(f.source, 0) + 1
    for src, cnt in sorted(by_source.items()):
        print(f"    {src:<11} {cnt}")
    print(f"  Would alert (after dedup): {len(to_alert)}")
    if to_alert:
        n = min(3, len(to_alert))
        print(f"  Sample messages (first {n}):")
        for i, f in enumerate(to_alert[:n], 1):
            print(f"    --- Message {i} ---")
            for line in format_single_filing(f).splitlines():
                print(f"    {line}")
    else:
        print("  Sample messages: (none — nothing would be alerted)")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
