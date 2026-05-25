"""Tests for sources.bse normalization, using the synthesized fixture."""

import json
from pathlib import Path

import pytest

from src.sources import bse

FIXTURE = Path(__file__).parent / "fixtures" / "bse_announcements_sample.json"


@pytest.fixture
def fixture_rows():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["Table"]


@pytest.fixture(autouse=True)
def stub_pdf_head(monkeypatch):
    """Never hit the network in unit tests — pretend every PDF URL is reachable."""
    monkeypatch.setattr(bse, "_verify_pdf_url", lambda url: url)


class TestNormalize:
    def test_hdfc_standalone_q3(self, fixture_rows):
        row = fixture_rows[0]
        headline = (row["HEADLINE"] or "") + " " + (row["MORE"] or "")
        f = bse._normalize(row, headline)
        assert f.symbol == "500180"
        assert f.company_name == "HDFC Bank Ltd"
        assert f.quarter == "Q3-FY26"
        assert f.quarter_source == "headline"
        assert f.is_consolidated is False
        assert f.filing_url and f.filing_url.endswith("abc123-def-456.pdf")
        assert f.raw_payload["_quarter_source"] == "headline"

    def test_ril_consolidated_q4(self, fixture_rows):
        row = fixture_rows[1]
        headline = (row["HEADLINE"] or "") + " " + (row["MORE"] or "")
        f = bse._normalize(row, headline)
        assert f.symbol == "500325"
        assert f.quarter == "Q4-FY26"
        assert f.is_consolidated is True

    def test_tcs_numeric_date(self, fixture_rows):
        row = fixture_rows[2]
        headline = (row["HEADLINE"] or "") + " " + (row["MORE"] or "")
        f = bse._normalize(row, headline)
        assert f.quarter == "Q3-FY26"
        assert f.quarter_source == "headline"

    def test_annual_report_with_hyphenated_month_falls_back(self, fixture_rows):
        # Headline "year ended 31-Mar-2025" uses hyphen-separated name-month format
        # not covered by our 3 simple patterns (would need a 4th regex). Per Phase 1
        # decision (Q5: ONE attempt then fallback), we accept the fallback.
        # Fallback derives quarter from filing date 2026-05-10 → Q4-FY26.
        row = fixture_rows[3]
        headline = (row["HEADLINE"] or "") + " " + (row["MORE"] or "")
        f = bse._normalize(row, headline)
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "filing-date-fallback"


class TestQuarterHintFilter:
    def test_drops_rows_without_quarter_keyword(self):
        rows = [
            {
                "SCRIP_CD": 1,
                "SLONGNAME": "Foo",
                "HEADLINE": "Notice of dividend payment",
                "MORE": "",
                "ATTACHMENTNAME": "",
                "NEWS_SUBMISSION_DT": "2026-05-01 10:00:00.000",
            }
        ]
        # _QUARTER_HINT_RE drops headline lacking 'quarter' or 'q[1-4]'
        assert bse._QUARTER_HINT_RE.search(rows[0]["HEADLINE"]) is None
