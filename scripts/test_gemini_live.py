"""One-shot live test of src.sources.gemini_parser against a real production PDF.

Picks ONE filing from the production filings table where filing_url IS NOT NULL,
downloads the PDF, runs the parser, prints JSON / latency / validation status.

This is a debugging script — not part of the application pipeline. Safe to delete
after Phase 3 acceptance.

Usage:
    python scripts/test_gemini_live.py
    python scripts/test_gemini_live.py --filing-id 42   # target a specific filing
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time

import requests

from src.db.client import get_client
from src.sources import gemini_parser
from src.sources.bse import BSE_HEADERS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filing-id", type=int, default=None)
    args = parser.parse_args()

    db = get_client()
    q = (
        db.table("filings")
        .select("id, symbol, company_name, quarter, filing_url, source, filing_time")
        .not_.is_("filing_url", "null")
    )
    if args.filing_id is not None:
        q = q.eq("id", args.filing_id)
    else:
        q = q.eq("source", "BSE").order("id").limit(1)
    rows = q.execute().data or []
    if not rows:
        print("No filing matching criteria.", file=sys.stderr)
        return 2
    f = rows[0]
    print(
        f"Selected filing: id={f['id']} symbol={f['symbol']} "
        f"company={f['company_name'][:60]!r} quarter={f['quarter']} "
        f"source={f['source']}"
    )
    print(f"  URL: {f['filing_url']}")

    headers = BSE_HEADERS if f["source"] == "BSE" else {"User-Agent": "Mozilla/5.0"}
    t_dl0 = time.perf_counter()
    resp = requests.get(f["filing_url"], headers=headers, timeout=60)
    dl_ms = int((time.perf_counter() - t_dl0) * 1000)
    resp.raise_for_status()
    pdf_bytes = resp.content
    print(f"  Downloaded {len(pdf_bytes):,} bytes in {dl_ms}ms")
    if not pdf_bytes.startswith(b"%PDF"):
        print(f"  WARNING: response does not look like a PDF (first 8 bytes: {pdf_bytes[:8]!r})")

    print()
    print("Calling Gemini parser...")
    t0 = time.perf_counter()
    try:
        parsed = gemini_parser.parse_pdf(pdf_bytes, expected_quarter=f["quarter"])
    except gemini_parser.ParseFailure as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        print(f"FAILED in {elapsed_ms}ms: {e}")
        print(f"  last_error type: {type(e.last_error).__name__}")
        print(f"  last_error: {e.last_error}")
        return 1
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    print(f"OK in {elapsed_ms}ms (parser_used={parsed.parser_used})")
    print()
    print("Extracted JSON:")
    print(json.dumps(dataclasses.asdict(parsed), indent=2))
    print()
    print("Pydantic validation status: PASSED (parser only returns validated objects)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
