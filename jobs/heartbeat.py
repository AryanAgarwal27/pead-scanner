"""Daily heartbeat — Phase 2.

Probes each source (NSE, BSE, Trendlyne) with a real fetch for today's IST
date, captures per-source latency + ok/fail status, writes one source_health
row per source (with `source="<NAME>-heartbeat"` so heartbeat stats stay
distinguishable from poll-time stats — Phase 2 Q5), then sends ONE
Telegram message summarizing the run. Always sends, even when fully green,
because a missing heartbeat is itself a signal that CI is broken (Q7).
"""

import time
from datetime import UTC, datetime

from src import config
from src.db.client import get_client
from src.notify.telegram import TelegramNotifier
from src.sources.base import FilingsSource
from src.sources.bse import BseSource
from src.sources.nse import NseSource
from src.sources.trendlyne import TrendlyneSource
from src.utils.logging import get_logger
from src.utils.time_utils import format_ist, today_ist

log = get_logger("heartbeat")


def main() -> int:
    db = get_client()
    notifier = TelegramNotifier()
    today = today_ist()
    run_at = datetime.now(UTC)

    sources: list[FilingsSource] = [NseSource(), BseSource(), TrendlyneSource()]
    results: list[tuple[str, bool, int, str | None, int]] = []

    for source in sources:
        t0 = time.perf_counter()
        try:
            filings = source.fetch(today)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            results.append((source.name, True, latency_ms, None, len(filings)))
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            err = f"{type(e).__name__}: {e}"[:300]
            results.append((source.name, False, latency_ms, err, 0))

    _write_health(db, run_at, results)
    body = _format_message(run_at, results)
    notifier.send_markdown(body)
    log.info("heartbeat done")
    return 0


def _write_health(
    db, run_at: datetime, results: list[tuple[str, bool, int, str | None, int]]
) -> None:
    for name, ok, latency_ms, error, count in results:
        marker = f"latency_ms={latency_ms}" if ok else f"latency_ms={latency_ms} {error}"
        try:
            db.table("source_health").insert(
                {
                    "run_at": run_at.isoformat(),
                    "source": f"{name}-heartbeat",
                    "ok": ok,
                    "error_msg": marker[:500],
                    "records_found": count,
                }
            ).execute()
        except Exception as e:
            log.warning(f"heartbeat source_health write failed for {name}: {e}")


def _format_message(
    run_at: datetime, results: list[tuple[str, bool, int, str | None, int]]
) -> str:
    lines = [
        f"🫀 *Daily Heartbeat* — {format_ist(run_at)}",
        "",
    ]
    any_down = False
    for name, ok, latency_ms, error, count in results:
        if ok:
            status = f"✅ ok ({latency_ms}ms, {count} filings)"
        else:
            any_down = True
            status = f"❌ FAIL — `{(error or '?')[:80]}`"
        lines.append(f"`{name:<10}` {status}")
    if any_down:
        lines.append("")
        lines.append(
            "_Detector will use any healthy primary; Trendlyne held as final fallback._"
        )
    return "\n".join(lines)


# Hint for ruff/mypy that config is intentionally referenced.
_ = config


if __name__ == "__main__":
    raise SystemExit(main())
