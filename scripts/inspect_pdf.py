"""Read raw text from a filing PDF and search for headline numbers + unit hints.

Usage:
    python scripts/inspect_pdf.py --filing-id 87
    python scripts/inspect_pdf.py --filing-id 87 --pages 1-10
"""

from __future__ import annotations

import argparse
import io
import re

import requests
from pypdf import PdfReader

from src.db.client import get_client
from src.sources.bse import BSE_HEADERS


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--filing-id", type=int, required=True)
    p.add_argument("--pages", type=str, default="1-5", help="1-indexed inclusive range")
    args = p.parse_args()

    db = get_client()
    row = (
        db.table("filings")
        .select("id, symbol, company_name, quarter, filing_url, source")
        .eq("id", args.filing_id)
        .single()
        .execute()
        .data
    )
    print(f"Filing id={row['id']} {row['company_name']!r} quarter={row['quarter']}")
    print(f"URL: {row['filing_url']}")

    headers = BSE_HEADERS if row["source"] == "BSE" else {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(row["filing_url"], headers=headers, timeout=60)
    resp.raise_for_status()
    pdf_bytes = resp.content
    print(f"PDF size: {len(pdf_bytes):,} bytes")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    print(f"Total pages: {total_pages}")

    lo_s, hi_s = args.pages.split("-")
    lo, hi = int(lo_s) - 1, int(hi_s)
    hi = min(hi, total_pages)

    # Scan first N pages for unit-of-measurement hints.
    print()
    print("=== UNIT HINTS in pages 1-5 ===")
    unit_re = re.compile(
        r"(rs\.?\s*in\s+(?:lakhs?|crores?|millions?|thousands?)|"
        r"\(in\s+(?:lakhs?|crores?|millions?)\)|"
        r"₹\s*(?:in\s+)?(?:lakhs?|crores?|millions?)|"
        r"amount\s+in\s+(?:lakhs?|crores?|millions?))",
        re.IGNORECASE,
    )
    for i in range(min(5, total_pages)):
        text = reader.pages[i].extract_text() or ""
        for m in unit_re.finditer(text):
            print(f"  page {i+1}: {m.group(0)!r}")

    print()
    print(f"=== RAW TEXT pages {lo+1}-{hi} ===")
    for i in range(lo, hi):
        text = reader.pages[i].extract_text() or ""
        print(f"\n--- page {i+1} ---")
        # Trim each line; collapse blank runs.
        prev_blank = False
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                if not prev_blank:
                    print()
                prev_blank = True
                continue
            prev_blank = False
            print(stripped)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
