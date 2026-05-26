"""Smoke test src.sources.yfinance_adapter against a real filing.

Confirms: (a) symbol resolution picks the right yfinance ticker, (b) the
T-30..T+1 window contains data, (c) helpers (close_on_or_before/after,
volume helpers) return sane values.
"""

from __future__ import annotations

import argparse

from src.db.client import get_client
from src.sources import yfinance_adapter as yfa
from src.utils.time_utils import to_ist


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--filing-ids", type=str, default="93,87,98")
    args = p.parse_args()

    db = get_client()
    ids = [int(x) for x in args.filing_ids.split(",")]
    for fid in ids:
        row = (
            db.table("filings")
            .select("id, symbol, source, filing_time, company_name")
            .eq("id", fid)
            .single()
            .execute()
            .data
        )
        import datetime as _dt
        filing_date = to_ist(
            _dt.datetime.fromisoformat(row["filing_time"].replace("Z", "+00:00"))
        ).date()

        print(
            f"\n=== id={row['id']} {row['symbol']} ({row['source']}) "
            f"{row['company_name'][:40]} ==="
        )
        pref, fb = yfa.resolve_yf_symbol(row["symbol"], row["source"])
        print(f"  symbols: preferred={pref!r} fallback={fb!r}")
        pw = yfa.fetch_ohlcv(row["symbol"], row["source"], filing_date)
        if pw is None:
            print(f"  FETCH FAILED for {filing_date}")
            continue
        print(f"  symbol_used: {pw.symbol_used}  rows: {len(pw.df)}")
        print(f"  date range:  {pw.df.index.min().date()} .. {pw.df.index.max().date()}")
        close_tm1 = yfa.close_on_or_before(pw.df, filing_date)
        close_tp1 = yfa.close_on_or_after(pw.df, filing_date)
        vol_tp1 = yfa.volume_on_or_after(pw.df, filing_date)
        avg_vol = yfa.avg_volume_window(pw.df, filing_date, 30)
        spike = (vol_tp1 / avg_vol) if (vol_tp1 and avg_vol) else None
        print(f"  filing_date={filing_date}")
        print(f"  Close T-1:    {close_tm1}")
        print(f"  Close T+1:    {close_tp1}")
        print(f"  Vol   T+1:    {vol_tp1}")
        print(f"  Avg30 T-1:    {avg_vol}")
        print(f"  Vol_Spike:    {spike}")
    print()
    print("--- Nifty ---")
    nifty_df = yfa.fetch_nifty(filing_date)
    if nifty_df is None:
        print("  FETCH FAILED for Nifty")
    else:
        print(
            f"  rows: {len(nifty_df)}  range: "
            f"{nifty_df.index.min().date()} .. {nifty_df.index.max().date()}"
        )
        print(f"  Close T-1: {yfa.close_on_or_before(nifty_df, filing_date)}")
        print(f"  Close T+1: {yfa.close_on_or_after(nifty_df, filing_date)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
