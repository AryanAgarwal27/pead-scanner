"""Tests for sources.bse normalization, using a real BSE response fixture.

Fixture was captured 2026-05-26 from the live BSE API for IST date 2026-05-14
(4 representative rows from a 142-row response).
"""

import json
from pathlib import Path

import pytest

from src.sources import bse

FIXTURE = Path(__file__).parent / "fixtures" / "bse_announcements_sample.json"


@pytest.fixture
def fixture_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_rows(fixture_payload):
    return fixture_payload["Table"]


@pytest.fixture(autouse=True)
def stub_pdf_head(monkeypatch):
    """Never hit the network in unit tests — pretend every PDF URL is reachable."""
    monkeypatch.setattr(bse, "_verify_pdf_url", lambda url: url)


class TestRowSubject:
    def test_prefers_newssub(self, fixture_rows):
        # Row 0: HEADLINE is the useless "Pls refer enclosed"; NEWSSUB has the real subject.
        row = fixture_rows[0]
        subject = bse._row_subject(row)
        assert "Centum Electronics Ltd" in subject
        assert "Fourth Quarter And Year Ended" in subject
        # HEADLINE is concatenated but doesn't drown out NEWSSUB
        assert "Pls refer enclosed" in subject


class TestNormalize:
    def test_centum_falls_back_for_ordinal_suffix(self, fixture_rows):
        # "31St March, 2026" — the 'St' suffix breaks all three regex patterns
        # (none expect day-then-suffix-then-month-name), so we fall back.
        # Filing date is 2026-05-14 IST → most-recent-completed = Mar 2026 → Q4-FY26.
        row = fixture_rows[0]
        f = bse._normalize(row, bse._row_subject(row))
        assert f.symbol == "517544"
        assert f.company_name == "Centum Electronics Ltd"
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "filing-date-fallback"
        assert f.is_consolidated is True
        assert f.filing_url.endswith("e334a390-486b-41a2-9c14-6d777137bd1d.pdf")
        # Timestamp: News_submission_dt "2026-05-14T23:50:28" IST → UTC 18:20.
        assert f.filing_time.hour == 18 and f.filing_time.minute == 20

    def test_allied_blenders_headline_parse(self, fixture_rows):
        # NEWSSUB ends with "Ended March 31, 2026." — pattern 1 (ended <Mon> <day>, <year>) hits.
        row = fixture_rows[1]
        f = bse._normalize(row, bse._row_subject(row))
        assert f.symbol == "544203"
        assert f.company_name == "Allied Blenders and Distillers Ltd"
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "headline"
        assert f.is_consolidated is True

    def test_chalet_no_ended_keyword_falls_back(self, fixture_rows):
        # "Financial Results For March 31, 2026" — no "ended" word, regex won't match.
        # Falls back to filing-date logic → Q4-FY26.
        row = fixture_rows[2]
        f = bse._normalize(row, bse._row_subject(row))
        assert f.symbol == "542399"
        assert f.company_name == "Chalet Hotels Ltd"
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "filing-date-fallback"
        # No standalone/consolidated marker in subject — leave None.
        assert f.is_consolidated is None

    def test_menon_numeric_dot_date(self, fixture_rows):
        # "Ended 31.03.2026" — pattern 3 (numeric d.m.y) hits.
        row = fixture_rows[3]
        f = bse._normalize(row, bse._row_subject(row))
        assert f.symbol == "523828"
        assert "Menon Bearings" in f.company_name
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "headline"

    def test_raw_payload_breadcrumb(self, fixture_rows):
        f = bse._normalize(fixture_rows[1], bse._row_subject(fixture_rows[1]))
        assert f.raw_payload["_quarter_source"] == "headline"


class TestFinancialResultFilter:
    def test_keeps_financial_results_subcategory(self, fixture_rows):
        # All 4 real fixture rows are SUBCATNAME="Financial Results" — must pass.
        for row in fixture_rows:
            assert bse._is_financial_result(row), (
                f"row dropped unexpectedly: subcat={row.get('SUBCATNAME')!r}"
            )

    def test_drops_other_subcategories(self):
        cases = [
            {"SUBCATNAME": "Press Release"},
            {"SUBCATNAME": "Voting Results"},
            {"SUBCATNAME": "Audio Recording of Conference Call"},
            {"SUBCATNAME": ""},
            {"SUBCATNAME": None},
            {},
        ]
        for row in cases:
            assert not bse._is_financial_result(row)


class TestFetchTodayResultsPagination:
    """Verify the pagination loop without hitting the network."""

    def test_stops_when_rowcnt_reached(self, monkeypatch):
        # Simulate 3 pages of 50/50/42 = 142 total.
        def _row(i):
            return {"NEWSSUB": "Quarter results", "SCRIP_CD": i, "SLONGNAME": "X",
                    "ATTACHMENTNAME": "", "News_submission_dt": "2026-05-14T10:00:00",
                    "SUBCATNAME": "Financial Results"}
        pages = [
            {"Table": [_row(i) for i in range(50)], "Table1": [{"ROWCNT": 142}]},
            {"Table": [_row(50 + i) for i in range(50)], "Table1": [{"ROWCNT": 142}]},
            {"Table": [_row(100 + i) for i in range(42)], "Table1": [{"ROWCNT": 142}]},
        ]
        call_count = {"n": 0}

        def fake_fetch(yyyymmdd, pageno):
            call_count["n"] += 1
            return pages[pageno - 1]

        monkeypatch.setattr(bse, "_fetch_page", fake_fetch)
        from datetime import date
        filings = bse.fetch_today_results(date(2026, 5, 14))
        assert len(filings) == 142
        assert call_count["n"] == 3  # stops after page 3 since 50+50+42 == 142

    def test_stops_on_empty_page(self, monkeypatch):
        # If ROWCNT is missing, stop the first time Table is empty.
        page1 = {"Table": [{"NEWSSUB": "Quarter results", "SCRIP_CD": 1, "SLONGNAME": "X",
                            "ATTACHMENTNAME": "", "News_submission_dt": "2026-05-14T10:00:00",
                            "SUBCATNAME": "Financial Results"}],
                 "Table1": []}
        page2 = {"Table": [], "Table1": []}

        def fake_fetch(yyyymmdd, pageno):
            return page1 if pageno == 1 else page2

        monkeypatch.setattr(bse, "_fetch_page", fake_fetch)
        from datetime import date
        filings = bse.fetch_today_results(date(2026, 5, 14))
        assert len(filings) == 1


class TestBuildParams:
    """Guard against future regressions of the magic-sentinel params."""

    def test_includes_subcategory_and_strsearch_p(self):
        p = bse._build_params("20260514", 1)
        assert p["subcategory"] == "-1"
        assert p["strSearch"] == "P"
        assert p["strCat"] == "Result"
        assert p["pageno"] == 1
        assert p["strPrevDate"] == "20260514"
        assert p["strToDate"] == "20260514"
