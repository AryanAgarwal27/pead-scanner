"""Tally symbol-map coverage across all current filings."""

from collections import Counter

from src.db.client import get_client
from src.sources.symbol_map import to_nse_ticker


def main() -> None:
    db = get_client()
    rows = db.table("filings").select("symbol, source").execute().data
    status: Counter[str] = Counter()
    for r in rows:
        nse = to_nse_ticker(r["symbol"], r["source"])
        src = r["source"]
        status[f"{src}_resolved" if nse else f"{src}_unresolved"] += 1
    for k, v in sorted(status.items()):
        print(f"  {k}: {v}")
    print(f"Total filings: {sum(status.values())}")


if __name__ == "__main__":
    main()
