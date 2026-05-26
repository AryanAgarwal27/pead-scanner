"""Pick diverse test filings: 1 NSE small-PDF, 2 more BSE.
Outputs filing IDs for downstream live-test runs.
"""

from src.db.client import get_client


def main() -> None:
    db = get_client()
    rows = (
        db.table("filings")
        .select("id, symbol, company_name, source, filing_url")
        .not_.is_("filing_url", "null")
        .execute()
        .data
    )
    nse = [r for r in rows if r["source"] == "NSE"][:5]
    bse = [r for r in rows if r["source"] == "BSE"][:10]
    print("NSE candidates (first 5):")
    for r in nse:
        print(f"  id={r['id']:>4} {r['symbol']:<15} {r['company_name'][:50]}")
    print("BSE candidates (first 10):")
    for r in bse:
        print(f"  id={r['id']:>4} {r['symbol']:<15} {r['company_name'][:50]}")


if __name__ == "__main__":
    main()
