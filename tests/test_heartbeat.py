"""Tests for jobs.heartbeat — message formatting."""

from datetime import UTC, datetime

from jobs import heartbeat


def _now():
    return datetime(2026, 5, 26, 3, 30, tzinfo=UTC)  # 09:00 IST


class TestFormatMessage:
    def test_all_up_message(self):
        results = [
            ("NSE", True, 412, None, 5),
            ("BSE", True, 318, None, 12),
            ("TRENDLYNE", True, 1124, None, 0),
        ]
        msg = heartbeat._format_message(_now(), results)
        assert "Daily Heartbeat" in msg
        assert "26-May-2026" in msg
        assert "NSE" in msg and "412ms" in msg and "5 filings" in msg
        assert "BSE" in msg and "318ms" in msg
        assert "TRENDLYNE" in msg
        # No degradation note when all are up.
        assert "Detector will use" not in msg

    def test_partial_failure_message(self):
        results = [
            ("NSE", False, 30001, "ConnectionError: timed out", 0),
            ("BSE", True, 318, None, 12),
            ("TRENDLYNE", True, 1124, None, 0),
        ]
        msg = heartbeat._format_message(_now(), results)
        assert "FAIL" in msg
        assert "ConnectionError" in msg
        # The degradation hint appears when at least one is down.
        assert "Detector will use" in msg

    def test_all_down_message(self):
        results = [
            ("NSE", False, 30000, "boom", 0),
            ("BSE", False, 30000, "boom2", 0),
            ("TRENDLYNE", False, 30000, "boom3", 0),
        ]
        msg = heartbeat._format_message(_now(), results)
        assert msg.count("FAIL") == 3
        assert "Detector will use" in msg
