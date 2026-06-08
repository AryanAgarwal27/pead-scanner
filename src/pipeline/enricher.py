"""Enricher orchestration — Phase 3.

For each filing that needs metrics, this module:
    1. Downloads the filing PDF (gated by Gemini-size cap from BRD/spec).
    2. Parses headline numbers via Gemini → regex fallback chain.
    3. Persists parsed numbers + parser_used + parser_confidence on the
       `filings` row.
    4. Pulls historical fundamentals from the local `fundamentals` table
       (populated nightly by screener-cache).
    5. Pulls OHLCV + Nifty closes via yfinance.
    6. Computes the 5 PEAD metrics (src.pipeline.metrics).
    7. Z-CHECK: warns loudly if current Gemini-extracted revenue/PAT is
       >5x or <0.2x the most recent Screener quarter — possible unit error.
    8. Upserts one `metrics` row per filing.

Idempotency:
    - Parse step gated by `filings.parsed_at IS NOT NULL` — never re-call Gemini.
    - Metric step gated by metrics PK on filing_id — upsert overwrites.

Aged-out filings (BRD §3.3 plus Mod 2 from Phase 3 plan):
    Filings older than 14 days that still lack a metrics row are logged at
    WARNING level — no silent drop. Manual review required.

The job (jobs/enrich_eod.py) is the entry point; this module is library code.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests
from pypdf import PdfReader

from src.pipeline import metrics as M
from src.sources import gemini_parser, regex_parser
from src.sources import yfinance_adapter as yfa
from src.sources.bse import BSE_HEADERS
from src.sources.gemini_parser import ParsedFiling
from src.sources.symbol_map import to_nse_ticker
from src.utils.logging import get_logger
from src.utils.time_utils import IST, to_ist

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Per Phase 3 Q3 decision: Gemini size gate. PDFs above these go straight to regex.
GEMINI_MAX_SIZE_BYTES = 20 * 1024 * 1024     # 20 MB (Gemini File API limit on inline)
# Heuristic: anything larger is almost certainly an annual-report attachment.
GEMINI_MAX_PAGES = 50

# Mod 2: aged-out window. Filings older than this without a metrics row are flagged.
ENRICH_WINDOW_DAYS = 14

# Mod 1+2 (instrumentation): Z-CHECK thresholds. If Gemini PAT > 5x last-quarter
# Screener PAT (or < 0.2x), warn loudly. Same on revenue.
Z_CHECK_UPPER = 5.0
Z_CHECK_LOWER = 0.2

# yfinance window — 30 trading-day average covers ~45 calendar days.
VOL_AVG_WINDOW_DAYS = 30

# TODO: phase 4 follow-up. The Phase 4 filterer (src.pipeline.filterer) lazily
# computes avg_30d_turnover_cr at ranking time using a second yfinance fetch
# per filing. Post-Phase 6, fold that calculation into this enricher's
# existing T-45..T+14 fetch so the filterer can read it straight from the
# metrics row without re-pulling OHLCV.


# ---------------------------------------------------------------------------
# Public entry point — used by jobs/enrich_eod.py
# ---------------------------------------------------------------------------


@dataclass
class EnrichOutcome:
    """One per filing processed."""

    filing_id: int
    symbol: str
    parser_used: str | None
    parser_confidence: str | None
    metrics_inserted: bool
    z_check_tripped: bool
    error: str | None = None


def enrich_pending(
    db, *, dry_run: bool = False, limit: int | None = None
) -> list[EnrichOutcome]:
    """Process filings within the 14-day window that lack a metrics row.

    Args:
        db: supabase client (from `src.db.client.get_client()`).
        dry_run: if True, computes everything but writes nothing.
        limit: if set, process only the first N pending filings (oldest-first
            per filing_time). Aged-out warning logs are NOT subject to the
            limit — operators always see the full list of stale filings.

    Returns one EnrichOutcome per filing processed. Also emits WARNING logs
    for filings older than ENRICH_WINDOW_DAYS that still lack metrics.
    """
    _log_aged_out(db)

    pending = _select_pending(db)
    total = len(pending)
    if limit is not None and limit < total:
        pending = pending[:limit]
        log.info(
            f"enricher: {total} filings pending; --limit={limit} -> "
            f"processing first {len(pending)} (within {ENRICH_WINDOW_DAYS}-day window)"
        )
    else:
        log.info(
            f"enricher: {total} filings pending (within {ENRICH_WINDOW_DAYS}-day window)"
        )
    outcomes: list[EnrichOutcome] = []

    for f in pending:
        try:
            outcomes.append(_process_one(db, f, dry_run=dry_run))
        except Exception as e:  # noqa: BLE001 — never abort the batch on one bad filing
            log.exception(f"enricher: unhandled error on filing_id={f['id']}: {e}")
            outcomes.append(
                EnrichOutcome(
                    filing_id=f["id"],
                    symbol=f["symbol"],
                    parser_used=None,
                    parser_confidence=None,
                    metrics_inserted=False,
                    z_check_tripped=False,
                    error=str(e),
                )
            )
    return outcomes


# ---------------------------------------------------------------------------
# Per-filing pipeline
# ---------------------------------------------------------------------------


def _process_one(db, f: dict, *, dry_run: bool) -> EnrichOutcome:
    filing_id: int = f["id"]
    symbol: str = f["symbol"]
    source: str = f["source"]
    quarter: str = f["quarter"]
    filing_time = _parse_supabase_ts(f["filing_time"])
    filing_date = to_ist(filing_time).date()

    log.info(f"enricher: processing filing_id={filing_id} {symbol} ({source}) {quarter}")

    # ---- Parse (cached on filings.parsed_at) -------------------------------
    if f.get("parsed_at") is None:
        parsed = _parse_filing(f, quarter)
        if not dry_run:
            _persist_parse(db, filing_id, parsed)
    else:
        parsed = _parsed_from_filing_row(f)
        log.info(
            f"  parse cached (parser_used={parsed.parser_used}, "
            f"confidence={parsed.confidence})"
        )

    # ---- Fundamentals (Screener cache) ------------------------------------
    nse_ticker = to_nse_ticker(symbol, source)
    fundamentals = _load_fundamentals(db, nse_ticker) if nse_ticker else None
    if nse_ticker and fundamentals is None:
        log.info(f"  no Screener cache for {nse_ticker} — SUE/Margin_Delta will be NULL")
    elif not nse_ticker:
        log.info(f"  no NSE ticker for {symbol} ({source}) — SUE/Margin_Delta will be NULL")

    # ---- Z-CHECK (Mod 2 instrumentation) ----------------------------------
    z_tripped = _z_check(filing_id, symbol, parsed, fundamentals)

    # ---- Price data (yfinance) --------------------------------------------
    pw = yfa.fetch_ohlcv(symbol, source, filing_date)
    nifty_df = yfa.fetch_nifty(filing_date)

    # ---- Metric inputs -----------------------------------------------------
    inputs = _assemble_metric_inputs(
        parsed=parsed,
        fundamentals=fundamentals,
        price_window=pw,
        nifty_df=nifty_df,
        filing_date=filing_date,
    )

    # T+1 must have actually occurred in real-world price data; else skip.
    if inputs is None:
        log.info(
            f"  T+1 trading day not yet available for filing_date={filing_date}; "
            "skipping metrics (will retry next run)"
        )
        return EnrichOutcome(
            filing_id=filing_id,
            symbol=symbol,
            parser_used=parsed.parser_used,
            parser_confidence=parsed.confidence,
            metrics_inserted=False,
            z_check_tripped=z_tripped,
        )

    # ---- Compute metrics ---------------------------------------------------
    last_8_pat = _historical_pat(fundamentals)
    opm_yoy = _opm_yoy(fundamentals, quarter)
    rev_yoy = _yoy_value(fundamentals, "quarterly_rev", quarter) or parsed.revenue_yoy_pct
    pat_yoy = _yoy_value(fundamentals, "quarterly_pat", quarter) or parsed.pat_yoy_pct

    metric_row: dict[str, Any] = {
        "filing_id": filing_id,
        "sue_proxy": M.sue_proxy(parsed.pat_cr, pat_yoy, last_8_pat),
        "rev_growth_yoy": M.rev_growth_yoy(parsed.revenue_cr, rev_yoy),
        "vol_spike": M.vol_spike(inputs["vol_t1"], inputs["prior_30d_vols"]),
        "ear": M.ear(
            inputs["close_tm1"], inputs["close_tp1"],
            inputs["nifty_tm1"], inputs["nifty_tp1"],
        ),
        "margin_delta": M.margin_delta(parsed.opm_pct, opm_yoy),
        "computed_at": datetime.now(UTC).isoformat(),
    }
    log.info(
        f"  metrics: sue={metric_row['sue_proxy']} "
        f"rev_g={metric_row['rev_growth_yoy']} vol_spike={metric_row['vol_spike']} "
        f"ear={metric_row['ear']} margin_delta={metric_row['margin_delta']}"
    )

    if not dry_run:
        db.table("metrics").upsert(metric_row, on_conflict="filing_id").execute()
    return EnrichOutcome(
        filing_id=filing_id,
        symbol=symbol,
        parser_used=parsed.parser_used,
        parser_confidence=parsed.confidence,
        metrics_inserted=True,
        z_check_tripped=z_tripped,
    )


# ---------------------------------------------------------------------------
# Sub-steps
# ---------------------------------------------------------------------------


def _parse_filing(f: dict, quarter: str) -> ParsedFiling:
    """Download + parse via Gemini→regex fallback chain.

    Returns a ParsedFiling. If everything fails, returns one with
    confidence='failed' (caller still proceeds; metric step will set
    parsed-dependent metrics to None)."""
    url = f.get("filing_url")
    if not url:
        log.warning(f"  no filing_url for filing_id={f['id']}; skipping LLM parse")
        return _failed_parsed("no filing_url")

    try:
        pdf_bytes = _download_pdf(url, source=f["source"])
    except Exception as e:  # noqa: BLE001
        log.warning(f"  PDF download failed ({url}): {e}")
        return _failed_parsed(f"pdf download failed: {e}")

    # Gemini size gate — large PDFs go straight to regex.
    if _exceeds_gemini_gate(pdf_bytes):
        log.info(
            f"  PDF exceeds Gemini gate ({len(pdf_bytes):,}B) — going straight to regex"
        )
        return regex_parser.parse_pdf(pdf_bytes)

    # Gemini tier (Flash-Lite -> Flash internally).
    t0 = time.perf_counter()
    try:
        parsed = gemini_parser.parse_pdf(pdf_bytes, expected_quarter=quarter)
        log.info(
            f"  Gemini OK in {int((time.perf_counter() - t0) * 1000)}ms "
            f"(parser={parsed.parser_used}, confidence={parsed.confidence})"
        )
        return parsed
    except gemini_parser.ParseFailure as e:
        log.warning(f"  Gemini exhausted in {int((time.perf_counter() - t0)*1000)}ms: {e}")
        # Regex last-resort.
        return regex_parser.parse_pdf(pdf_bytes)


def _persist_parse(db, filing_id: int, parsed: ParsedFiling) -> None:
    """Mod 1 instrumentation: store parser_used + parser_confidence alongside
    the parsed numbers so future audits can identify which filings the
    parser fumbled."""
    payload: dict[str, Any] = {
        "revenue_cr": parsed.revenue_cr,
        "pat_cr": parsed.pat_cr,
        "eps": parsed.eps,
        "opm_pct": parsed.opm_pct,
        "revenue_yoy_pct": parsed.revenue_yoy_pct,
        "pat_yoy_pct": parsed.pat_yoy_pct,
        "is_consolidated": parsed.is_consolidated,
        "has_exceptional_items": parsed.has_exceptional_items,
        "parser_used": parsed.parser_used,
        "parser_confidence": parsed.confidence,
        "parsed_at": datetime.now(UTC).isoformat(),
    }
    db.table("filings").update(payload).eq("id", filing_id).execute()
    log.info(
        f"  persisted parse: revenue={parsed.revenue_cr} pat={parsed.pat_cr} "
        f"parser={parsed.parser_used} confidence={parsed.confidence}"
    )


def _z_check(
    filing_id: int,
    symbol: str,
    parsed: ParsedFiling,
    fundamentals: dict | None,
) -> bool:
    """Mod 2 instrumentation: flag suspicious unit-conversion errors.

    Compares current Gemini-extracted revenue / PAT against the most recent
    Screener-cached quarter. If ratio is >5x or <0.2x, log a loud WARNING.
    Does NOT suppress the filing — Phase 4 ranking decides separately.

    Returns True if a Z-CHECK warning was emitted.
    """
    if fundamentals is None:
        return False

    tripped = False

    def _ratio_check(
        field_name: str,
        gemini_val: float | None,
        screener_list: list[dict] | None,
    ) -> bool:
        if gemini_val is None or not screener_list:
            return False
        last = screener_list[0].get("value") if screener_list[0] else None
        if last is None or last == 0:
            return False
        ratio = gemini_val / last
        if ratio > Z_CHECK_UPPER or ratio < Z_CHECK_LOWER:
            log.warning(
                f"[WARNING] enricher: Z-CHECK filing_id={filing_id} symbol={symbol} "
                f"{field_name}_jump={ratio:.2f}x — possible parser unit error, "
                f"review filings table (gemini={gemini_val}, last_q_screener={last})"
            )
            return True
        return False

    tripped |= _ratio_check("revenue", parsed.revenue_cr, fundamentals.get("quarterly_rev"))
    tripped |= _ratio_check("pat", parsed.pat_cr, fundamentals.get("quarterly_pat"))
    return tripped


def _assemble_metric_inputs(
    *,
    parsed: ParsedFiling,
    fundamentals: dict | None,
    price_window: yfa.PriceWindow | None,
    nifty_df,
    filing_date: date,
) -> dict | None:
    """Collect all price-derived inputs the metric functions need.

    Returns None if T+1 hasn't materialized yet (filing is too recent and
    yfinance has no post-filing trading day). The enricher then defers
    metrics computation to a later run.
    """
    t_minus_1 = filing_date - timedelta(days=1)
    t_plus_1 = filing_date + timedelta(days=1)

    close_tp1 = None
    close_tm1 = None
    vol_tp1 = None
    prior_vols: list[float] = []
    nifty_tm1 = None
    nifty_tp1 = None

    if price_window is not None and not price_window.empty:
        close_tm1 = yfa.close_on_or_before(price_window.df, t_minus_1)
        close_tp1 = yfa.close_on_or_after(price_window.df, t_plus_1)
        vol_tp1 = yfa.volume_on_or_after(price_window.df, t_plus_1)
        # Build the prior-30-trading-day volume series ending at T-1.
        cutoff_ts = price_window.df.index <= __ts(t_minus_1)
        prior_slice = price_window.df[cutoff_ts].tail(VOL_AVG_WINDOW_DAYS)
        prior_vols = [float(v) for v in prior_slice["Volume"] if v and v > 0]
    if nifty_df is not None and not nifty_df.empty:
        nifty_tm1 = yfa.close_on_or_before(nifty_df, t_minus_1)
        nifty_tp1 = yfa.close_on_or_after(nifty_df, t_plus_1)

    # If the stock has no T+1 trading day yet, OR Nifty has no T+1, defer.
    # We require AT LEAST one T+1 datapoint per side (price OR nifty); if
    # both stock and nifty miss T+1 entirely, defer.
    stock_t1_available = close_tp1 is not None or vol_tp1 is not None
    nifty_t1_available = nifty_tp1 is not None
    if not (stock_t1_available or nifty_t1_available):
        return None

    return {
        "close_tm1": close_tm1,
        "close_tp1": close_tp1,
        "vol_t1": vol_tp1,
        "prior_30d_vols": prior_vols,
        "nifty_tm1": nifty_tm1,
        "nifty_tp1": nifty_tp1,
    }


def __ts(d: date):  # tiny pandas-Timestamp helper kept local
    import pandas as pd
    return pd.Timestamp(d)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------


def _select_pending(db) -> list[dict]:
    """Return filings within the 14-day window whose metrics row is missing.

    Approach: select recent filings, join in metrics (left), keep rows with
    no metrics row.
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(days=ENRICH_WINDOW_DAYS)).isoformat()
    # Fetch filings + their metrics row (if any) in one trip.
    resp = (
        db.table("filings")
        .select(
            "id, symbol, source, quarter, filing_time, filing_url, parsed_at, "
            "revenue_cr, pat_cr, eps, opm_pct, revenue_yoy_pct, pat_yoy_pct, "
            "is_consolidated, has_exceptional_items, parser_used, parser_confidence, "
            "metrics(filing_id)"
        )
        .gte("filing_time", cutoff_iso)
        .order("filing_time")
        .execute()
    )
    rows = resp.data or []
    return [r for r in rows if not r.get("metrics")]


def _log_aged_out(db) -> None:
    """Find filings older than ENRICH_WINDOW_DAYS without metrics → loud WARN."""
    cutoff = datetime.now(UTC) - timedelta(days=ENRICH_WINDOW_DAYS)
    resp = (
        db.table("filings")
        .select("id, symbol, quarter, filing_time, metrics(filing_id)")
        .lt("filing_time", cutoff.isoformat())
        # Hard cap to avoid loading the whole history.
        .gte("filing_time", (cutoff - timedelta(days=60)).isoformat())
        .execute()
    )
    aged = [r for r in (resp.data or []) if not r.get("metrics")]
    for r in aged:
        log.warning(
            f"[WARNING] enricher: filing_id={r['id']} aged out without metrics after "
            f"{ENRICH_WINDOW_DAYS} days; manual review required "
            f"(symbol={r['symbol']} quarter={r['quarter']} filing_time={r['filing_time']})"
        )


def _load_fundamentals(db, nse_ticker: str) -> dict | None:
    """Return the fundamentals row dict, or None if missing / 404'd."""
    try:
        resp = (
            db.table("fundamentals")
            .select("*")
            .eq("symbol", nse_ticker)
            .limit(1)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"fundamentals query failed for {nse_ticker}: {e}")
        return None
    rows = resp.data or []
    if not rows:
        return None
    row = rows[0]
    if not row.get("on_screener"):
        return None
    return row


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _exceeds_gemini_gate(pdf_bytes: bytes) -> bool:
    if len(pdf_bytes) > GEMINI_MAX_SIZE_BYTES:
        return True
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:  # noqa: BLE001
        return False  # let Gemini try; size cap already cleared
    return len(reader.pages) > GEMINI_MAX_PAGES


def _download_pdf(url: str, source: str) -> bytes:
    headers = BSE_HEADERS if source == "BSE" else {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


def _failed_parsed(reason: str) -> ParsedFiling:
    return ParsedFiling(
        revenue_cr=None,
        pat_cr=None,
        eps=None,
        opm_pct=None,
        revenue_yoy_pct=None,
        pat_yoy_pct=None,
        is_consolidated=None,
        has_exceptional_items=False,
        confidence="failed",
        notes=reason,
        parser_used="regex",
    )


def _parsed_from_filing_row(f: dict) -> ParsedFiling:
    """Reconstitute a ParsedFiling from a cached filings row."""
    return ParsedFiling(
        revenue_cr=f.get("revenue_cr"),
        pat_cr=f.get("pat_cr"),
        eps=f.get("eps"),
        opm_pct=f.get("opm_pct"),
        revenue_yoy_pct=f.get("revenue_yoy_pct"),
        pat_yoy_pct=f.get("pat_yoy_pct"),
        is_consolidated=f.get("is_consolidated"),
        has_exceptional_items=bool(f.get("has_exceptional_items")),
        confidence=f.get("parser_confidence") or "medium",
        notes=None,
        parser_used=f.get("parser_used") or "gemini-flash-lite",
    )


def _historical_pat(fundamentals: dict | None) -> list[float]:
    """Last 8 quarters of PAT, newest-first → bare float list."""
    if not fundamentals:
        return []
    series = fundamentals.get("quarterly_pat") or []
    return [float(e["value"]) for e in series if e.get("value") is not None]


def _opm_yoy(fundamentals: dict | None, current_quarter: str) -> float | None:
    """Find the YoY-same-quarter OPM from Screener cache. None if not found."""
    if not fundamentals:
        return None
    series = fundamentals.get("quarterly_opm") or []
    yoy_label = _yoy_quarter_label(current_quarter)
    for entry in series:
        if entry.get("quarter") == yoy_label:
            v = entry.get("value")
            return float(v) if v is not None else None
    return None


def _yoy_value(fundamentals: dict | None, field: str, current_quarter: str) -> float | None:
    if not fundamentals:
        return None
    series = fundamentals.get(field) or []
    yoy_label = _yoy_quarter_label(current_quarter)
    for entry in series:
        if entry.get("quarter") == yoy_label:
            v = entry.get("value")
            return float(v) if v is not None else None
    return None


def _yoy_quarter_label(quarter: str) -> str:
    """'Q4-FY26' -> 'Q4-FY25' (same quarter, prior fiscal year)."""
    qpart, fypart = quarter.split("-")
    fy_yy = int(fypart[2:])
    return f"{qpart}-FY{(fy_yy - 1) % 100:02d}"


def _parse_supabase_ts(ts: str) -> datetime:
    """Supabase returns ISO-8601 with 'Z' or '+00:00'. Normalize to aware UTC."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


# Silence unused-import warnings for IST (referenced by external callers via re-export).
_ = IST
