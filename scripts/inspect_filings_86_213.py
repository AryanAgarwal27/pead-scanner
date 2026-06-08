"""Diagnostic: are filings 86 and 213 the same company filing via two exchanges,
or is there a real symbol-map leak?
"""

import json
import pathlib

from src.db.client import get_client
from src.sources.symbol_map import to_nse_ticker


def main() -> None:
    db = get_client()
    rows = (
        db.table("filings")
        .select("id, symbol, source, company_name, quarter, filing_time")
        .in_("id", [86, 213, 212])
        .execute()
        .data
    )
    print("Filings:")
    for r in sorted(rows, key=lambda x: x["id"]):
        nse = to_nse_ticker(r["symbol"], r["source"])
        print(
            f"  id={r['id']:>4}  source={r['source']:<3}  symbol={r['symbol']:<10}  "
            f"-> to_nse_ticker={nse!r}  {r['company_name']!r}"
        )

    map_path = pathlib.Path("src/data/bse_to_nse.json")
    m = json.loads(map_path.read_text(encoding="utf-8"))
    for scrip in ("544333", "544066"):
        print(f"  bse_to_nse[{scrip!r}] = {m.get(scrip)!r}")


if __name__ == "__main__":
    main()
