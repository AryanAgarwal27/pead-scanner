"""Phase 5 signal generator — turns the daily ranking into tiered trade signals.

Pipeline (mirrors src.pipeline.ranker's shape):
    1. Load today's rankings for run_date, joined to filings + metrics +
       fundamentals (one bulk load each, like ranker._select_cohort).
    2. Skip filings that already have a signal row (idempotent — UNIQUE(filing_id)).
    3. Fetch the Nifty regime ONCE for the run (confirmation C2).
    4. Per candidate (in rank order):
         a. assign_tier(score) — SKIP-tier dropped, logged only.
         b. fetch T+1 OHLC + corporate actions (one yfinance call).
         c. compute_levels() — entry/stop/T1 from the T+1 candle (FR-5.1).
         d. evaluate the 5 confirmations (FR-5.5) from metrics + price + regime.
         e. decide_size_r() — sizing matrix + C2/C4 hard-skip (FR-5.6).
            None → dropped, logged only.
       Survivors are SENT to Telegram and inserted (status PENDING_ENTRY).
    5. Emit a run-summary message with the 3 concentration flags (FR-5.7).

Only sent signals are persisted. SKIP-tier and sizing-skipped candidates are
logged but never written — `signals` == the trades the operator was told about.

Idempotency: the existing-signal check in step 2 is the dedup. Within a
candidate we SEND first, then INSERT, so a row is never recorded for a message
that didn't go out. A re-run picks up only filings still missing a signal row.

Failure model: per-candidate errors are caught and logged (one bad stock never
aborts the batch), matching the enricher. A total failure (e.g. rankings query
throws) bubbles up so the workflow fails loudly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src import config
from src.notify.formatters import format_signal, format_signal_summary
from src.pipeline import tiering
from src.sources import yfinance_adapter as yfa
from src.utils.logging import get_logger
from src.utils.time_utils import IST, to_ist

log = get_logger(__name__)


# Risk per trade in ₹ (R). DEFAULT_RISK_PER_TRADE_PCT of the portfolio value.
def _risk_per_trade_inr() -> float:
    return config.DEFAULT_RISK_PER_TRADE_PCT * config.PORTFOLIO_VALUE_INR


# ---------------------------------------------------------------------------
# Pure: T+1 candle → entry / stop / target levels (BRD §3.5 FR-5.1)
# ---------------------------------------------------------------------------


@dataclass
class SignalLevels:
    entry: float
    stop: float
    target1: float
    risk_reward: float


def compute_levels(high_t1: float, low_t1: float) -> SignalLevels | None:
    """Entry/stop/T1 from the T+1 daily candle.

        entry  = high of T+1 candle
        stop   = tighter of (T+1 low, entry × (1 - STOP_PCT_CAP))   [higher price]
        T1     = entry + TARGET_R_MULTIPLE × (entry - stop)
        R:R    = (T1 - entry) / (entry - stop)   (== TARGET_R_MULTIPLE by construction)

    "Tighter" stop = the one closer to entry = the higher of the two prices.
    Returns None for a degenerate candle where entry <= stop (no positive risk
    distance, e.g. a zero-range candle) — such a row can't form a valid trade.
    """
    entry = high_t1
    stop_cap = entry * (1.0 - config.STOP_PCT_CAP)
    stop = max(low_t1, stop_cap)
    risk = entry - stop
    if risk <= 0:
        return None
    target1 = entry + config.TARGET_R_MULTIPLE * risk
    risk_reward = (target1 - entry) / risk
    return SignalLevels(entry=entry, stop=stop, target1=target1, risk_reward=risk_reward)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """A signal that cleared tier + sizing and is ready to send/persist."""

    payload: dict[str, Any]      # the signals-row dict
    sector: str | None
    position_value_inr: float    # at the SUGGESTED size (for concentration)


@dataclass
class SignalSummary:
    run_date: date
    ranked_count: int = 0
    skipped_tier: int = 0
    skipped_sizing: int = 0
    skipped_data: int = 0        # no T+1 candle / level / OHLC fetch failure
    already_signalled: int = 0
    sent_count: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)
    regime_available: bool = True
    flags: list[str] = field(default_factory=list)
    runtime_seconds: float = 0.0


def run_signals(
    db, *, run_date: date | None = None, dry_run: bool = False, notifier=None
) -> SignalSummary:
    """Generate, send, and persist signals for one run_date's ranking.

    Args:
        db: supabase client.
        run_date: IST date whose ranking to read. Defaults to today (IST).
        dry_run: compute + log everything, but never send Telegram or write DB.
        notifier: a TelegramNotifier (or anything with send_markdown). Required
            unless dry_run. Constructed by the job, injected for testability.
    """
    t0 = time.perf_counter()
    if run_date is None:
        run_date = datetime.now(IST).date()
    summary = SignalSummary(run_date=run_date)
    log.info(f"signaler: run_date={run_date} dry_run={dry_run}")

    rows = _load_ranking(db, run_date)
    summary.ranked_count = len(rows)
    if not rows:
        log.info(f"signaler: no rankings for run_date={run_date}; nothing to do")
        summary.runtime_seconds = time.perf_counter() - t0
        return summary

    already = _existing_signal_filing_ids(db, [r["filing_id"] for r in rows])
    metrics_by_id = _load_metrics(db, [r["filing_id"] for r in rows])
    fundamentals_by_symbol = _load_fundamentals(db, [r["symbol_nse"] for r in rows])

    # C2 regime — one fetch for the whole run.
    regime = yfa.fetch_nifty_regime(run_date)
    nifty_is_above = regime[2] if regime else None
    summary.regime_available = regime is not None
    if regime is None:
        log.warning(
            "signaler: Nifty regime unavailable — confirmation C2 fails for ALL "
            "candidates this run, so no signals will be sent (conservative; "
            "C2 is a non-negotiable gate per FR-5.6)"
        )
    else:
        log.info(
            f"signaler: Nifty regime close={regime[0]:.1f} 50dma={regime[1]:.1f} "
            f"above={regime[2]}"
        )

    candidates: list[_Candidate] = []
    for r in rows:
        fid = int(r["filing_id"])
        if fid in already:
            summary.already_signalled += 1
            log.info(f"signaler: filing_id={fid} already has a signal — skip (idempotent)")
            continue
        try:
            cand = _prepare_candidate(
                r,
                metrics_by_id.get(fid) or {},
                fundamentals_by_symbol.get(r["symbol_nse"]) or {},
                nifty_is_above=nifty_is_above,
                summary=summary,
            )
        except Exception as e:  # noqa: BLE001 — one bad stock never aborts the batch
            log.exception(f"signaler: error preparing filing_id={fid}: {e}")
            continue
        if cand is not None:
            candidates.append(cand)

    # ---- Send + persist ----------------------------------------------------
    for cand in candidates:
        if dry_run:
            log.info(
                f"signaler: [DRY RUN] would send {cand.payload['symbol']} "
                f"tier={cand.payload['tier']} size={cand.payload['suggested_size_r']}R "
                f"entry={cand.payload['entry_price']:.2f} stop={cand.payload['stop_price']:.2f}"
            )
            summary.sent_count += 1
        else:
            _send_and_persist(db, notifier, cand.payload)
            summary.sent_count += 1
        summary.by_tier[cand.payload["tier"]] = (
            summary.by_tier.get(cand.payload["tier"], 0) + 1
        )

    # ---- Concentration summary (FR-5.7) -----------------------------------
    summary.flags = _concentration_flags(db, candidates, fundamentals_by_symbol)
    _send_summary(notifier, summary, dry_run=dry_run)

    summary.runtime_seconds = time.perf_counter() - t0
    return summary


# ---------------------------------------------------------------------------
# Candidate preparation
# ---------------------------------------------------------------------------


def _prepare_candidate(
    r: dict[str, Any],
    metrics: dict[str, Any],
    fundamentals: dict[str, Any],
    *,
    nifty_is_above: bool | None,
    summary: SignalSummary,
) -> _Candidate | None:
    """Run one ranking row through tier → levels → confirmations → sizing.

    Returns a _Candidate if it should be SENT, else None (and bumps the
    appropriate skip counter on `summary`).
    """
    fid = int(r["filing_id"])
    symbol_nse = str(r["symbol_nse"])
    score = _as_float(r["pead_score"])
    if score is None:
        summary.skipped_data += 1
        return None

    # ---- Tier (FR-5.4) ----------------------------------------------------
    tier = tiering.assign_tier(score)
    if tier not in tiering.SENDABLE_TIERS:
        summary.skipped_tier += 1
        log.info(f"signaler: filing_id={fid} {symbol_nse} score={score:.2f}σ tier=SKIP — drop")
        return None

    # ---- Price window (one yfinance call: OHLC + actions) -----------------
    filing = r.get("filings") or {}
    filing_symbol = filing.get("symbol")
    source = filing.get("source")
    filing_dt = filing.get("filing_time")
    if not (filing_symbol and source and filing_dt):
        summary.skipped_data += 1
        log.warning(f"signaler: filing_id={fid} missing filings join fields — skip")
        return None
    filing_date = to_ist(_parse_ts(filing_dt)).date()
    t_minus_1 = filing_date - timedelta(days=1)
    t_plus_1 = filing_date + timedelta(days=1)

    sw = yfa.fetch_signal_window(filing_symbol, source, filing_date)
    if sw is None or sw.empty:
        summary.skipped_data += 1
        log.info(f"signaler: filing_id={fid} {symbol_nse} no OHLC — skip")
        return None

    candle = yfa.candle_on_or_after(sw.df, t_plus_1)
    if candle is None:
        summary.skipped_data += 1
        log.info(f"signaler: filing_id={fid} {symbol_nse} no T+1 candle yet — skip")
        return None

    levels = compute_levels(candle["high"], candle["low"])
    if levels is None:
        summary.skipped_data += 1
        log.info(f"signaler: filing_id={fid} {symbol_nse} degenerate T+1 candle — skip")
        return None

    # ---- Confirmation inputs ----------------------------------------------
    vol_spike = _as_float(metrics.get("vol_spike"))
    turnover_cr = _as_float(metrics.get("avg_30d_turnover_cr"))

    close_tm1 = yfa.close_on_or_before(sw.df, t_minus_1)
    close_tp1 = candle.get("close")
    t1_move_pct = (
        abs(close_tp1 / close_tm1 - 1.0)
        if close_tm1 and close_tp1 and close_tm1 != 0
        else None
    )

    # C4 gate uses the NOMINAL full 1.0R position (most conservative liquidity
    # check — the largest position we might take), per the Phase 5 plan.
    risk_per_share = levels.entry - levels.stop
    nominal_1r_value = (_risk_per_trade_inr() / risk_per_share) * levels.entry
    liquidity_ok = (
        nominal_1r_value <= config.CONF_MAX_LIQUIDITY_PCT * (turnover_cr * 1e7)
        if turnover_cr is not None
        else None
    )

    no_corp_action = not yfa.corporate_action_within(
        sw.df, t_plus_1, config.CONF_CORPORATE_ACTION_WINDOW_DAYS
    )

    confirmations = tiering.evaluate_confirmations(
        vol_spike=vol_spike,
        nifty_is_above=nifty_is_above,
        t1_move_pct=t1_move_pct,
        liquidity_ok=liquidity_ok,
        no_corporate_action=no_corp_action,
    )
    passed = tiering.count_passed(confirmations)

    # ---- Sizing (FR-5.6) --------------------------------------------------
    size_r = tiering.decide_size_r(score, confirmations)
    if size_r is None:
        summary.skipped_sizing += 1
        log.info(
            f"signaler: filing_id={fid} {symbol_nse} score={score:.2f}σ tier={tier} "
            f"confirmations={confirmations} passed={passed} → sizing SKIP "
            f"(C2={confirmations['C2']} C4={confirmations['C4']})"
        )
        return None

    # Actual position value at the suggested size (for concentration math).
    position_value_inr = (size_r * _risk_per_trade_inr() / risk_per_share) * levels.entry

    payload = {
        "filing_id": fid,
        "symbol": symbol_nse,
        "rank": int(r["rank"]),
        "pead_score": score,
        "tier": tier,
        "confirmations": confirmations,
        "confirmations_passed": passed,
        "suggested_size_r": size_r,
        "entry_price": round(levels.entry, 2),
        "stop_price": round(levels.stop, 2),
        "target1_price": round(levels.target1, 2),
        "risk_reward": round(levels.risk_reward, 2),
        "status": "PENDING_ENTRY",
        # z components for the message (may be NULL — n_components is 3–5).
        "_z": {
            "z_sue": _as_float(r.get("z_sue")),
            "z_rev": _as_float(r.get("z_rev")),
            "z_ear": _as_float(r.get("z_ear")),
            "z_vol": _as_float(r.get("z_vol")),
            "z_margin": _as_float(r.get("z_margin")),
        },
    }
    log.info(
        f"signaler: filing_id={fid} {symbol_nse} SEND tier={tier} size={size_r}R "
        f"passed={passed}/5 entry={levels.entry:.2f} stop={levels.stop:.2f} "
        f"t1={levels.target1:.2f} rr={levels.risk_reward:.2f}"
    )
    return _Candidate(
        payload=payload,
        sector=fundamentals.get("sector"),
        position_value_inr=position_value_inr,
    )


# ---------------------------------------------------------------------------
# Send + persist
# ---------------------------------------------------------------------------


def _send_and_persist(db, notifier, payload: dict[str, Any]) -> None:
    """Send the Telegram message FIRST, then insert the row. A signal is never
    recorded for a message that failed to send (the row's absence drives the
    next run's retry)."""
    message = format_signal(payload)
    if notifier is not None:
        notifier.send_markdown(message)
    row = {k: v for k, v in payload.items() if not k.startswith("_")}
    row["signal_sent_at"] = datetime.now(UTC).isoformat()
    db.table("signals").insert(row).execute()


def _send_summary(notifier, summary: SignalSummary, *, dry_run: bool) -> None:
    msg = format_signal_summary(summary)
    if dry_run:
        log.info(f"signaler: [DRY RUN] run summary:\n{msg}")
        return
    if notifier is not None:
        notifier.send_markdown(msg)


# ---------------------------------------------------------------------------
# Concentration flags (FR-5.7) — flag, never block.
# ---------------------------------------------------------------------------


def _concentration_flags(
    db,
    candidates: list[_Candidate],
    fundamentals_by_symbol: dict[str, dict[str, Any]],
) -> list[str]:
    """Compute the 3 concentration flags over the open book.

    "Open" = existing signals with status PENDING_ENTRY/ACTIVE, PLUS this run's
    candidates. Position values for prior-open rows are recomputed from their
    stored entry/stop/suggested_size_r; this run's rows use the in-memory value.
    """
    # This run's contribution.
    by_sector: dict[str, int] = {}
    total_open = 0
    total_value = 0.0
    for c in candidates:
        total_open += 1
        total_value += c.position_value_inr
        if c.sector:
            by_sector[c.sector] = by_sector.get(c.sector, 0) + 1

    # Prior open positions (exclude any filing_ids in this run — dedup means
    # they can't overlap, but be defensive).
    this_run_fids = {c.payload["filing_id"] for c in candidates}
    for row in _load_open_signals(db):
        if row.get("filing_id") in this_run_fids:
            continue
        total_open += 1
        total_value += _recompute_value(row)
        sector = (fundamentals_by_symbol.get(row.get("symbol")) or {}).get("sector")
        if sector is None:
            sector = _lookup_sector(db, row.get("symbol"))
        if sector:
            by_sector[sector] = by_sector.get(sector, 0) + 1

    flags: list[str] = []
    if total_open > config.MAX_OPEN_POSITIONS:
        flags.append(
            f"⚠️ Open PEAD positions: {total_open} (> {config.MAX_OPEN_POSITIONS} limit)"
        )
    over_sectors = {s: n for s, n in by_sector.items() if n > config.MAX_PER_SECTOR}
    for sector, n in sorted(over_sectors.items()):
        flags.append(f"⚠️ {sector}: {n} positions (> {config.MAX_PER_SECTOR} per-sector limit)")
    allocation_pct = total_value / config.PORTFOLIO_VALUE_INR if config.PORTFOLIO_VALUE_INR else 0.0
    if allocation_pct > config.MAX_PEAD_ALLOCATION_PCT:
        flags.append(
            f"⚠️ PEAD allocation: {allocation_pct:.0%} "
            f"(> {config.MAX_PEAD_ALLOCATION_PCT:.0%} limit)"
        )
    return flags


def _recompute_value(row: dict[str, Any]) -> float:
    """Reconstruct a stored signal's position value from entry/stop/size_r."""
    entry = _as_float(row.get("entry_price"))
    stop = _as_float(row.get("stop_price"))
    size_r = _as_float(row.get("suggested_size_r"))
    if entry is None or stop is None or size_r is None or entry <= stop:
        return 0.0
    return (size_r * _risk_per_trade_inr() / (entry - stop)) * entry


# ---------------------------------------------------------------------------
# DB loads (one bulk call each — mirrors ranker._select_cohort)
# ---------------------------------------------------------------------------


def _load_ranking(db, run_date: date) -> list[dict[str, Any]]:
    """Today's ranking rows, rank-ordered, with the filings join embedded."""
    resp = (
        db.table("rankings")
        .select(
            "filing_id, symbol_nse, rank, pead_score, "
            "z_sue, z_rev, z_ear, z_vol, z_margin, "
            "filings!inner(symbol, source, filing_time, company_name)"
        )
        .eq("run_date", run_date.isoformat())
        .order("rank")
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


def _existing_signal_filing_ids(db, filing_ids: list[int]) -> set[int]:
    if not filing_ids:
        return set()
    resp = (
        db.table("signals")
        .select("filing_id")
        .in_("filing_id", filing_ids)
        .execute()
    )
    return {int(r["filing_id"]) for r in (resp.data or [])}


def _load_metrics(db, filing_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not filing_ids:
        return {}
    resp = (
        db.table("metrics")
        .select("filing_id, vol_spike, avg_30d_turnover_cr")
        .in_("filing_id", filing_ids)
        .execute()
    )
    return {int(r["filing_id"]): r for r in (resp.data or [])}


def _load_fundamentals(db, symbols: list[str]) -> dict[str, dict[str, Any]]:
    uniq = sorted({s for s in symbols if s})
    if not uniq:
        return {}
    resp = (
        db.table("fundamentals")
        .select("symbol, sector")
        .in_("symbol", uniq)
        .execute()
    )
    return {r["symbol"]: r for r in (resp.data or [])}


def _load_open_signals(db) -> list[dict[str, Any]]:
    resp = (
        db.table("signals")
        .select("filing_id, symbol, entry_price, stop_price, suggested_size_r, status")
        .in_("status", ["PENDING_ENTRY", "ACTIVE"])
        .execute()
    )
    return resp.data or []


def _lookup_sector(db, symbol: str | None) -> str | None:
    if not symbol:
        return None
    try:
        resp = (
            db.table("fundamentals")
            .select("sector")
            .eq("symbol", symbol)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    rows = resp.data or []
    return rows[0].get("sector") if rows else None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_float(v: Any) -> float | None:
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
