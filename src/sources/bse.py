"""BSE corporate-announcement source — Phase 1.

Fetches today's result-category filings via the public BSE API, filters to
quarterly results, normalizes to BseFiling records. No PDF parsing happens here;
Phase 3 (Gemini) owns financial-number extraction.

Real BSE request shape (verified against the live UI 2026-05-26 against
www.bseindia.com/corporates/ann.html):
    GET /BseIndiaAPI/api/AnnSubCategoryGetData/w
        ?pageno=<N>
        &strCat=Result
        &strPrevDate=<YYYYMMDD>
        &strToDate=<YYYYMMDD>
        &strScrip=
        &strSearch=P          ← REQUIRED sentinel; without it the API returns {}
        &strType=C
        &subcategory=-1       ← REQUIRED; without it the API returns {}
Response: {"Table": [...up to 50 rows...], "Table1": [{"ROWCNT": <total>}]}.
We paginate until we have all ROWCNT rows or hit the safety cap.

Per-row field mapping:
    SCRIP_CD              → symbol (stringified)
    SLONGNAME             → company_name
    NEWSSUB               → subject line for quarter-derivation + is-consolidated
                            detection. HEADLINE is often a useless "Pls refer
                            enclosed" — do NOT rely on it.
    News_submission_dt    → filing_time (IST, no tz suffix). Mixed-case in BSE's
                            payload; not the upper-snake-case we guessed first.
    ATTACHMENTNAME        → filing_url via BSE_PDF_URL_TEMPLATE (HEAD-checked)

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
from datetime import date
from typing import Any

import requests

from src.sources.base import Filing
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

# Second-level filter: strCat=Result includes things like "Press Release",
# "Voting Results", "Audio Recording of Conference Call" alongside the actual
# numbers. Keep only rows whose SUBCATNAME marks them as financial results.
# (A regex on the subject was tried first but failed on real data — many
# legitimate quarterly filings have subjects like "Financial Results For
# March 31, 2026" with no word "quarter" anywhere.)
_FINANCIAL_RESULT_SUBCAT_RE = re.compile(r"financial\s+result", re.IGNORECASE)


def _is_financial_result(row: dict[str, Any]) -> bool:
    return bool(_FINANCIAL_RESULT_SUBCAT_RE.search(row.get("SUBCATNAME") or ""))


# Phase 2: source-agnostic dataclass lives in base.py. Re-export so existing
# `from src.sources.bse import BseFiling` imports keep working through Phase 2
# transition; new code should import Filing directly from base.
BseFiling = Filing


_PAGE_SIZE = 50      # BSE returns up to 50 rows per page
_MAX_PAGES = 50      # safety cap (50 × 50 = 2,500 rows/day, well above any realistic peak)


def _build_params(yyyymmdd: str, pageno: int) -> dict[str, Any]:
    return {
        "pageno": pageno,
        "strCat": "Result",
        "strPrevDate": yyyymmdd,
        "strToDate": yyyymmdd,
        "strScrip": "",
        "strSearch": "P",      # REQUIRED sentinel — BSE's own UI sends this; empty value yields {}
        "strType": "C",
        "subcategory": "-1",   # REQUIRED — "all subcategories"; without it the API yields {}
    }


def _fetch_page(yyyymmdd: str, pageno: int) -> dict[str, Any]:
    params = _build_params(yyyymmdd, pageno)

    def _do_get() -> requests.Response:
        return requests.get(BSE_API_URL, params=params, headers=BSE_HEADERS, timeout=15)

    resp = with_retries(_do_get)
    resp.raise_for_status()
    return resp.json() or {}


def fetch_today_results(target_date: date | None = None) -> list[Filing]:
    """Fetch result-category announcements for `target_date` (IST). Default: today.

    Paginates over BSE's 50-rows-per-page API until all ROWCNT rows are collected
    (or the safety cap is hit). Filters to subjects containing a quarter hint
    before normalizing.
    """
    if target_date is None:
        target_date = time_utils.today_ist()
    yyyymmdd = target_date.strftime("%Y%m%d")

    raw_rows: list[dict[str, Any]] = []
    total: int | None = None
    for pageno in range(1, _MAX_PAGES + 1):
        payload = _fetch_page(yyyymmdd, pageno)
        rows = payload.get("Table") or []
        if total is None:
            table1 = payload.get("Table1") or []
            if table1 and "ROWCNT" in table1[0]:
                total = int(table1[0]["ROWCNT"])
        raw_rows.extend(rows)
        if not rows:
            break
        if total is not None and len(raw_rows) >= total:
            break
    log.info(
        f"BSE returned {len(raw_rows)} result-category rows for {yyyymmdd} "
        f"(ROWCNT={total}, pages_fetched={pageno})"
    )

    filings: list[Filing] = []
    dropped_subcat = 0
    for row in raw_rows:
        if not _is_financial_result(row):
            dropped_subcat += 1
            continue
        try:
            filings.append(_normalize(row, _row_subject(row)))
        except Exception as e:
            log.warning(f"Failed to normalize BSE row scrip={row.get('SCRIP_CD')}: {e}")
            continue
    log.info(
        f"BSE: {len(filings)} financial-result filings kept, "
        f"{dropped_subcat} dropped by SUBCATNAME filter"
    )
    return filings


def _row_subject(row: dict[str, Any]) -> str:
    """Compose the full subject from NEWSSUB + HEADLINE + MORE.

    NEWSSUB is the authoritative subject line (HEADLINE is often a useless
    "Pls refer enclosed"). We concatenate all three to maximize the chance the
    quarter-hint regex and the date-extractor find what they need.
    """
    parts = (row.get("NEWSSUB") or "", row.get("HEADLINE") or "", row.get("MORE") or "")
    return " ".join(p for p in parts if p).strip()


def _normalize(row: dict[str, Any], subject: str) -> Filing:
    scrip_code = str(row.get("SCRIP_CD") or "").strip()
    company_name = (row.get("SLONGNAME") or row.get("NEWSSUB") or "").strip()

    submission = (
        row.get("News_submission_dt")
        or row.get("DT_TM")
        or row.get("NEWS_DT")
        or ""
    )
    filing_dt = time_utils.parse_bse_timestamp(submission)

    quarter, source = time_utils.derive_quarter(filing_dt, subject)

    attachment = (row.get("ATTACHMENTNAME") or "").strip()
    if attachment:
        candidate = BSE_PDF_URL_TEMPLATE.format(name=attachment)
        filing_url = _verify_pdf_url(candidate)
    else:
        filing_url = None

    is_consolidated = _detect_consolidated(subject)

    payload = dict(row)
    # Breadcrumb so post-hoc audits can tell whether the quarter came from regex or fallback.
    payload["_quarter_source"] = source
    return Filing(
        source="BSE",
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


class BseSource:
    """Phase 2 Protocol adapter — thin class wrapper around fetch_today_results."""

    name = "BSE"

    def fetch(self, target_date: date | None = None) -> list[Filing]:
        return fetch_today_results(target_date)
