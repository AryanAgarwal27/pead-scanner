"""Screener.in nightly fundamentals cache — Phase 3.

Scrapes the public quarterly-results table from screener.in for each NSE
ticker we care about, extracting up to 8 quarters of Sales (Revenue), Net
Profit (PAT), and OPM%. These power the enricher's SUE_proxy and
Margin_Delta calculations (BRD §3.3 FR-3.1).

Etiquette (BRD §3.1 FR-1.6): used ONLY in the nightly screener-cache job —
NEVER hit Screener during real-time poll cycles. One-second sleep between
requests in batch mode (see jobs/screener_cache.py); this module exposes
a single-symbol fetch.

URL strategy:
    1. Try `https://www.screener.in/company/<SYMBOL>/consolidated/` first —
       consolidated numbers are the BRD-preferred basis (FR-3.3).
    2. On 404 (no consolidated tab), fall back to `/company/<SYMBOL>/`
       (standalone).
    3. On 404 again, mark the cache row as on_screener=false and set
       last_404_at. The 30-day TTL is enforced by the caller (job code).

Returned data is the SCRAPED RAW — no unit-conversion needed because
Screener displays values in ₹ Crores universally. Negative values are
shown as plain numbers with a minus sign or in parentheses; we coerce.

Test/CI: tests/fixtures/screener_cmsinfo.html captures a real page so the
parser is unit-testable without network. Run scripts/refresh_screener_fixture.py
if Screener's HTML structure changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import requests
from bs4 import BeautifulSoup, Tag

from src.utils.logging import get_logger
from src.utils.retry import with_retries

log = get_logger(__name__)

SCREENER_URL_TEMPLATE_CONS = "https://www.screener.in/company/{symbol}/consolidated/"
SCREENER_URL_TEMPLATE_STD = "https://www.screener.in/company/{symbol}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class ScreenerFundamentals:
    """One company's most recent ~8 quarters of fundamentals from Screener."""

    symbol: str                            # NSE ticker we queried
    company_name: str | None = None
    market_cap_cr: float | None = None
    sector: str | None = None
    # Each list is newest-first, up to 8 entries. Quarter labels follow
    # Indian-FY convention (Q1-FY26 etc) — see _column_to_quarter_label.
    quarterly_pat: list[dict] = field(default_factory=list)
    quarterly_rev: list[dict] = field(default_factory=list)
    quarterly_opm: list[dict] = field(default_factory=list)
    used_basis: Literal["consolidated", "standalone"] = "consolidated"
    on_screener: bool = True               # False = 404 from Screener


class ScreenerNotFound(Exception):
    """Symbol returned 404 from Screener (consolidated AND standalone)."""


def fetch_fundamentals(nse_ticker: str) -> ScreenerFundamentals:
    """Try consolidated, fall back to standalone. Raises ScreenerNotFound on 404."""
    nse_ticker = nse_ticker.upper().strip()

    for basis, url in (
        ("consolidated", SCREENER_URL_TEMPLATE_CONS.format(symbol=nse_ticker)),
        ("standalone", SCREENER_URL_TEMPLATE_STD.format(symbol=nse_ticker)),
    ):
        html = _fetch_page(url)
        if html is None:
            continue
        try:
            return _parse_html(html, nse_ticker, basis=basis)
        except _NoQuarterTable:
            log.info(f"screener: {nse_ticker} {basis} page had no quarterly table; trying next")
            continue
    raise ScreenerNotFound(f"Screener has no usable page for {nse_ticker}")


# ---------------------------------------------------------------------------
# Internal — HTTP + parsing
# ---------------------------------------------------------------------------


class _NoQuarterTable(Exception):
    """Page loaded but contained no section#quarters table."""


def _fetch_page(url: str) -> str | None:
    """GET url. Returns text on 2xx, None on 404, raises otherwise (retried)."""

    def _do() -> requests.Response:
        return requests.get(url, headers=_HEADERS, timeout=20)

    resp = with_retries(_do)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _parse_html(
    html: str, nse_ticker: str, *, basis: Literal["consolidated", "standalone"]
) -> ScreenerFundamentals:
    soup = BeautifulSoup(html, "lxml")
    company_name = _extract_company_name(soup)
    market_cap = _extract_market_cap(soup)
    sector = _extract_sector(soup)

    section = soup.find("section", id="quarters")
    if not isinstance(section, Tag):
        raise _NoQuarterTable()
    table = section.find("table")
    if not isinstance(table, Tag):
        raise _NoQuarterTable()

    headers = _extract_quarter_headers(table)
    if not headers:
        raise _NoQuarterTable()

    rows = _extract_rows(table)

    # Find the row labels we care about. Row names on Screener have a trailing
    # '+' badge sometimes (drill-down indicator); strip it.
    rev_values = _row_values(rows, ("Sales", "Revenue"))
    pat_values = _row_values(rows, ("Net Profit",))
    opm_values = _row_values(rows, ("OPM %", "OPM"))

    # Build newest-first lists, max 8 entries.
    rev_list = _zip_newest_first(headers, rev_values, parse_money=True)
    pat_list = _zip_newest_first(headers, pat_values, parse_money=True)
    opm_list = _zip_newest_first(headers, opm_values, parse_money=False)

    return ScreenerFundamentals(
        symbol=nse_ticker,
        company_name=company_name,
        market_cap_cr=market_cap,
        sector=sector,
        quarterly_rev=rev_list,
        quarterly_pat=pat_list,
        quarterly_opm=opm_list,
        used_basis=basis,
        on_screener=True,
    )


# ---------------------------------------------------------------------------
# Cell + header helpers
# ---------------------------------------------------------------------------


def _extract_company_name(soup: BeautifulSoup) -> str | None:
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else None


def _extract_market_cap(soup: BeautifulSoup) -> float | None:
    """Find 'Market Cap' in #top-ratios; value is rendered in ₹ Cr."""
    top = soup.find("ul", id="top-ratios")
    if not isinstance(top, Tag):
        return None
    for li in top.find_all("li"):
        name = li.find("span", class_="name")
        value = li.find("span", class_="value")
        if name and value and name.get_text(strip=True).lower().startswith("market cap"):
            return _parse_money(value.get_text(" ", strip=True))
    return None


def _extract_sector(soup: BeautifulSoup) -> str | None:
    """Sector text on Screener is in a header anchor labelled 'Sector'.
    Best-effort — pages without a sector simply return None."""
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if href.startswith("/company/") and "compare" in href:
            # Skip compare-page links.
            continue
        # Heuristic: the breadcrumb pattern.
    return None


def _extract_quarter_headers(table: Tag) -> list[str]:
    thead = table.find("thead")
    if not isinstance(thead, Tag):
        return []
    return [th.get_text(strip=True) for th in thead.find_all("th")]


def _extract_rows(table: Tag) -> list[tuple[str, list[str]]]:
    tbody = table.find("tbody")
    if not isinstance(tbody, Tag):
        return []
    out: list[tuple[str, list[str]]] = []
    for tr in tbody.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        label = re.sub(r"[+ \s]+$", "", cells[0])  # drop trailing '+' badge / nbsp
        out.append((label, cells[1:]))
    return out


def _row_values(rows: list[tuple[str, list[str]]], aliases: tuple[str, ...]) -> list[str]:
    """Return the values list for the first row whose label matches any alias."""
    lower_aliases = tuple(a.lower() for a in aliases)
    for label, values in rows:
        if label.lower().startswith(lower_aliases):
            return values
    return []


def _zip_newest_first(
    headers: list[str], values: list[str], *, parse_money: bool
) -> list[dict]:
    """Headers are oldest-left, newest-right. Skip the first header (label
    column) and align with values. Return up to 8 newest entries, each as
    {'quarter': 'Q3-FY26', 'value': 123.4}.
    """
    quarter_headers = headers[1:]  # skip the label-column header
    pairs: list[tuple[str, str]] = list(zip(quarter_headers, values, strict=False))
    pairs.reverse()  # newest first

    out: list[dict] = []
    for col_label, raw in pairs:
        if len(out) >= 8:
            break
        quarter = _column_to_quarter_label(col_label)
        if quarter is None:
            continue
        parsed = _parse_money(raw) if parse_money else _parse_percent(raw)
        out.append({"quarter": quarter, "value": parsed})
    return out


# Screener column headers are like 'Mar 2026', 'Jun 2025', 'Sep 2025', 'Dec 2025'.
_MONTH_TO_QUARTER: dict[str, tuple[int, int]] = {
    # month → (quarter, fy-offset where fy_year = calendar_year + offset)
    "Mar": (4, 0),    # March of calendar Y → Q4 of FY Y
    "Jun": (1, 1),    # June of Y → Q1 of FY Y+1
    "Sep": (2, 1),    # September of Y → Q2 of FY Y+1
    "Dec": (3, 1),    # December of Y → Q3 of FY Y+1
}
_HEADER_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{4})$")


def _column_to_quarter_label(header: str) -> str | None:
    """'Mar 2026' -> 'Q4-FY26'. None if unparseable."""
    m = _HEADER_RE.match(header.strip())
    if not m:
        return None
    month = m.group(1).title()
    year = int(m.group(2))
    spec = _MONTH_TO_QUARTER.get(month)
    if not spec:
        return None
    q, offset = spec
    fy = year + offset
    return f"Q{q}-FY{fy % 100:02d}"


_MONEY_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _parse_money(raw: str) -> float | None:
    """Parse a Screener cell like '627', '-12', '1,234.56', '(45.67)' → float (₹ Cr)."""
    raw = (raw or "").strip()
    if not raw or raw in ("-", "—", "–"):
        return None
    is_paren_neg = raw.startswith("(") and raw.endswith(")")
    if is_paren_neg:
        raw = raw[1:-1]
    m = _MONEY_NUMBER_RE.search(raw.replace(",", ""))
    if not m:
        return None
    try:
        val = float(m.group(0))
    except ValueError:
        return None
    return -val if is_paren_neg else val


def _parse_percent(raw: str) -> float | None:
    """Parse a percent cell like '29%' or '-2.5%' → 29.0 / -2.5."""
    raw = (raw or "").strip().rstrip("%").strip()
    if not raw or raw in ("-", "—", "–"):
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None
