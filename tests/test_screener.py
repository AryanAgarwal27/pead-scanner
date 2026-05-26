"""Screener parser tests — uses a captured HTML fixture so they don't hit the network.

Covers:
- Quarter header decoding (Mar 2026 -> Q4-FY26 etc).
- Newest-first ordering, max 8 entries.
- Money cell parsing (commas, parens, em-dashes).
- Percent cell parsing.
"""

from __future__ import annotations

import pathlib

import pytest

from src.sources.screener import (
    _column_to_quarter_label,
    _parse_html,
    _parse_money,
    _parse_percent,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "screener_cmsinfo.html"


def test_column_to_quarter_label() -> None:
    assert _column_to_quarter_label("Mar 2026") == "Q4-FY26"
    assert _column_to_quarter_label("Jun 2025") == "Q1-FY26"
    assert _column_to_quarter_label("Sep 2025") == "Q2-FY26"
    assert _column_to_quarter_label("Dec 2025") == "Q3-FY26"
    assert _column_to_quarter_label("Mar 2023") == "Q4-FY23"
    assert _column_to_quarter_label("garbage") is None
    assert _column_to_quarter_label("") is None


def test_parse_money_handles_paren_negative() -> None:
    assert _parse_money("(45.67)") == -45.67
    assert _parse_money("-12") == -12.0
    assert _parse_money("1,234.56") == 1234.56
    assert _parse_money("627") == 627.0


def test_parse_money_handles_missing() -> None:
    assert _parse_money("-") is None
    assert _parse_money("") is None
    assert _parse_money("—") is None


def test_parse_percent() -> None:
    assert _parse_percent("29%") == 29.0
    assert _parse_percent("-2.5%") == -2.5
    assert _parse_percent("100%") == 100.0
    assert _parse_percent("-") is None


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture missing")
def test_parse_real_fixture_cmsinfo() -> None:
    """End-to-end against a captured Screener page for CMSINFO (Q4-FY26 era)."""
    html = FIXTURE.read_text(encoding="utf-8")
    f = _parse_html(html, "CMSINFO", basis="consolidated")

    assert f.symbol == "CMSINFO"
    assert f.company_name and "CMS Info" in f.company_name
    assert f.on_screener is True
    assert f.used_basis == "consolidated"

    # Expect 8 entries newest-first; newest is the most-recent quarter the
    # captured page showed.
    assert len(f.quarterly_rev) == 8
    assert len(f.quarterly_pat) == 8
    assert len(f.quarterly_opm) == 8

    # Newest quarter must be the one with the largest fiscal year + quarter.
    # In the captured page (May 2026 capture) it's Mar 2026 → Q4-FY26.
    assert f.quarterly_rev[0]["quarter"] == "Q4-FY26"
    assert f.quarterly_rev[0]["value"] == pytest.approx(633, rel=0.01)
    assert f.quarterly_pat[0]["quarter"] == "Q4-FY26"
    assert f.quarterly_opm[0]["quarter"] == "Q4-FY26"
    # OPM cell was '25%' for Mar 2026 column in the captured fixture.
    assert f.quarterly_opm[0]["value"] == pytest.approx(25, rel=0.05)


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture missing")
def test_quarters_strictly_newest_first() -> None:
    """The first entry's quarter must rank latest by (FY, Q)."""
    html = FIXTURE.read_text(encoding="utf-8")
    f = _parse_html(html, "CMSINFO", basis="consolidated")

    def rank(q: str) -> tuple[int, int]:
        # 'Q3-FY26' -> (26, 3) — higher FY first, then higher Q
        qpart, fypart = q.split("-")
        return (int(fypart[2:]), int(qpart[1:]))

    ranks = [rank(e["quarter"]) for e in f.quarterly_rev]
    assert ranks == sorted(ranks, reverse=True), f"not newest-first: {ranks}"
