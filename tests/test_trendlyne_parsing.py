"""Tests for sources.trendlyne — uses a real captured HTML fixture."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from src.sources import trendlyne

FIXTURE = Path(__file__).parent / "fixtures" / "trendlyne_rapid_results.html"


@pytest.fixture
def fixture_html():
    return FIXTURE.read_text(encoding="utf-8")


class TestPanelRegex:
    def test_extracts_post_blocks(self, fixture_html):
        panels = trendlyne._PANEL_RE.findall(fixture_html)
        # Captured 3 panels in the fixture.
        assert len(panels) >= 1
        for postid, jsonld_str in panels:
            assert postid.isdigit()
            assert jsonld_str.lstrip().startswith("{")


class TestSlugify:
    def test_strips_punctuation(self):
        assert trendlyne._slugify("Pine Labs Ltd.") == "pine-labs-ltd"
        assert trendlyne._slugify("HDFC Bank Ltd") == "hdfc-bank-ltd"
        assert trendlyne._slugify("Suprajit Engineering Ltd.") == "suprajit-engineering-ltd"

    def test_handles_multiple_spaces(self):
        assert trendlyne._slugify("  Foo   Bar  ") == "foo-bar"


class TestNormalize:
    def test_parses_quarter_and_company(self):
        meta = {
            "headline": "Q4FY26 & FY26 Result Announced for Pine Labs Ltd.",
            "url": "https://trendlyne.com/posts/5657956/q4fy26-fy26-result-announced-for-pine-labs-ltd",
            "datePublished": "2026-05-25T12:44:37+00:00",
        }
        f = trendlyne._normalize("5657956", meta)
        assert f is not None
        assert f.source == "TRENDLYNE"
        assert f.symbol == "TL-pine-labs-ltd"
        assert f.company_name == "Pine Labs Ltd"
        assert f.quarter == "Q4-FY26"
        assert f.quarter_source == "headline"
        assert f.filing_url == meta["url"]
        assert f.filing_time == datetime(2026, 5, 25, 12, 44, 37, tzinfo=UTC)
        assert f.is_consolidated is None
        assert f.raw_payload["postid"] == "5657956"

    def test_returns_none_without_quarter_marker(self):
        meta = {
            "headline": "Result Announced for Foo Ltd.",  # no Q?FY?? token
            "url": "https://trendlyne.com/posts/1/foo",
            "datePublished": "2026-05-25T12:00:00+00:00",
        }
        assert trendlyne._normalize("1", meta) is None

    def test_returns_none_without_company(self):
        meta = {
            "headline": "Q4FY26 Result something else",  # no "Result Announced for"
            "url": "x",
            "datePublished": "2026-05-25T12:00:00+00:00",
        }
        assert trendlyne._normalize("1", meta) is None


class TestFetch:
    def test_fetch_parses_fixture(self, monkeypatch, fixture_html):
        # Make requests.get return the fixture HTML, regardless of URL.
        class FakeResp:
            status_code = 200
            text = fixture_html
            def raise_for_status(self): pass

        monkeypatch.setattr(trendlyne.requests, "get", lambda *a, **k: FakeResp())
        # The fixture's posts are dated 2026-05-25 UTC = 2026-05-25 IST (no offset issue).
        filings = trendlyne.TrendlyneSource().fetch(date(2026, 5, 25))
        # At least one panel should resolve to a Filing
        assert len(filings) >= 1
        for f in filings:
            assert f.source == "TRENDLYNE"
            assert f.symbol.startswith("TL-")
            assert f.quarter.startswith("Q")

    def test_fetch_filters_by_date(self, monkeypatch, fixture_html):
        class FakeResp:
            status_code = 200
            text = fixture_html
            def raise_for_status(self): pass

        monkeypatch.setattr(trendlyne.requests, "get", lambda *a, **k: FakeResp())
        # Asking for a different date returns 0.
        filings = trendlyne.TrendlyneSource().fetch(date(2020, 1, 1))
        assert len(filings) == 0
