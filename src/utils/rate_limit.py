"""Per-source error-alert rate limiter (BRD §3.7 FR-7.2 / Phase 2 spec:
one alert per source per hour max).

Persistence-by-marker: when an alert IS sent, we insert a `source_health` row
with `error_msg = "[ALERTED] <raw error>"`. Future runs query `source_health`
for rows with that marker within the last `ERROR_ALERT_COOLDOWN_MINUTES` and
suppress if found. This avoids a schema change (consistent with how the
`error_msg='dry_run'` marker is used by jobs/poll_filings.py).
"""

from datetime import datetime, timedelta

from src import config
from src.utils.logging import get_logger

log = get_logger(__name__)

ALERTED_PREFIX = "[ALERTED] "


def maybe_alert_error(db, notifier, source: str, error: str, now_utc: datetime) -> bool:
    """Send a Telegram error alert for `source` unless one was sent within the
    cooldown window. Returns True if an alert was sent, False if suppressed
    (or if `notifier is None`, e.g. during --dry-run).
    """
    if notifier is None:
        log.info(
            f"[DRY-RUN] would alert {source}: "
            f"{(error or '').strip()[:200]}"
        )
        return False

    cooldown = timedelta(minutes=config.ERROR_ALERT_COOLDOWN_MINUTES)
    cutoff = (now_utc - cooldown).isoformat()

    try:
        recent = (
            db.table("source_health")
            .select("id")
            .eq("source", source)
            .like("error_msg", f"{ALERTED_PREFIX}%")
            .gte("run_at", cutoff)
            .limit(1)
            .execute()
        )
    except Exception as e:
        # On query failure, be conservative: ASSUME no recent alert and send.
        # Worse to suppress than over-alert.
        log.warning(f"rate_limit query failed for {source}, sending anyway: {e}")
        recent = None

    if recent is not None and recent.data:
        log.info(
            f"Suppressing {source} alert; already alerted within "
            f"{config.ERROR_ALERT_COOLDOWN_MINUTES}min"
        )
        return False

    truncated = (error or "").strip()[:500]
    msg = f"⚠️ *Source down*: `{source}`\n\n```\n{truncated}\n```"
    try:
        notifier.send_markdown(msg)
    except Exception as e:
        log.error(f"Failed to send error alert for {source}: {e}")
        return False

    try:
        db.table("source_health").insert(
            {
                "run_at": now_utc.isoformat(),
                "source": source,
                "ok": False,
                "error_msg": f"{ALERTED_PREFIX}{truncated}",
                "records_found": 0,
            }
        ).execute()
    except Exception as e:
        # Marker write is best-effort. If it fails, next run may double-alert —
        # acceptable trade-off vs swallowing the user-visible alert.
        log.warning(f"Failed to write [ALERTED] marker for {source}: {e}")
    return True
