"""NSE corporate-announcements source — Phase 2.

Endpoint shape (verified against live nseindia.com 2026-05-26 for IST date
2026-05-14, which returned 94 result filings of 1,050 total announcements):

    Step 1 (cookie bootstrap): GET https://www.nseindia.com/
    Step 2: GET https://www.nseindia.com/api/corporate-announcements
        ?index=equities
        &from_date=DD-MM-YYYY
        &to_date=DD-MM-YYYY

Response: top-level JSON array, each row keyed by `symbol`, `desc`,
`sm_name`, `sm_isin`, `an_dt`, `attchmntFile`, `attchmntText`, etc.

Filter to quarterly results:
    desc == "Outcome of Board Meeting"      ← NSE's regulatory wrapper category
    AND attchmntText contains "financial result"  (case-insensitive)

NSE classifies quarterly results under "Outcome of Board Meeting" because the
board meets, declares results, and files. The dedicated
`/api/corporates-financial-results` endpoint is the XBRL structured-data submission
and lags announcements by days — not useful for Day-0 alerts.

Per Phase 2 plan: NSE's ISIN (`sm_isin`) is captured in raw_payload for future
cross-source dedup work (Phase 4+). Symbol stored is the NSE ticker as-is.
"""

import re
from datetime import date
from typing import Any

import requests

from src.sources.base import Filing, FilingsSource
from src.utils import time_utils
from src.utils.logging import get_logger
from src.utils.retry import with_retries

log = get_logger(__name__)

NSE_HOME_URL = "https://www.nseindia.com/"
NSE_API_URL = "https://www.nseindia.com/api/corporate-announcements"

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    "Connection": "keep-alive",
}

_FIN_RESULT_RE = re.compile(r"financial\s+result", re.IGNORECASE)


def _is_financial_result(row: dict[str, Any]) -> bool:
    desc = (row.get("desc") or "").strip()
    if desc != "Outcome of Board Meeting":
        return False
    return bool(_FIN_RESULT_RE.search(row.get("attchmntText") or ""))


class NseSource:
    name = "NSE"

    def fetch(self, target_date: date | None = None) -> list[Filing]:
        if target_date is None:
            target_date = time_utils.today_ist()
        ddmmyyyy = target_date.strftime("%d-%m-%Y")

        sess = requests.Session()
        # Step 1: bootstrap cookies (nsit, _abck, etc.) from homepage.
        sess.get(NSE_HOME_URL, headers=NSE_HEADERS, timeout=15)

        # Step 2: pull announcements.
        def _do_get() -> requests.Response:
            return sess.get(
                NSE_API_URL,
                params={"index": "equities", "from_date": ddmmyyyy, "to_date": ddmmyyyy},
                headers=NSE_HEADERS,
                timeout=15,
            )

        resp = with_retries(_do_get)
        resp.raise_for_status()
        rows = resp.json() or []
        log.info(f"NSE returned {len(rows)} total announcements for {ddmmyyyy}")

        filings: list[Filing] = []
        dropped = 0
        for row in rows:
            if not _is_financial_result(row):
                dropped += 1
                continue
            try:
                filings.append(_normalize(row))
            except Exception as e:
                log.warning(f"Failed to normalize NSE row symbol={row.get('symbol')}: {e}")
                continue
        log.info(f"NSE: {len(filings)} financial-result filings kept, {dropped} dropped by filter")
        return filings


def _normalize(row: dict[str, Any]) -> Filing:
    symbol = (row.get("symbol") or "").strip()
    company_name = (row.get("sm_name") or "").strip()

    # NSE provides both an_dt ("14-May-2026 23:55:21") and sort_date
    # ("2026-05-14 23:55:21") — both IST, no tz suffix.
    timestamp = (row.get("an_dt") or row.get("sort_date") or "").strip()
    filing_dt = time_utils.parse_bse_timestamp(timestamp)

    subject = (
        (row.get("attchmntText") or "") + " " + (row.get("desc") or "")
    ).strip()
    quarter, quarter_src = time_utils.derive_quarter(filing_dt, subject)

    pdf_url = (row.get("attchmntFile") or "").strip() or None

    is_consolidated = _detect_consolidated(subject)

    payload = dict(row)
    payload["_quarter_source"] = quarter_src
    return Filing(
        source="NSE",
        symbol=symbol,
        company_name=company_name,
        quarter=quarter,
        quarter_source=quarter_src,
        filing_time=filing_dt,
        filing_url=pdf_url,
        is_consolidated=is_consolidated,
        raw_payload=payload,
    )


def _detect_consolidated(subject: str) -> bool | None:
    s = subject.lower()
    if "consolidated" in s:
        return True
    if "standalone" in s:
        return False
    return None


# Module-level singleton for the detector — instantiation is free.
_INSTANCE: FilingsSource | None = None


def get_source() -> FilingsSource:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = NseSource()
    return _INSTANCE
