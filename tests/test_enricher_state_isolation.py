"""Regression test: per-filing state must not leak between iterations of
`enrich_pending`'s loop.

WHY THIS TEST EXISTS — May 26 false-alarm:
    During a `--limit 5` smoke test, two consecutive filings (#86 NSE / SETL
    and #213 BSE / 544333) produced byte-identical metrics. This LOOKED like
    state was bleeding from iteration N into N+1. Investigation showed BSE
    scrip 544333 is the legitimate ISIN-joined NSE ticker SETL — i.e. the
    same company filing on two exchanges (a documented Phase 2 cross-source-
    duplicate pattern). Identical metrics were correct.

    No bug was found, but this test exists as belt-and-suspenders: if a
    future refactor in `_process_one` accidentally introduces shared state
    (a closure, a mutable default, a forgotten module-level cache), this
    test will fail before the bug ships.

What it asserts:
    Two consecutive filings whose symbols resolve to DIFFERENT NSE tickers
    (SETL vs HDFCBANK) produce DIFFERENT metric-row payloads when their
    Screener fundamentals and yfinance OHLCV are stubbed to differ. Identical
    metrics on different symbols would prove iteration-N state leaked into N+1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd
import pytest

from src.pipeline import enricher
from src.sources.gemini_parser import ParsedFiling
from src.sources.yfinance_adapter import PriceWindow

# ---------------------------------------------------------------------------
# Minimal Supabase-shaped fake — captures writes, serves canned reads.
# ---------------------------------------------------------------------------


@dataclass
class _FakeQuery:
    db: _FakeDB
    table: str
    op: str | None = None
    payload: Any = None
    filters: list[tuple] = field(default_factory=list)

    def select(self, _cols: str) -> _FakeQuery:
        return self

    def insert(self, payload: Any) -> _FakeQuery:
        self.op = "insert"
        self.payload = payload
        return self

    def upsert(self, payload: Any, on_conflict: str | None = None) -> _FakeQuery:
        self.op = "upsert"
        self.payload = payload
        return self

    def update(self, payload: Any) -> _FakeQuery:
        self.op = "update"
        self.payload = payload
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col: str, val: Any) -> _FakeQuery:
        self.filters.append(("in", col, val))
        return self

    def gte(self, _c: str, _v: Any) -> _FakeQuery:
        return self

    def lt(self, _c: str, _v: Any) -> _FakeQuery:
        return self

    def is_(self, _c: str, _v: Any) -> _FakeQuery:
        return self

    def order(self, _c: str) -> _FakeQuery:
        return self

    def limit(self, _n: int) -> _FakeQuery:
        return self

    def execute(self) -> Any:
        if self.op in ("insert", "upsert", "update"):
            self.db.writes.append(
                {"table": self.table, "op": self.op, "payload": self.payload,
                 "filters": list(self.filters)}
            )
            return type("Resp", (), {"data": []})()
        data = self.db.reads.get(self.table, [])
        return type("Resp", (), {"data": data})()


@dataclass
class _FakeDB:
    reads: dict[str, list[dict]] = field(default_factory=dict)
    writes: list[dict] = field(default_factory=list)

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ohlcv_df(*, base_close: float, base_volume: float, end: date) -> pd.DataFrame:
    """Build a 45-trading-day DataFrame ending at `end` with monotonic closes."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=45)
    closes = [base_close + i * 0.5 for i in range(len(idx))]
    volumes = [base_volume + i * 100 for i in range(len(idx))]
    return pd.DataFrame({"Close": closes, "Volume": volumes}, index=idx)


def _fundamentals_row(
    *, symbol: str, pat_q4_fy25: float, rev_q4_fy25: float, opm_q4_fy25: float
) -> dict:
    """Build a fundamentals row with at least 4 historical PAT entries (so SUE
    has enough series to compute) and a Q4-FY25 YoY anchor."""
    return {
        "symbol": symbol,
        "on_screener": True,
        "quarterly_pat": [
            {"quarter": "Q4-FY25", "value": pat_q4_fy25},
            {"quarter": "Q3-FY25", "value": pat_q4_fy25 * 0.95},
            {"quarter": "Q2-FY25", "value": pat_q4_fy25 * 0.9},
            {"quarter": "Q1-FY25", "value": pat_q4_fy25 * 0.85},
        ],
        "quarterly_rev": [{"quarter": "Q4-FY25", "value": rev_q4_fy25}],
        "quarterly_opm": [{"quarter": "Q4-FY25", "value": opm_q4_fy25}],
    }


def _filing_row(*, id: int, symbol: str, source: str, url: str) -> dict:
    return {
        "id": id,
        "symbol": symbol,
        "source": source,
        "quarter": "Q4-FY26",
        "filing_time": "2026-05-14T10:00:00+00:00",
        "filing_url": url,
        "parsed_at": None,
        "revenue_cr": None, "pat_cr": None, "eps": None, "opm_pct": None,
        "revenue_yoy_pct": None, "pat_yoy_pct": None,
        "is_consolidated": None, "has_exceptional_items": None,
        "parser_used": None, "parser_confidence": None,
        "metrics": [],  # empty list = no metrics row yet (PostgREST left-join shape)
    }


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


def test_no_state_leak_between_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two distinct symbols -> two distinct metric payloads.

    If iteration N's stock data, fundamentals lookup, or parsed values somehow
    bled into iteration N+1, the captured metric payloads would be identical
    or near-identical. We assert strict inequality on the three price-derived
    and fundamentals-derived metric fields.
    """
    filing_setl = _filing_row(id=1, symbol="SETL", source="NSE", url="https://x/setl.pdf")
    filing_hdfc = _filing_row(id=2, symbol="HDFCBANK", source="NSE", url="https://x/hdfc.pdf")

    db = _FakeDB(
        reads={
            "filings": [filing_setl, filing_hdfc],
            # _load_fundamentals is monkeypatched below; no fundamentals reads hit this fake.
            "fundamentals": [],
        }
    )

    # Per-ticker stubs ---------------------------------------------------------
    setl_funds = _fundamentals_row(
        symbol="SETL", pat_q4_fy25=8.0, rev_q4_fy25=80.0, opm_q4_fy25=12.0
    )
    hdfc_funds = _fundamentals_row(
        symbol="HDFCBANK", pat_q4_fy25=8000.0, rev_q4_fy25=50000.0, opm_q4_fy25=25.0
    )
    funds_by_ticker = {"SETL": setl_funds, "HDFCBANK": hdfc_funds}

    def fake_load_fundamentals(_db: Any, nse_ticker: str | None) -> dict | None:
        return funds_by_ticker.get(nse_ticker) if nse_ticker else None

    monkeypatch.setattr(enricher, "_load_fundamentals", fake_load_fundamentals)

    # PDF download: distinct bytes per filing so we can route the parser stub.
    # Parameter names MUST match the real signature — the enricher calls with
    # `source=...` as a kwarg.
    def fake_download_pdf(url: str, source: str) -> bytes:
        _ = source
        return b"%PDF-SETL" if "setl" in url else b"%PDF-HDFC"

    monkeypatch.setattr(enricher, "_download_pdf", fake_download_pdf)

    # Gemini parser: distinct revenue / PAT per company. Accept the throttle/
    # budget kwargs the enricher now threads through (on_dispatch/sleep/ceiling).
    def fake_parse_pdf(
        pdf_bytes: bytes, expected_quarter: str, *, on_dispatch=None, **_kw
    ) -> ParsedFiling:
        if on_dispatch is not None:
            on_dispatch()  # mimic a single real API dispatch so the gate counts it
        if pdf_bytes == b"%PDF-SETL":
            return ParsedFiling(
                revenue_cr=100.0, pat_cr=10.0, eps=1.0, opm_pct=15.0,
                revenue_yoy_pct=None, pat_yoy_pct=None,
                is_consolidated=False, has_exceptional_items=False,
                confidence="high", notes=None, parser_used="gemini-flash-lite",
            )
        return ParsedFiling(
            revenue_cr=60000.0, pat_cr=9000.0, eps=12.0, opm_pct=22.0,
            revenue_yoy_pct=None, pat_yoy_pct=None,
            is_consolidated=True, has_exceptional_items=False,
            confidence="high", notes=None, parser_used="gemini-flash-lite",
        )

    monkeypatch.setattr(enricher.gemini_parser, "parse_pdf", fake_parse_pdf)

    # yfinance: distinct closes + volumes per ticker -> distinct vol_spike and EAR.
    end = date(2026, 5, 15)

    def fake_fetch_ohlcv(symbol: str, source: str, _filing_date: date) -> PriceWindow | None:
        if symbol == "SETL":
            return PriceWindow(symbol_used="SETL.NS",
                               df=_ohlcv_df(base_close=100.0, base_volume=100_000, end=end))
        return PriceWindow(symbol_used="HDFCBANK.NS",
                           df=_ohlcv_df(base_close=1500.0, base_volume=2_000_000, end=end))

    def fake_fetch_nifty(_filing_date: date) -> pd.DataFrame:
        return _ohlcv_df(base_close=23_000.0, base_volume=0, end=end)

    monkeypatch.setattr(enricher.yfa, "fetch_ohlcv", fake_fetch_ohlcv)
    monkeypatch.setattr(enricher.yfa, "fetch_nifty", fake_fetch_nifty)

    # Run -----------------------------------------------------------------------
    outcomes = enricher.enrich_pending(db, dry_run=False)

    # Sanity: both filings processed, both produced metric rows.
    assert len(outcomes) == 2
    assert all(o.metrics_inserted for o in outcomes), outcomes
    assert all(o.error is None for o in outcomes), outcomes

    # The interesting assertion — extract metrics upserts and prove they differ.
    metric_writes = [w for w in db.writes if w["table"] == "metrics"]
    assert len(metric_writes) == 2, f"expected 2 metric upserts, got {len(metric_writes)}"

    by_filing = {w["payload"]["filing_id"]: w["payload"] for w in metric_writes}
    setl_metrics = by_filing[1]
    hdfc_metrics = by_filing[2]

    # No leak: at least one of the three "shared input" derived metrics must differ.
    # We assert ALL three differ — if a future refactor accidentally re-uses
    # iteration-N's price_window for iteration N+1, vol_spike+ear would match.
    assert setl_metrics["vol_spike"] != hdfc_metrics["vol_spike"], (
        f"vol_spike collision suggests yfinance data leak: "
        f"setl={setl_metrics['vol_spike']} hdfc={hdfc_metrics['vol_spike']}"
    )
    assert setl_metrics["ear"] != hdfc_metrics["ear"], (
        f"EAR collision suggests close/nifty data leak: "
        f"setl={setl_metrics['ear']} hdfc={hdfc_metrics['ear']}"
    )
    assert setl_metrics["rev_growth_yoy"] != hdfc_metrics["rev_growth_yoy"], (
        f"rev_growth_yoy collision suggests parsed/fundamentals leak: "
        f"setl={setl_metrics['rev_growth_yoy']} hdfc={hdfc_metrics['rev_growth_yoy']}"
    )
    assert setl_metrics["margin_delta"] != hdfc_metrics["margin_delta"], (
        f"margin_delta collision suggests fundamentals leak: "
        f"setl={setl_metrics['margin_delta']} hdfc={hdfc_metrics['margin_delta']}"
    )
    assert setl_metrics["sue_proxy"] != hdfc_metrics["sue_proxy"], (
        f"SUE collision suggests fundamentals leak: "
        f"setl={setl_metrics['sue_proxy']} hdfc={hdfc_metrics['sue_proxy']}"
    )
