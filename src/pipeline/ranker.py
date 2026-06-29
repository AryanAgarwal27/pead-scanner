"""Phase 4 ranker — produces the daily top-N PEAD ranking.

Pipeline:
    1. SELECT the cohort: filings in the trailing COHORT_WINDOW_DAYS that
       have a metrics row.
    2. Cross-source dedup on (NSE_ticker, quarter) — keep NSE over BSE,
       BSE over Trendlyne (BRD §3.1 SOURCES_ORDER).
    3. Run hard filters (src.pipeline.filterer).
    4. Score the survivors (src.pipeline.scorer).
    5. Sort by pead_score descending; keep top TOP_N.
    6. Upsert into the rankings table for run_date. Re-running the same
       run_date overwrites that day's rows (delete-then-insert).

Idempotency: a single run_date can be re-computed any number of times; the
output is deterministic given the same DB state.

Failure model: any unhandled exception bubbles up so the workflow fails.
Per Phase 4 plan, the ranker is the final step of enrich-eod.yml — a
failure there fails the whole job and the operator gets a CI notification.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src import config
from src.pipeline import filterer as F
from src.pipeline import scorer as S
from src.sources.symbol_map import to_nse_ticker
from src.utils.logging import get_logger
from src.utils.time_utils import IST

log = get_logger(__name__)


# Source-priority for cross-source dedup. Index = priority (lower wins).
# Matches src.config.SOURCES_ORDER conceptually but we encode it as a dict
# here so dedup doesn't depend on list-index lookups.
_SOURCE_PRIORITY: dict[str, int] = {"NSE": 0, "BSE": 1, "TRENDLYNE": 2}

# Safety cap on cohort pagination (1000 rows/page). 20 pages = 20k filings in a
# single 7-day window — far beyond any real result week — so hitting it means a
# bad window, not a legitimately huge cohort.
_COHORT_MAX_PAGES = 20


@dataclass
class RankSummary:
    """Final summary emitted by run_ranking. Used by jobs/rank_eod.py for
    the BRD-mandated end-of-run log line."""

    run_date: date
    cohort_raw_size: int                # filings in the 7-day window with metrics
    cohort_after_dedup: int             # after cross-source dedup
    cohort_after_filters: int           # after hard filters
    ranked_count: int                   # rows written to rankings table
    top_score: float | None             # highest pead_score (None if empty ranking)
    runtime_seconds: float


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_ranking(
    db, *, run_date: date | None = None, dry_run: bool = False
) -> RankSummary:
    """Compute and persist the top-N ranking for a single run_date.

    Args:
        db: supabase client.
        run_date: IST date the ranking is FOR. Defaults to today (IST).
        dry_run: if True, compute everything but never write to DB.
    """
    t0 = time.perf_counter()
    if run_date is None:
        run_date = datetime.now(IST).date()

    log.info(f"ranker: run_date={run_date} dry_run={dry_run}")

    # ---- 1. Load cohort ---------------------------------------------------
    cohort = _select_cohort(db, run_date)
    log.info(f"ranker: cohort raw size = {len(cohort)} (last {config.COHORT_WINDOW_DAYS} days)")

    # ---- 2. Cross-source dedup --------------------------------------------
    deduped = _dedup_cross_source(cohort)
    log.info(f"ranker: cohort after cross-source dedup = {len(deduped)}")

    # ---- 3. Hard filters --------------------------------------------------
    filter_outcomes = F.filter_cohort(db, deduped, dry_run=dry_run)
    survivors_by_id = {o.filing_id: o for o in filter_outcomes if o.passed}
    surviving_rows = [r for r in deduped if int(r["filing_id"]) in survivors_by_id]
    log.info(f"ranker: cohort after hard filters = {len(surviving_rows)}")
    _log_drop_breakdown(filter_outcomes)

    if len(surviving_rows) < config.RANK_MIN_COHORT_SIZE:
        log.info(
            f"ranker: cohort < RANK_MIN_COHORT_SIZE ({config.RANK_MIN_COHORT_SIZE}); "
            "skipping ranking write (graceful degradation, BRD §8 Phase 4 acceptance)"
        )
        return RankSummary(
            run_date=run_date,
            cohort_raw_size=len(cohort),
            cohort_after_dedup=len(deduped),
            cohort_after_filters=len(surviving_rows),
            ranked_count=0,
            top_score=None,
            runtime_seconds=time.perf_counter() - t0,
        )

    # ---- 4. Score ---------------------------------------------------------
    scoring_input = _shape_for_scorer(surviving_rows, survivors_by_id)
    scored = S.score_cohort(scoring_input)
    rankable = [s for s in scored if s.pead_score is not None]
    log.info(
        f"ranker: scored {len(scored)} rows; "
        f"{len(rankable)} have ≥{config.RANK_MIN_COMPONENTS} components and a composite score"
    )

    # ---- 5. Rank ----------------------------------------------------------
    rankable.sort(key=lambda s: s.pead_score, reverse=True)
    top = rankable[: config.TOP_N]

    # ---- 5a. Optional per-row debug dump (RANK_DEBUG=1) -------------------
    # Eyeball-validation aid for small daily outputs (6-8 rows during the
    # warmup window). Off by default so the prod log stays clean.
    if os.environ.get("RANK_DEBUG") == "1":
        _emit_debug_rows(top, surviving_rows)

    # ---- 6. Persist -------------------------------------------------------
    if not dry_run:
        _persist_ranking(db, run_date, top, cohort_size=len(surviving_rows))
    else:
        log.info(f"ranker: dry-run — would write {len(top)} ranking rows for {run_date}")

    runtime = time.perf_counter() - t0
    return RankSummary(
        run_date=run_date,
        cohort_raw_size=len(cohort),
        cohort_after_dedup=len(deduped),
        cohort_after_filters=len(surviving_rows),
        ranked_count=len(top),
        top_score=(top[0].pead_score if top else None),
        runtime_seconds=runtime,
    )


# ---------------------------------------------------------------------------
# Cohort selection
# ---------------------------------------------------------------------------


def _select_cohort(db, run_date: date) -> list[dict[str, Any]]:
    """Filings in the trailing 7 days that already have a metrics row.

    The 7-day cohort window is inclusive of `run_date` and counts back
    COHORT_WINDOW_DAYS in calendar days. Trailing-cohort definition matches
    BRD §3.4 FR-4.2: "results filed in the trailing 7 days".

    Joined includes:
      * filings columns needed by filter + scorer + dedup
      * metrics columns (all 5 + cached turnover) via PostgREST foreign-table
      * fundamentals columns via NSE-ticker resolution. Because fundamentals
        is keyed on NSE ticker (not filings.symbol), we can't do this as a
        single PostgREST embed for BSE filings. Strategy: select filings +
        metrics in one call, then bulk-load fundamentals for the resolved
        NSE tickers in a second call. Saves N+1 round-trips.

    Window: anchored on `run_date`, NOT wall-clock now(). The cohort is the
    COHORT_WINDOW_DAYS (7) IST calendar days ENDING ON run_date, inclusive —
    so a `--as-of` historical replay queries that date's true cohort rather
    than "the last 7 days from now". Bounds (both present):
        upper (exclusive) = IST midnight starting the day AFTER run_date
        lower (inclusive) = upper - COHORT_WINDOW_DAYS
    i.e. IST dates [run_date - 6 … run_date]. filing_time is stored UTC, so we
    build the IST day boundaries and convert to UTC for the query.
    """
    # IST midnight starting the day after run_date (exclusive upper bound).
    # Built directly from the date parts to avoid importing datetime.time,
    # which would shadow the module-level `import time` used for perf_counter.
    upper_ist = (
        datetime(run_date.year, run_date.month, run_date.day, tzinfo=IST)
        + timedelta(days=1)
    )
    lower_ist = upper_ist - timedelta(days=config.COHORT_WINDOW_DAYS)
    upper_utc = upper_ist.astimezone(UTC)
    lower_utc = lower_ist.astimezone(UTC)

    select_cols = (
        "id, symbol, company_name, quarter, filing_time, source, "
        "parser_confidence, has_exceptional_items, "
        "parsed_at, revenue_cr, pat_cr, opm_pct, "
        "metrics!inner(filing_id, sue_proxy, rev_growth_yoy, ear, "
        "vol_spike, margin_delta, avg_30d_turnover_cr)"
    )
    # Paginate: PostgREST caps a single response at 1000 rows. A dense result
    # week (Q4 season puts >1,000 filings-with-metrics in the trailing 7 days)
    # would otherwise silently keep only the OLDEST 1000 by filing_time —
    # dropping high/medium filings beyond the cap and shrinking the cohort the
    # z-scores are normalized within. Fetch every page until one comes back
    # short. Ordered by (filing_time, id) so the id tiebreaker makes paging
    # stable across rows sharing a filing_time.
    rows: list[dict[str, Any]] = []
    page_size = 1000
    for page in range(_COHORT_MAX_PAGES):
        lo = page * page_size
        resp = (
            db.table("filings")
            .select(select_cols)
            .gte("filing_time", lower_utc.isoformat())
            .lt("filing_time", upper_utc.isoformat())
            .order("filing_time")
            .order("id")
            .range(lo, lo + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
    else:
        log.warning(
            f"ranker: cohort pagination hit the {_COHORT_MAX_PAGES}-page cap "
            f"({_COHORT_MAX_PAGES * page_size} rows) for run_date={run_date}; "
            "cohort may be truncated — investigate window size"
        )

    # Bulk-load fundamentals for every resolvable NSE ticker in the cohort.
    nse_tickers: set[str] = set()
    for r in rows:
        t = to_nse_ticker(r["symbol"], r["source"])
        if t:
            nse_tickers.add(t)

    fundamentals_by_ticker: dict[str, dict[str, Any]] = {}
    if nse_tickers:
        f_resp = (
            db.table("fundamentals")
            .select("symbol, market_cap_cr, sector, listed_long_enough, on_screener")
            .in_("symbol", sorted(nse_tickers))
            .execute()
        )
        for row in (f_resp.data or []):
            fundamentals_by_ticker[row["symbol"]] = row

    # Flatten the joined shape into what filter + scorer expect.
    out: list[dict[str, Any]] = []
    for r in rows:
        metrics_payload = r.get("metrics")
        # PostgREST returns embedded rows as a list (or dict, version-dependent).
        if isinstance(metrics_payload, list):
            metrics_row = metrics_payload[0] if metrics_payload else None
        else:
            metrics_row = metrics_payload
        if not metrics_row:
            continue  # !inner should prevent this, but be defensive

        nse = to_nse_ticker(r["symbol"], r["source"])
        out.append(
            {
                "filing_id": r["id"],
                "symbol": r["symbol"],
                "source": r["source"],
                "quarter": r["quarter"],
                "company_name": r.get("company_name"),
                "filing_time": r["filing_time"],
                "parser_confidence": r.get("parser_confidence"),
                "has_exceptional_items": r.get("has_exceptional_items"),
                "parsed_at": r.get("parsed_at"),
                "revenue_cr": r.get("revenue_cr"),
                "pat_cr": r.get("pat_cr"),
                "opm_pct": r.get("opm_pct"),
                "nse_ticker": nse,
                "metrics": metrics_row,
                "fundamentals": fundamentals_by_ticker.get(nse) if nse else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Cross-source dedup
# ---------------------------------------------------------------------------


def _dedup_cross_source(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse (NSE_ticker, quarter) duplicates across exchanges.

    Tiebreaker (matches BRD §3.1 SOURCES_ORDER):
        NSE > BSE > TRENDLYNE.

    Rows with no resolvable NSE ticker (BSE-only, Trendlyne slug) pass
    through unchanged — they cannot collide with anything since the dedup
    key includes the NSE ticker.
    """
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []

    for r in rows:
        nse = r.get("nse_ticker")
        if nse is None:
            passthrough.append(r)
            continue
        key = (nse, r["quarter"])
        prev = keyed.get(key)
        if prev is None or _source_rank(r["source"]) < _source_rank(prev["source"]):
            if prev is not None:
                log.info(
                    f"ranker: cross-source dedup → dropped filing_id={prev['filing_id']} "
                    f"({prev['source']}) in favor of filing_id={r['filing_id']} "
                    f"({r['source']}) for ({nse}, {r['quarter']})"
                )
            keyed[key] = r
        else:
            log.info(
                f"ranker: cross-source dedup → dropped filing_id={r['filing_id']} "
                f"({r['source']}) in favor of filing_id={prev['filing_id']} "
                f"({prev['source']}) for ({nse}, {r['quarter']})"
            )
    return list(keyed.values()) + passthrough


def _source_rank(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, 99)


# ---------------------------------------------------------------------------
# Shape conversion: cohort row → scorer input
# ---------------------------------------------------------------------------


def _shape_for_scorer(
    rows: list[dict[str, Any]], survivors: dict[int, F.FilterOutcome]
) -> list[dict[str, Any]]:
    """Flatten the joined cohort row into the dict shape `scorer.score_cohort`
    expects. symbol_nse is taken from the filter outcome (already canonical
    and lookup-validated)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        fid = int(r["filing_id"])
        outcome = survivors[fid]                    # all rows here passed filters
        m = r["metrics"]
        out.append(
            {
                "filing_id": fid,
                "symbol_nse": outcome.symbol_nse,
                "sue_proxy":      m.get("sue_proxy"),
                "rev_growth_yoy": m.get("rev_growth_yoy"),
                "ear":            m.get("ear"),
                "vol_spike":      m.get("vol_spike"),
                "margin_delta":   m.get("margin_delta"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_ranking(
    db, run_date: date, top: list[S.ScoredRow], *, cohort_size: int
) -> None:
    """Delete-then-insert for the run_date — idempotent."""
    iso = run_date.isoformat()

    # Wipe prior rows for the same run_date so re-runs are deterministic.
    db.table("rankings").delete().eq("run_date", iso).execute()

    if not top:
        log.info(f"ranker: nothing to insert for run_date={iso}")
        return

    payload = [
        {
            "run_date": iso,
            "filing_id": s.filing_id,
            "symbol_nse": s.symbol_nse,
            "rank": rank,
            "pead_score": s.pead_score,
            "n_components": s.n_components,
            "z_sue": s.z_sue,
            "z_rev": s.z_rev,
            "z_ear": s.z_ear,
            "z_vol": s.z_vol,
            "z_margin": s.z_margin,
            "cohort_size": cohort_size,
        }
        for rank, s in enumerate(top, start=1)
    ]
    db.table("rankings").insert(payload).execute()
    log.info(f"ranker: wrote {len(payload)} ranking rows for run_date={iso}")


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_drop_breakdown(outcomes: list[F.FilterOutcome]) -> None:
    from collections import Counter
    drops = Counter(o.drop_reason for o in outcomes if not o.passed and o.drop_reason)
    if drops:
        log.info(f"ranker: drop breakdown = {dict(drops)}")


def _emit_debug_rows(
    top: list[S.ScoredRow], surviving_rows: list[dict[str, Any]]
) -> None:
    """Print each ranked row with its z components and source-of-truth fields.

    Format (per BRD §8 Phase 4 — operator-facing debug):
        rank=1 symbol=X score=2.30σ z_sue=Y z_rev=Y z_ear=Y z_vol=Y z_margin=Y n_comp=5
          filing_id=N parsed=2026-05-26 confidence=high source=NSE
          revenue=N.N pat=N.N opm=N.N
    """
    by_id = {int(r["filing_id"]): r for r in surviving_rows}
    log.info(f"ranker: RANK_DEBUG=1 — dumping {len(top)} ranked rows")
    for i, s in enumerate(top, start=1):
        cohort = by_id.get(s.filing_id, {})
        parsed_at = cohort.get("parsed_at")
        parsed_label = _date_label(parsed_at)
        log.info(
            f"rank={i} symbol={s.symbol_nse} score={s.pead_score:.2f}σ "
            f"z_sue={_fnum(s.z_sue)} z_rev={_fnum(s.z_rev)} z_ear={_fnum(s.z_ear)} "
            f"z_vol={_fnum(s.z_vol)} z_margin={_fnum(s.z_margin)} "
            f"n_comp={s.n_components}"
        )
        log.info(
            f"  filing_id={s.filing_id} parsed={parsed_label} "
            f"confidence={cohort.get('parser_confidence')} "
            f"source={cohort.get('source')}"
        )
        log.info(
            f"  revenue={_fnum(cohort.get('revenue_cr'))} "
            f"pat={_fnum(cohort.get('pat_cr'))} "
            f"opm={_fnum(cohort.get('opm_pct'))}"
        )


def _fnum(v: Any) -> str:
    """Format a number for the debug dump. None → 'null'."""
    if v is None:
        return "null"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _date_label(ts: Any) -> str:
    """Format an ISO timestamp as YYYY-MM-DD for the debug dump."""
    if ts is None:
        return "null"
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return ts
    if isinstance(ts, datetime):
        return ts.date().isoformat()
    return str(ts)
