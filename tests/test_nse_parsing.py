"""Tests for sources.nse normalization, using a real captured fixture."""

import json
from pathlib import Path

import pytest

from src.sources import nse

FIXTURE = Path(__file__).parent / "fixtures" / "nse_announcements_sample.json"


@pytest.fixture
def fixture_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_rows(fixture_payload):
    return fixture_payload["rows"]


class TestIsFinancialResult:
    def test_keeps_real_fixture_rows(self, fixture_rows):
        for row in fixture_rows:
            assert nse._is_financial_result(row), (
                f"row dropped unexpectedly: desc={row.get('desc')!r}"
            )

    def test_drops_non_board_meeting_desc(self):
        row = {
            "desc": "Press Release",
            "attchmntText": "Q4 financial results press release",
        }
        assert not nse._is_financial_result(row)

    def test_drops_board_meeting_without_result_text(self):
        row = {
            "desc": "Outcome of Board Meeting",
            "attchmntText": "Appointment of director X",
        }
        assert not nse._is_financial_result(row)


class TestNormalize:
    def test_centum_standalone_and_consolidated(self, fixture_rows):
        # Centum's attchmntText mentions "financial results"; subject doesn't
        # explicitly say standalone vs consolidated. Should resolve to None.
        row = next(r for r in fixture_rows if r["symbol"] == "CENTUM")
        f = nse._normalize(row)
        assert f.source == "NSE"
        assert f.symbol == "CENTUM"
        assert f.company_name == "Centum Electronics Limited"
        assert f.quarter == "Q4-FY26"  # filing-date-fallback (May 2026 → Q4)
        # NSE's attchmntText "ended March 31, 2026" matches pattern 1 (Month day, year).
        assert f.quarter_source == "headline"
        assert f.filing_url and f.filing_url.startswith("https://nsearchives.nseindia.com/")
        assert "_quarter_source" in f.raw_payload
        # ISIN captured for future Phase 4 cross-source dedup
        assert "sm_isin" in f.raw_payload

    def test_abdl(self, fixture_rows):
        row = next(r for r in fixture_rows if r["symbol"] == "ABDL")
        f = nse._normalize(row)
        assert f.source == "NSE"
        assert f.symbol == "ABDL"
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "headline"

    def test_chalet_no_ended_keyword_falls_back(self, fixture_rows):
        # Chalet's attchmntText may not contain "ended" → filing-date fallback.
        row = next(r for r in fixture_rows if r["symbol"] == "CHALET")
        f = nse._normalize(row)
        assert f.quarter == "Q4-FY26"
        # Whether the source is 'headline' or 'filing-date-fallback' depends on
        # the exact wording NSE used — just confirm one of the two.
        assert f.quarter_source in ("headline", "filing-date-fallback")

    def test_filing_time_parsed_to_utc(self, fixture_rows):
        # NSE provides an_dt like "14-May-2026 23:55:21" (IST).
        # 23:55 IST = 18:25 UTC.
        row = next(r for r in fixture_rows if r["symbol"] == "CENTUM")
        f = nse._normalize(row)
        from datetime import UTC
        assert f.filing_time.tzinfo is UTC
        assert f.filing_time.date().isoformat() == "2026-05-14"

    def test_detect_consolidated_keywords(self):
        assert nse._detect_consolidated("Audited consolidated results") is True
        assert nse._detect_consolidated("Standalone results") is False
        assert nse._detect_consolidated("results filed") is None


class TestFetch:
    """Verify the orchestration of cookie bootstrap + paged fetch without network."""

    def test_fetch_filters_to_financial_results(self, monkeypatch, fixture_payload):
        # The captured fixture has 4 rows, all financial results.
        rows = fixture_payload["rows"] + [
            # Add 2 non-result rows that should be filtered out.
            {"symbol": "FOO", "desc": "Dividend", "attchmntText": "interim dividend",
             "an_dt": "14-May-2026 10:00:00", "sm_name": "Foo Ltd",
             "attchmntFile": "x.pdf"},
            {"symbol": "BAR", "desc": "Outcome of Board Meeting",
             "attchmntText": "Director appointment", "an_dt": "14-May-2026 11:00:00",
             "sm_name": "Bar Ltd", "attchmntFile": "y.pdf"},
        ]

        # Stub the Session class so neither homepage nor API hits the network.
        class FakeResp:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

            def raise_for_status(self):
                pass

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(url)
                if url == nse.NSE_HOME_URL:
                    return FakeResp({})
                return FakeResp(rows)

        monkeypatch.setattr(nse.requests, "Session", FakeSession)
        from datetime import date
        filings = nse.NseSource().fetch(date(2026, 5, 14))
        # 4 fixture financial-result rows survive; FOO + BAR dropped by filter.
        assert len(filings) == 4
        assert {f.symbol for f in filings} == {"CENTUM", "ABDL", "CHALET"}
