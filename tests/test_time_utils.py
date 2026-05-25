"""Unit tests for time_utils — quarter derivation and IST helpers."""

from datetime import UTC, datetime

import pytest

from src.utils.time_utils import (
    IST,
    _most_recent_quarter_end,
    _quarter_label,
    derive_quarter,
    format_ist,
    parse_bse_timestamp,
)


class TestQuarterLabel:
    @pytest.mark.parametrize(
        "year,month,expected",
        [
            (2025, 6, "Q1-FY26"),
            (2025, 9, "Q2-FY26"),
            (2025, 12, "Q3-FY26"),
            (2026, 3, "Q4-FY26"),
            (2026, 6, "Q1-FY27"),
        ],
    )
    def test_known_quarter_ends(self, year, month, expected):
        assert _quarter_label(year, month) == expected

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            _quarter_label(2025, 5)


class TestMostRecentQuarterEnd:
    @pytest.mark.parametrize(
        "ymd,expected",
        [
            ((2026, 1, 25), (2025, 12)),   # Q3-FY26 filings land Jan
            ((2026, 2, 14), (2025, 12)),
            ((2026, 4, 10), (2026, 3)),    # Q4-FY26 filings land Apr
            ((2026, 5, 28), (2026, 3)),
            ((2026, 8, 1),  (2026, 6)),    # Q1-FY27 filings land Aug
            ((2026, 11, 5), (2026, 9)),    # Q2-FY27 filings land Nov
        ],
    )
    def test_recent_end(self, ymd, expected):
        from datetime import date
        assert _most_recent_quarter_end(date(*ymd)) == expected


class TestDeriveQuarter:
    def _ist_to_utc(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(UTC)

    def test_headline_named_month_first(self):
        dt = self._ist_to_utc(2026, 1, 25, 18, 23)
        q, src = derive_quarter(dt, "Quarter Ended December 31, 2025")
        assert q == "Q3-FY26"
        assert src == "headline"

    def test_headline_day_first_named_month(self):
        dt = self._ist_to_utc(2026, 4, 22, 16, 45)
        q, src = derive_quarter(dt, "Quarter and Year Ended 31 March 2026")
        assert q == "Q4-FY26"
        assert src == "headline"

    def test_headline_numeric_slash(self):
        dt = self._ist_to_utc(2026, 1, 13, 14, 5)
        q, src = derive_quarter(dt, "Audited results for the Quarter ended 31/12/2025")
        assert q == "Q3-FY26"
        assert src == "headline"

    def test_headline_numeric_dot(self):
        dt = self._ist_to_utc(2025, 11, 5, 10, 0)
        q, src = derive_quarter(dt, "Results for Quarter ended 30.09.2025")
        assert q == "Q2-FY26"
        assert src == "headline"

    def test_fallback_when_no_headline_match(self):
        dt = self._ist_to_utc(2026, 1, 28, 17, 0)
        q, src = derive_quarter(dt, "Outcome of Board Meeting - financial results")
        assert q == "Q3-FY26"
        assert src == "filing-date-fallback"

    def test_fallback_with_empty_headline(self):
        dt = self._ist_to_utc(2026, 8, 12, 11, 30)
        q, src = derive_quarter(dt, "")
        assert q == "Q1-FY27"
        assert src == "filing-date-fallback"

    def test_non_quarter_end_date_in_headline_falls_back(self):
        # Headline mentions an "ended" date that's NOT a quarter-end — should fall back.
        dt = self._ist_to_utc(2026, 1, 28, 17, 0)
        q, src = derive_quarter(dt, "Pursuant to circular ended 15-01-2026, please find attached")
        assert src == "filing-date-fallback"
        assert q == "Q3-FY26"


class TestParseBseTimestamp:
    def test_iso_with_millis(self):
        dt = parse_bse_timestamp("2026-01-25 18:23:00.000")
        # 18:23 IST = 12:53 UTC
        assert dt.tzinfo is UTC
        assert dt.hour == 12 and dt.minute == 53

    def test_iso_without_millis(self):
        dt = parse_bse_timestamp("2026-01-25 18:23:00")
        assert dt.hour == 12 and dt.minute == 53

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_bse_timestamp("not-a-date")


class TestFormatIst:
    def test_utc_to_ist_string(self):
        dt = datetime(2026, 5, 25, 8, 53, tzinfo=UTC)
        # 08:53 UTC = 14:23 IST
        assert format_ist(dt) == "25-May-2026, 14:23 IST"
