"""Unit tests for notify.formatters."""

from datetime import UTC, datetime

from src.notify.formatters import (
    TELEGRAM_MAX_LEN,
    _escape_md,
    format_batched,
    format_single_filing,
)
from src.sources.bse import BseFiling


def _make_filing(
    symbol="500180",
    company="HDFC Bank Ltd",
    quarter="Q3-FY26",
    quarter_source="headline",
    filing_url="https://www.bseindia.com/x/y.pdf",
    is_consolidated=False,
) -> BseFiling:
    return BseFiling(
        source="BSE",
        symbol=symbol,
        company_name=company,
        quarter=quarter,
        quarter_source=quarter_source,
        filing_time=datetime(2026, 5, 25, 8, 53, tzinfo=UTC),  # 14:23 IST
        filing_url=filing_url,
        is_consolidated=is_consolidated,
        raw_payload={},
    )


class TestEscapeMd:
    def test_escapes_all_markdown_specials(self):
        assert _escape_md("a*b_c[d]e`f") == "a\\*b\\_c\\[d\\]e\\`f"

    def test_no_op_when_clean(self):
        assert _escape_md("plain text") == "plain text"


class TestFormatSingleFiling:
    def test_includes_required_fields(self):
        msg = format_single_filing(_make_filing())
        assert "🔔 *Quarterly Result Filed* (BSE)" in msg
        assert "*Company:* HDFC Bank Ltd" in msg
        assert "*Symbol:* 500180" in msg
        assert "*Quarter:* Q3-FY26" in msg
        assert "*Filed:* 25-May-2026, 14:23 IST" in msg
        assert "parsing in progress" in msg
        assert "[View filing](https://www.bseindia.com/x/y.pdf)" in msg

    def test_swaps_link_line_when_url_missing(self):
        msg = format_single_filing(_make_filing(filing_url=None))
        assert "PDF link not yet available" in msg
        assert "[View filing]" not in msg

    def test_escapes_company_name_with_markdown_chars(self):
        msg = format_single_filing(_make_filing(company="Foo_Bar* Ltd"))
        assert "Foo\\_Bar\\* Ltd" in msg


class TestFormatBatched:
    def test_empty_returns_empty(self):
        assert format_batched([]) == []

    def test_small_batch_renders_one_message(self):
        filings = [_make_filing(symbol=str(i), company=f"Co {i}") for i in range(15)]
        msgs = format_batched(filings)
        assert len(msgs) == 1
        assert "15 Quarterly Results Filed" in msgs[0]
        for i in range(15):
            assert f"(Co {i})" in msgs[0] or f"Co {i}" in msgs[0]

    def test_huge_batch_chunks_under_max_len(self):
        # Build enough filings to force chunking. Each line ~80 chars; 100 filings ~ 8KB > 4KB.
        filings = [
            _make_filing(symbol=str(i), company=f"Company Number {i} With A Reasonably Long Name")
            for i in range(100)
        ]
        msgs = format_batched(filings)
        assert len(msgs) >= 2
        for m in msgs:
            assert len(m) <= TELEGRAM_MAX_LEN

    def test_missing_url_renders_no_pdf_marker(self):
        filings = [_make_filing(filing_url=None)]
        msgs = format_batched(filings)
        assert "_(no PDF)_" in msgs[0]
