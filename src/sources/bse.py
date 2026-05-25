"""BSE corporate-announcement source — Phase 1.

Fetches today's result-category filings via the public BSE API, filters to
quarterly results, normalizes to BseFiling records. No PDF parsing happens here;
Phase 3 (Gemini) owns financial-number extraction.

Schema mapping (per Phase 1 Q1 decision):
    filings has UNIQUE (symbol, quarter) only — no filing_type column.
    Standalone vs consolidated for the same (symbol, quarter) collapse into ONE
    row; first one to arrive wins. is_consolidated is set from whichever arrives
    first. Revisit only if real data shows this losing important information.

Symbol field (per Phase 1 Q2 decision):
    BSE returns scrip codes (e.g. 500180), not NSE tickers. We store the scrip
    code in `symbol` for now.
    # TODO: phase 2 — cross-reference with NSE source to enrich to NSE symbol.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests

from src.utils import time_utils
from src.utils.logging import get_logger
from src.utils.retry import with_retries

log = get_logger(__name__)

BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_PDF_URL_TEMPLATE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"

BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Belt-and-braces filter after `strCat=Result`: many entries in "Result" are
# annual results or restatements; we restrict to those whose subject mentions a
# quarter explicitly.
_QUARTER_HINT_RE = re.compile(r"\b(quarter|q[1-4])\b", re.IGNORECASE)


@dataclass
class BseFiling:
    symbol: str
    company_name: str
    quarter: str
    quarter_source: str
    filing_time: datetime
    filing_url: str | None
    is_consolidated: bool | None
    raw_payload: dict[str, Any]


def fetch_today_results(target_date: date | None = None) -> list[BseFiling]:
    """Fetch result-category announcements for `target_date` (IST). Default: today."""
    if target_date is None:
        target_date = time_utils.today_ist()
    yyyymmdd = target_date.strftime("%Y%m%d")
    params = {
        "pageno": 1,
        "strCat": "Result",
        "strPrevDate": yyyymmdd,
        "strToDate": yyyymmdd,
        "strScrip": "",
        "strSearch": "",
        "strType": "C",
    }

    def _do_get() -> requests.Response:
        return requests.get(BSE_API_URL, params=params, headers=BSE_HEADERS, timeout=15)

    resp = with_retries(_do_get)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("Table") or []
    log.info(f"BSE returned {len(rows)} result-category announcements for {yyyymmdd}")

    filings: list[BseFiling] = []
    for row in rows:
        headline = ((row.get("HEADLINE") or "") + " " + (row.get("MORE") or "")).strip()
        if not _QUARTER_HINT_RE.search(headline):
            continue
        try:
            filing = _normalize(row, headline)
        except Exception as e:
            log.warning(f"Failed to normalize BSE row scrip={row.get('SCRIP_CD')}: {e}")
            continue
        filings.append(filing)
    log.info(f"BSE: {len(filings)} quarterly filings after subject-line filter")
    return filings


def _normalize(row: dict[str, Any], headline: str) -> BseFiling:
    scrip_code = str(row.get("SCRIP_CD") or "").strip()
    company_name = (row.get("SLONGNAME") or row.get("HEADLINE") or "").strip()

    submission = row.get("NEWS_SUBMISSION_DT") or row.get("DT_TM") or ""
    filing_dt = time_utils.parse_bse_timestamp(submission)

    quarter, source = time_utils.derive_quarter(filing_dt, headline)

    attachment = (row.get("ATTACHMENTNAME") or "").strip()
    if attachment:
        candidate = BSE_PDF_URL_TEMPLATE.format(name=attachment)
        filing_url = _verify_pdf_url(candidate)
    else:
        filing_url = None

    is_consolidated = _detect_consolidated(headline)

    payload = dict(row)
    # Breadcrumb so post-hoc audits can tell whether the quarter came from regex or fallback.
    payload["_quarter_source"] = source
    return BseFiling(
        symbol=scrip_code,
        company_name=company_name,
        quarter=quarter,
        quarter_source=source,
        filing_time=filing_dt,
        filing_url=filing_url,
        is_consolidated=is_consolidated,
        raw_payload=payload,
    )


def _verify_pdf_url(url: str) -> str | None:
    """HEAD-check the BSE PDF URL. On 404 or network error, return None so the
    alert still goes out without a link (per formatter fallback)."""
    try:
        resp = requests.head(url, headers=BSE_HEADERS, timeout=10, allow_redirects=True)
    except requests.RequestException as e:
        log.warning(f"PDF HEAD failed for {url}: {e}; storing null filing_url")
        return None
    if 200 <= resp.status_code < 300:
        return url
    log.warning(f"PDF HEAD returned {resp.status_code} for {url}; storing null filing_url")
    return None


def _detect_consolidated(headline: str) -> bool | None:
    h = headline.lower()
    if "consolidated" in h:
        return True
    if "standalone" in h:
        return False
    return None
