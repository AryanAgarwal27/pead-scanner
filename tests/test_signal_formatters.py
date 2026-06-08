"""Unit tests for the Phase 5 signal message templates (notify.formatters).

Covers FR-5.3 field coverage, NULL z-component rendering as "—" (n_components
can be 3–5), tier-specific decorations, and the run-summary concentration
flags (FR-5.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src import config
from src.notify.formatters import (
    _z_or_dash,
    format_signal,
    format_signal_summary,
)


def _signal_payload(**over) -> dict:
    base = {
        "filing_id": 1,
        "symbol": "HDFCBANK",
        "rank": 3,
        "pead_score": 2.73,
        "tier": "TAKE",
        "confirmations": {"C1": True, "C2": True, "C3": False, "C4": True, "C5": True},
        "confirmations_passed": 4,
        "suggested_size_r": 0.5,
        "entry_price": 1650.50,
        "stop_price": 1600.00,
        "target1_price": 1726.25,
        "risk_reward": 1.5,
        "status": "PENDING_ENTRY",
        "_z": {
            "z_sue": 1.85,
            "z_rev": 0.42,
            "z_ear": 2.10,
            "z_vol": -0.30,
            "z_margin": 0.05,
        },
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# _z_or_dash
# ---------------------------------------------------------------------------


class TestZOrDash:
    def test_none_renders_em_dash(self) -> None:
        assert _z_or_dash(None) == "—"

    def test_number_renders_signed_two_dp(self) -> None:
        assert _z_or_dash(1.5) == "+1.50"
        assert _z_or_dash(-0.3) == "-0.30"
        assert _z_or_dash(0.0) == "+0.00"


# ---------------------------------------------------------------------------
# format_signal
# ---------------------------------------------------------------------------


class TestFormatSignal:
    def test_includes_all_required_fields(self) -> None:
        msg = format_signal(_signal_payload())
        # rank + symbol + tier + score
        assert "#3 HDFCBANK" in msg
        assert "TAKE" in msg
        assert "2.73σ" in msg
        # levels
        assert "Entry: 1650.50" in msg
        assert "Stop:  1600.00" in msg
        assert "T1:    1726.25" in msg
        assert "R:R 1.50" in msg
        # T2 descriptor (no static price, FR-5.1 trailing)
        assert f"Trail {config.TRAILING_EMA}-EMA" in msg
        assert f"max {config.MAX_HOLD_DAYS}d" in msg
        # confirmations checklist with pass count
        assert "*Confirmations* (4/5)" in msg
        assert "✅ C1" in msg
        assert "❌ C3" in msg
        # size
        assert "0.5R" in msg

    def test_null_z_components_render_as_dash_not_crash(self) -> None:
        # BSE-only-style row: SUE + Margin missing (n_components == 3).
        payload = _signal_payload(
            _z={"z_sue": None, "z_rev": 0.42, "z_ear": 2.10, "z_vol": 1.20, "z_margin": None}
        )
        msg = format_signal(payload)
        assert "SUE —" in msg
        assert "Margin —" in msg
        assert "Rev +0.42" in msg

    def test_missing_z_dict_entirely_is_safe(self) -> None:
        payload = _signal_payload()
        del payload["_z"]
        msg = format_signal(payload)  # must not raise
        # all five components fall back to dashes
        assert msg.count("—") >= 5

    def test_strong_tier_flags_for_review(self) -> None:
        msg = format_signal(_signal_payload(tier="STRONG", pead_score=3.4))
        assert "🔥" in msg
        assert "manual review" in msg.lower()

    def test_watch_tier_no_review_flag(self) -> None:
        msg = format_signal(_signal_payload(tier="WATCH", pead_score=2.1))
        assert "manual review" not in msg.lower()

    def test_symbol_markdown_escaped(self) -> None:
        msg = format_signal(_signal_payload(symbol="M&M_FIN"))
        assert "M&M\\_FIN" in msg


# ---------------------------------------------------------------------------
# format_signal_summary
# ---------------------------------------------------------------------------


@dataclass
class _FakeSummary:
    run_date: date = date(2026, 5, 26)
    ranked_count: int = 10
    skipped_tier: int = 3
    skipped_sizing: int = 2
    skipped_data: int = 1
    already_signalled: int = 0
    sent_count: int = 4
    by_tier: dict = field(default_factory=lambda: {"STRONG": 1, "TAKE": 2, "WATCH": 1})
    regime_available: bool = True
    flags: list = field(default_factory=list)


class TestFormatSignalSummary:
    def test_basic_counts(self) -> None:
        msg = format_signal_summary(_FakeSummary())
        assert "PEAD Signals — 2026-05-26" in msg
        assert "Sent: *4*" in msg
        assert "STRONG=1" in msg and "TAKE=2" in msg and "WATCH=1" in msg
        assert "Ranked: 10" in msg

    def test_no_flags_message(self) -> None:
        msg = format_signal_summary(_FakeSummary(flags=[]))
        assert "No concentration limits breached" in msg

    def test_flags_rendered(self) -> None:
        flags = [
            "⚠️ Open PEAD positions: 14 (> 12 limit)",
            "⚠️ Banks: 5 positions (> 4 per-sector limit)",
        ]
        msg = format_signal_summary(_FakeSummary(flags=flags))
        assert "Concentration flags" in msg
        assert "14 (> 12 limit)" in msg
        assert "Banks: 5 positions" in msg

    def test_regime_unavailable_warning(self) -> None:
        msg = format_signal_summary(_FakeSummary(regime_available=False, sent_count=0))
        assert "Nifty regime unavailable" in msg
