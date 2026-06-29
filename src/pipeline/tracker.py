"""Position tracker — Phase 6 (BRD §3.6, §6.4).

Closes the PEAD feedback loop: replays each sent signal against real daily price
bars and records its lifecycle in the `positions` table, updating the parent
`signals.status`.

State machine (BRD §8 Phase 6 acceptance):

    PENDING_ENTRY ─ high ≥ entry within ENTRY_WINDOW_DAYS ─▶ ACTIVE
                  ╲ window elapses, never triggered       ─▶ EXPIRED
    ACTIVE ─ low ≤ stop (pre-T1)            ─▶ CLOSED_STOP
           ─ close < 20-EMA (post-T1)       ─▶ CLOSED_TRAIL
           ─ 60 trading days held           ─▶ CLOSED_TIME_EXPIRY

Trade management mirrors BRD §3.5 FR-5.1:
  * Entry trigger is a breakout of the T+1 high (`entry_price`); it can only fire
    on a bar AFTER the T+1 reference candle (on T+1 itself high == entry_price by
    construction, and the signal wasn't actionable until that close).
  * Stop = `stop_price` (tighter of T+1 low / -5%). Pre-T1 only.
  * Target 1 = `target1_price`: BOOK 50%. The remaining 50% then trails the
    20-EMA — exit on the first daily close below the 20-EMA (TRAIL), or at
    MAX_HOLD_DAYS trading days (TIME_EXPIRY), whichever comes first.
  * Realized P&L is BLENDED for a position that reached T1:
        0.5 × (T1/entry − 1)  +  0.5 × (exit_close/entry − 1)
    A position stopped before T1 realizes the full (stop/entry − 1).

Intrabar convention (owner decision): when a single daily candle straddles both
levels (low ≤ stop AND high ≥ target on the same bar), the STOP is assumed to
fill first — the conservative outcome, so hit-rate is never overstated.

`simulate_position` is a PURE function of (levels, bars, as_of). It does no I/O
and is exhaustively unit-tested; `run_tracker` wraps it with the yfinance fetch
and Supabase reads/writes (mirroring the enricher/signaler orchestration shape).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from src import config
from src.notify.formatters import format_position_summary
from src.sources import yfinance_adapter as yfa
from src.utils.logging import get_logger
from src.utils.time_utils import IST, to_ist

log = get_logger(__name__)

# Terminal (closed/expired) statuses — a position in one of these is never
# re-simulated. PENDING_ENTRY / ACTIVE are the "open" statuses the job re-checks.
OPEN_STATUSES: tuple[str, ...] = ("PENDING_ENTRY", "ACTIVE")
CLOSED_STATUSES: tuple[str, ...] = ("CLOSED_STOP", "CLOSED_TRAIL", "CLOSED_TIME_EXPIRY")
TERMINAL_STATUSES: tuple[str, ...] = (*CLOSED_STATUSES, "EXPIRED")

# Calendar lookback before T+1 used to seed the 20-EMA (≈ 35 calendar days of
# trading bars comfortably covers a 20-day EMA) and the post-window that covers
# a full MAX_HOLD_DAYS (60 trading) hold plus holidays.
_EMA_SEED_CALENDAR_DAYS = 50
_HOLD_CALENDAR_BUFFER_DAYS = 30


@dataclass
class PositionResult:
    """Outcome of replaying one signal to `as_of`. Mirrors the positions table
    (plus `status` for the parent signals row and `t1_hit_at` for audit)."""

    status: str
    entry_filled_at: date | None = None
    t1_hit_at: date | None = None
    exit_at: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None          # 'STOP' | 'TRAIL' | 'TIME_EXPIRY'
    pnl_pct: float | None = None            # realized if closed, else unrealized; blended post-T1
    max_favorable: float | None = None      # max (high/entry − 1) over the hold
    max_adverse: float | None = None        # min (low/entry − 1) over the hold
    days_held: int | None = None            # trading days from entry (0 on entry day)

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES


def _bar_date(ts: Any) -> date:
    return ts.date() if hasattr(ts, "date") else ts


def _blended_pnl(entry: float, t1: float, exit_close: float) -> float:
    """50% booked at T1, 50% exited at exit_close."""
    return 0.5 * (t1 / entry - 1.0) + 0.5 * (exit_close / entry - 1.0)


def simulate_position(
    *,
    entry_price: float,
    stop_price: float,
    target1_price: float,
    signal_ref_date: date,
    bars: pd.DataFrame,
    as_of: date,
    entry_window_days: int | None = None,
    max_hold_days: int | None = None,
    ema_span: int | None = None,
) -> PositionResult:
    """Replay one signal against daily OHLC bars up to `as_of`.

    Args:
        entry_price/stop_price/target1_price: the signal's levels.
        signal_ref_date: the T+1 reference candle's date. Entry is scanned on
            bars STRICTLY AFTER this date (a breakout of the T+1 high).
        bars: OHLC indexed by trading date (ascending), columns High/Low/Close.
            Should include ~ema_span bars of lookback before signal_ref_date so
            the 20-EMA is seeded.
        as_of: simulate only through this date (bars after it are ignored).
        entry_window_days/max_hold_days/ema_span: default to config.

    Returns a PositionResult. Never raises on ordinary data; an empty/short
    series yields PENDING_ENTRY (nothing to act on yet).
    """
    entry_window_days = entry_window_days or config.ENTRY_WINDOW_DAYS
    max_hold_days = max_hold_days or config.MAX_HOLD_DAYS
    ema_span = ema_span or config.TRAILING_EMA

    if bars is None or bars.empty:
        return PositionResult(status="PENDING_ENTRY")

    df = bars.sort_index()
    df = df[df.index <= pd.Timestamp(as_of)]
    if df.empty:
        return PositionResult(status="PENDING_ENTRY")

    ema = df["Close"].ewm(span=ema_span, adjust=False).mean()
    post = df[df.index > pd.Timestamp(signal_ref_date)]
    if post.empty:
        return PositionResult(status="PENDING_ENTRY")

    # ---- Entry scan: first of the next ENTRY_WINDOW_DAYS bars to print a high
    # at or above the entry trigger (breakout of the T+1 high). ----------------
    entry_pos: int | None = None
    entry_filled_at: date | None = None
    for i in range(min(entry_window_days, len(post))):
        if float(post.iloc[i]["High"]) >= entry_price:
            entry_pos = i
            entry_filled_at = _bar_date(post.index[i])
            break

    if entry_pos is None:
        # Not triggered. EXPIRED only once the whole window has elapsed; until
        # then it is genuinely still PENDING (the window hasn't closed yet).
        if len(post) >= entry_window_days:
            return PositionResult(
                status="EXPIRED", exit_at=_bar_date(post.index[entry_window_days - 1])
            )
        return PositionResult(status="PENDING_ENTRY")

    # ---- Manage the position from the entry bar onward -----------------------
    max_fav = -math.inf
    max_adv = math.inf
    t1_hit_at: date | None = None
    phase = "PRE_T1"

    for j in range(entry_pos, len(post)):
        idx = post.index[j]
        bdate = _bar_date(idx)
        hi = float(post.iloc[j]["High"])
        lo = float(post.iloc[j]["Low"])
        cl = float(post.iloc[j]["Close"])
        max_fav = max(max_fav, hi / entry_price - 1.0)
        max_adv = min(max_adv, lo / entry_price - 1.0)
        days_held = j - entry_pos

        if phase == "PRE_T1":
            # Conservative: a straddling bar resolves to the STOP first.
            if lo <= stop_price:
                return PositionResult(
                    status="CLOSED_STOP",
                    entry_filled_at=entry_filled_at,
                    exit_at=bdate,
                    exit_price=round(stop_price, 2),
                    exit_reason="STOP",
                    pnl_pct=stop_price / entry_price - 1.0,
                    max_favorable=max_fav,
                    max_adverse=max_adv,
                    days_held=days_held,
                )
            if hi >= target1_price:
                t1_hit_at = bdate
                phase = "TRAIL"   # book 50% here; trail the remainder from next bar

        if phase == "TRAIL":
            if days_held >= max_hold_days:
                return PositionResult(
                    status="CLOSED_TIME_EXPIRY",
                    entry_filled_at=entry_filled_at,
                    t1_hit_at=t1_hit_at,
                    exit_at=bdate,
                    exit_price=round(cl, 2),
                    exit_reason="TIME_EXPIRY",
                    pnl_pct=_blended_pnl(entry_price, target1_price, cl),
                    max_favorable=max_fav,
                    max_adverse=max_adv,
                    days_held=days_held,
                )
            # The 20-EMA trailing stop starts the bar AFTER T1 is booked.
            if bdate != t1_hit_at and cl < float(ema.loc[idx]):
                return PositionResult(
                    status="CLOSED_TRAIL",
                    entry_filled_at=entry_filled_at,
                    t1_hit_at=t1_hit_at,
                    exit_at=bdate,
                    exit_price=round(cl, 2),
                    exit_reason="TRAIL",
                    pnl_pct=_blended_pnl(entry_price, target1_price, cl),
                    max_favorable=max_fav,
                    max_adverse=max_adv,
                    days_held=days_held,
                )

    # ---- Still open at as_of → ACTIVE, with unrealized P&L -------------------
    last_close = float(post.iloc[-1]["Close"])
    pnl = (
        _blended_pnl(entry_price, target1_price, last_close)
        if t1_hit_at is not None
        else last_close / entry_price - 1.0
    )
    return PositionResult(
        status="ACTIVE",
        entry_filled_at=entry_filled_at,
        t1_hit_at=t1_hit_at,
        pnl_pct=pnl,
        max_favorable=max_fav,
        max_adverse=max_adv,
        days_held=(len(post) - 1) - entry_pos,
    )


# ---------------------------------------------------------------------------
# Orchestration — load open signals, simulate, persist, summarize.
# ---------------------------------------------------------------------------


@dataclass
class TrackSummary:
    run_date: date
    open_count: int = 0
    updated: int = 0
    newly_closed: int = 0
    newly_expired: int = 0
    today_pnl_pct: float | None = None       # mean realized pnl of positions closed today
    mtd_pnl_pct: float | None = None         # mean realized pnl of positions closed this month
    hit_rate: float | None = None            # fraction profitable over last 50 closed
    hit_rate_n: int = 0
    errors: int = 0
    runtime_seconds: float = 0.0


def run_tracker(
    db, *, run_date: date | None = None, dry_run: bool = False, notifier=None
) -> TrackSummary:
    """Update every open signal's position to `run_date` and send the daily
    summary (BRD §3.6 FR-6.2/6.3/6.4). Idempotent: each signal is re-simulated
    from full price history, so a re-run reproduces the same state."""
    t0 = time.perf_counter()
    if run_date is None:
        run_date = datetime.now(IST).date()
    summary = TrackSummary(run_date=run_date)
    log.info(f"tracker: run_date={run_date} dry_run={dry_run}")

    open_signals = _load_open_signals(db)
    summary.open_count = len(open_signals)

    for sig in open_signals:
        try:
            result = _track_one(sig, run_date)
        except Exception as e:  # noqa: BLE001 — one bad signal never aborts the batch
            log.exception(f"tracker: error on signal_id={sig.get('id')}: {e}")
            summary.errors += 1
            continue
        if result is None:
            continue
        summary.updated += 1
        if result.status in CLOSED_STATUSES:
            summary.newly_closed += 1
        elif result.status == "EXPIRED":
            summary.newly_expired += 1
        if not dry_run:
            _persist(db, int(sig["id"]), result)

    _fill_summary_stats(db, summary, run_date, dry_run=dry_run)

    msg = format_position_summary(summary)
    if dry_run:
        log.info(f"tracker: [DRY RUN] summary:\n{msg}")
    elif notifier is not None:
        notifier.send_markdown(msg)

    summary.runtime_seconds = time.perf_counter() - t0
    log.info(
        f"tracker: done open={summary.open_count} updated={summary.updated} "
        f"closed={summary.newly_closed} expired={summary.newly_expired} "
        f"errors={summary.errors} hit_rate={summary.hit_rate} "
        f"runtime={summary.runtime_seconds:.1f}s"
    )
    return summary


def _track_one(sig: dict[str, Any], run_date: date) -> PositionResult | None:
    """Fetch prices for one signal and simulate to run_date."""
    filing = sig.get("filings") or {}
    filing_symbol = filing.get("symbol")
    source = filing.get("source")
    filing_dt = filing.get("filing_time")
    if not (filing_symbol and source and filing_dt):
        log.warning(f"tracker: signal_id={sig.get('id')} missing filings join — skip")
        return None

    filing_date = to_ist(_parse_ts(filing_dt)).date()
    start = filing_date - timedelta(days=_EMA_SEED_CALENDAR_DAYS)
    end = run_date + timedelta(days=1)
    window = yfa.fetch_ohlc_range(filing_symbol, source, start, end)
    if window is None or window.empty:
        log.info(f"tracker: signal_id={sig['id']} {filing_symbol} no OHLC — leave as-is")
        return None

    df = window.df
    # T+1 reference candle = first trading bar on/after filing_date+1.
    ref_candle = yfa.candle_on_or_after(df, filing_date + timedelta(days=1))
    if ref_candle is None:
        log.info(f"tracker: signal_id={sig['id']} {filing_symbol} no T+1 candle yet — skip")
        return None
    ref_ts = df[df.index >= pd.Timestamp(filing_date + timedelta(days=1))].index[0]

    result = simulate_position(
        entry_price=float(sig["entry_price"]),
        stop_price=float(sig["stop_price"]),
        target1_price=float(sig["target1_price"]),
        signal_ref_date=_bar_date(ref_ts),
        bars=df,
        as_of=run_date,
    )
    log.info(
        f"tracker: signal_id={sig['id']} {sig.get('symbol')} {sig['status']} -> "
        f"{result.status} entry={result.entry_filled_at} exit={result.exit_at} "
        f"reason={result.exit_reason} pnl={result.pnl_pct}"
    )
    return result


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------


def _load_open_signals(db) -> list[dict[str, Any]]:
    resp = (
        db.table("signals")
        .select(
            "id, filing_id, symbol, status, entry_price, stop_price, target1_price, "
            "signal_sent_at, filings!inner(symbol, source, filing_time)"
        )
        .in_("status", list(OPEN_STATUSES))
        .order("id")
        .execute()
    )
    out: list[dict[str, Any]] = []
    for r in resp.data or []:
        filing = r.get("filings")
        if isinstance(filing, list):
            filing = filing[0] if filing else None
        r["filings"] = filing
        out.append(r)
    return out


def _persist(db, signal_id: int, result: PositionResult) -> None:
    """Upsert the positions row and update the parent signals.status."""
    row = {
        "signal_id": signal_id,
        "entry_filled_at": result.entry_filled_at.isoformat() if result.entry_filled_at else None,
        "t1_hit_at": result.t1_hit_at.isoformat() if result.t1_hit_at else None,
        "exit_at": result.exit_at.isoformat() if result.exit_at else None,
        "exit_price": result.exit_price,
        "exit_reason": result.exit_reason,
        "pnl_pct": result.pnl_pct,
        "max_favorable": result.max_favorable,
        "max_adverse": result.max_adverse,
        "days_held": result.days_held,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    db.table("positions").upsert(row, on_conflict="signal_id").execute()
    db.table("signals").update({"status": result.status}).eq("id", signal_id).execute()


def _fill_summary_stats(db, summary: TrackSummary, run_date: date, *, dry_run: bool) -> None:
    """Open count after update + today/MTD P&L + hit rate over last 50 closed.

    Reads positions joined to signals.status so closed-today / closed-this-month
    cohorts are derived from the persisted state (post-update, unless dry-run).
    """
    # Re-read open count from signals (authoritative post-update).
    if not dry_run:
        oc = (
            db.table("signals")
            .select("id", count="exact")
            .in_("status", list(OPEN_STATUSES))
            .limit(1)
            .execute()
        )
        summary.open_count = oc.count or 0

    closed = (
        db.table("positions")
        .select("pnl_pct, exit_at, exit_reason")
        .not_.is_("exit_at", "null")
        .order("exit_at", desc=True)
        .limit(200)
        .execute()
    ).data or []

    month_start = run_date.replace(day=1).isoformat()
    today_iso = run_date.isoformat()
    today_vals = [_f(r["pnl_pct"]) for r in closed if r.get("exit_at") == today_iso]
    mtd_vals = [_f(r["pnl_pct"]) for r in closed if r.get("exit_at", "") >= month_start]
    today_vals = [v for v in today_vals if v is not None]
    mtd_vals = [v for v in mtd_vals if v is not None]
    summary.today_pnl_pct = sum(today_vals) / len(today_vals) if today_vals else None
    summary.mtd_pnl_pct = sum(mtd_vals) / len(mtd_vals) if mtd_vals else None

    last50 = [_f(r["pnl_pct"]) for r in closed[:50]]
    last50 = [v for v in last50 if v is not None]
    summary.hit_rate_n = len(last50)
    summary.hit_rate = (
        sum(1 for v in last50 if v > 0) / len(last50) if last50 else None
    )


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


_ = IST  # re-exported for callers/tests
