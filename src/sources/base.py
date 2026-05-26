"""Source-agnostic Filing dataclass + FilingsSource Protocol.

All source adapters (BSE, NSE, Trendlyne) produce `Filing` instances. The
detector consumes `FilingsSource` implementations interchangeably.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol


@dataclass
class Filing:
    source: str                 # "NSE" | "BSE" | "TRENDLYNE"
    symbol: str                 # native id per source (NSE ticker / BSE scrip / TL slug)
    company_name: str
    quarter: str                # e.g. "Q3-FY26"
    quarter_source: str         # "headline" | "filing-date-fallback"
    filing_time: datetime       # tz-aware UTC
    filing_url: str | None
    is_consolidated: bool | None
    raw_payload: dict[str, Any]


class FilingsSource(Protocol):
    """Each source implementation supplies `name` and `fetch(target_date)`.

    Phase 2 keeps the interface synchronous and per-source self-contained — the
    detector composes them. Implementations SHOULD raise on network/protocol
    errors; the detector catches and routes to source_health + rate_limit.
    """

    name: str

    def fetch(self, target_date: date | None = None) -> list[Filing]: ...
