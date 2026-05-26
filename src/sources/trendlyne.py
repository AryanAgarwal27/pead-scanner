"""Trendlyne rapid-results scraper — Phase 2 last-resort fallback.

When BOTH NSE and BSE error in the same poll run, the detector calls
TrendlyneSource as a continuity signal.

Endpoint: https://trendlyne.com/rapid-results/all/ (HTML, no login for the
basic listing). The page renders the latest ~10 result announcements as
`<div class="panel-post" data-postid="...">` blocks, each containing a
`<script type="application/ld+json">` block with structured NewsArticle
metadata (`headline`, `url`, `datePublished`).

Captured 2026-05-26 — headlines follow the pattern:
    "Q4FY26 & FY26 Result Announced for Pine Labs Ltd."

We parse:
- quarter from "Q{N}FY{YY}" in the headline (no derive_quarter fallback needed)
- company name from "Result Announced for {Name}"
- filing_time from `datePublished` (ISO-8601, UTC)
- symbol: kebab-case slug derived from the company name. Trendlyne does not
  expose NSE/BSE tickers in the listing; we prefix "TL-" to avoid collision
  with primary-source symbols in the filings table.

Brittleness: HTML scrapes break when sites redesign. ~12-month expected
stability. Heartbeat (jobs/heartbeat.py) will flag failure within 24h.
"""

import json
import re
from datetime import date, datetime
from typing import Any

import requests

from src.sources.base import Filing
from src.utils import time_utils
from src.utils.logging import get_logger
from src.utils.retry import with_retries

log = get_logger(__name__)

TRENDLYNE_URL = "https://trendlyne.com/rapid-results/all/"

TRENDLYNE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# Capture data-postid and the JSON-LD body of each panel-post block.
_PANEL_RE = re.compile(
    r'<div\s+class="[^"]*panel-post[^"]*"[^>]*data-postid="(\d+)"[^>]*>'
    r'.*?<script\s+type="application/ld\+json">\s*(\{.*?\})\s*</script>',
    re.DOTALL,
)

_QFY_RE = re.compile(r"Q([1-4])FY(\d{2})", re.IGNORECASE)
_HEADLINE_COMPANY_RE = re.compile(
    r"Result Announced for\s+(?P<name>.+?)\.?\s*$", re.IGNORECASE
)


class TrendlyneSource:
    name = "TRENDLYNE"

    def fetch(self, target_date: date | None = None) -> list[Filing]:
        if target_date is None:
            target_date = time_utils.today_ist()

        def _do_get() -> requests.Response:
            return requests.get(TRENDLYNE_URL, headers=TRENDLYNE_HEADERS, timeout=15)

        resp = with_retries(_do_get)
        resp.raise_for_status()
        html = resp.text

        filings: list[Filing] = []
        panels = _PANEL_RE.findall(html)
        log.info(f"Trendlyne returned {len(panels)} panel-post blocks")
        for postid, jsonld_str in panels:
            try:
                meta = json.loads(jsonld_str)
            except json.JSONDecodeError as e:
                log.warning(f"Trendlyne JSON-LD parse failed post={postid}: {e}")
                continue
            try:
                filing = _normalize(postid, meta)
            except Exception as e:
                log.warning(f"Trendlyne normalize failed post={postid}: {e}")
                continue
            if filing is None:
                continue
            # The listing is global (latest N); filter to the requested IST date.
            if filing.filing_time.astimezone(time_utils.IST).date() != target_date:
                continue
            filings.append(filing)
        log.info(
            f"Trendlyne: {len(filings)} result posts for {target_date.isoformat()}"
        )
        return filings


def _normalize(postid: str, meta: dict[str, Any]) -> Filing | None:
    headline = (meta.get("headline") or "").strip()
    if not headline:
        return None

    q_match = _QFY_RE.search(headline)
    if not q_match:
        # Without a quarter marker we can't trust this is a quarterly result post.
        return None
    quarter = f"Q{q_match.group(1)}-FY{q_match.group(2)}"

    company_match = _HEADLINE_COMPANY_RE.search(headline)
    if not company_match:
        return None
    company_name = company_match.group("name").strip().rstrip(".").strip()

    date_str = (meta.get("datePublished") or "").strip()
    if not date_str:
        return None
    filing_dt = _parse_iso(date_str)

    slug = _slugify(company_name)
    # Prefix with TL- so trendlyne-sourced rows can't accidentally collide with
    # NSE tickers / BSE scrip codes in the filings table.
    symbol = f"TL-{slug}"

    payload = {"postid": postid, "headline": headline, "meta": meta, "_quarter_source": "headline"}
    return Filing(
        source="TRENDLYNE",
        symbol=symbol,
        company_name=company_name,
        quarter=quarter,
        quarter_source="headline",
        filing_time=filing_dt,
        filing_url=(meta.get("url") or "").strip() or None,
        is_consolidated=None,  # not exposed in the listing
        raw_payload=payload,
    )


def _parse_iso(s: str) -> datetime:
    """Parse Trendlyne's datePublished. Format is ISO-8601 with offset, e.g.
    '2026-05-25T12:44:37+00:00'."""
    return datetime.fromisoformat(s)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")
