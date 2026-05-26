"""One-shot live test of src.sources.regex_parser against production PDFs."""

from __future__ import annotations

import argparse
import dataclasses
import json
import time

import requests

from src.db.client import get_client
from src.sources import regex_parser
from src.sources.bse import BSE_HEADERS


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--filing-ids", type=str, default="87,2,93,98")
    args = p.parse_args()

    db = get_client()
    ids = [int(x) for x in args.filing_ids.split(",")]
    for fid in ids:
        row = (
            db.table("filings")
            .select("id, symbol, company_name, quarter, filing_url, source")
            .eq("id", fid)
            .single()
            .execute()
            .data
        )
        print(f"\n=== id={row['id']} {row['symbol']} ({row['source']}) {row['quarter']} ===")
        headers = BSE_HEADERS if row["source"] == "BSE" else {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(row["filing_url"], headers=headers, timeout=60)
        resp.raise_for_status()
        t0 = time.perf_counter()
        parsed = regex_parser.parse_pdf(resp.content)
        ms = int((time.perf_counter() - t0) * 1000)
        print(f"  latency: {ms}ms")
        print(json.dumps(dataclasses.asdict(parsed), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
