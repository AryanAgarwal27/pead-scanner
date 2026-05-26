"""IST helpers and Indian-fiscal-quarter derivation.

Quarter labelling convention (Indian FY ends March):
  Q1 = Apr–Jun, Q2 = Jul–Sep, Q3 = Oct–Dec, Q4 = Jan–Mar
  FY26 ends Mar 2026, so:
    Q1-FY26 ends 30-Jun-2025
    Q2-FY26 ends 30-Sep-2025
    Q3-FY26 ends 31-Dec-2025
    Q4-FY26 ends 31-Mar-2026
"""

import re
from datetime import UTC, date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def format_ist(dt: datetime) -> str:
    """Render as '25-May-2026, 14:23 IST'."""
    return to_ist(dt).strftime("%d-%b-%Y, %H:%M IST")


def parse_bse_timestamp(s: str) -> datetime:
    """Parse a BSE-style timestamp string (IST, no tz info) to a tz-aware UTC datetime."""
    s = (s or "").strip()
    # ISO with T-separator covers News_submission_dt and DT_TM (with or without millis).
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ):
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=IST).astimezone(UTC)
    raise ValueError(f"unrecognized BSE timestamp: {s!r}")


_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Order matters: try named-month patterns before numeric, and "day before month"
# before "month before day" to avoid mis-binding on ambiguous formats.
_HEADLINE_DATE_PATTERNS = (
    re.compile(
        r"ended?\s+(?:on\s+)?(?P<month_name>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})[,\s]+(?P<year>\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"ended?\s+(?:on\s+)?(?P<day>\d{1,2})\s+(?P<month_name>[A-Za-z]{3,9})[,\s]+(?P<year>\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"ended?\s+(?:on\s+)?(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2,4})",
        re.IGNORECASE,
    ),
)


def _extract_quarter_end_from_headline(headline: str) -> tuple[int, int] | None:
    for pattern in _HEADLINE_DATE_PATTERNS:
        m = pattern.search(headline)
        if not m:
            continue
        groups = m.groupdict()
        try:
            year = int(groups["year"])
            if year < 100:
                year += 2000
            month_name = groups.get("month_name")
            month = _MONTH_NAMES[month_name.lower()] if month_name else int(groups["month"])
        except (KeyError, ValueError):
            continue
        if month not in (3, 6, 9, 12):
            # Not a quarter-end date; the headline may have mentioned a different date.
            continue
        return (year, month)
    return None


def _quarter_label(end_year: int, end_month: int) -> str:
    if end_month == 3:
        q, fy = 4, end_year
    elif end_month == 6:
        q, fy = 1, end_year + 1
    elif end_month == 9:
        q, fy = 2, end_year + 1
    elif end_month == 12:
        q, fy = 3, end_year + 1
    else:
        raise ValueError(f"not a quarter-end month: {end_month}")
    return f"Q{q}-FY{fy % 100:02d}"


def _most_recent_quarter_end(d: date) -> tuple[int, int]:
    """Return (year, month) of the most recently *completed* Indian fiscal quarter.

    Reasoning: result-filing windows in India give companies 45 days after quarter-end,
    so the quarter being reported is almost always the most recently completed one.
    """
    m = d.month
    if 4 <= m <= 6:
        return (d.year, 3)        # Q4 of FY{year} ended Mar
    if 7 <= m <= 9:
        return (d.year, 6)        # Q1 ended Jun
    if 10 <= m <= 12:
        return (d.year, 9)        # Q2 ended Sep
    return (d.year - 1, 12)        # Jan–Mar → Q3 ended Dec of prev year


def derive_quarter(filing_dt: datetime, headline: str | None) -> tuple[str, str]:
    """Return (quarter_label, source). source ∈ {'headline', 'filing-date-fallback'}.

    Headline regex first; on miss, derive from filing date assuming "most recent
    completed quarter". Per Phase 1 Q5, do NOT drop on miss — accept fallback.
    """
    extracted = _extract_quarter_end_from_headline(headline or "")
    if extracted:
        year, month = extracted
        return (_quarter_label(year, month), "headline")
    ist_date = to_ist(filing_dt).date()
    year, month = _most_recent_quarter_end(ist_date)
    return (_quarter_label(year, month), "filing-date-fallback")
