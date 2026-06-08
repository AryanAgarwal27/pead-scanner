"""Unit tests for src.pipeline.tiering — pure functions, no I/O.

Covers BRD §3.5:
    FR-5.4 tier matrix          (assign_tier)
    FR-5.5 confirmation checks   (evaluate_confirmations / count_passed)
    FR-5.6 sizing matrix + the C2/C4 non-negotiable skip (decide_size_r)
"""

from __future__ import annotations

import pytest

from src import config
from src.pipeline import tiering

# ---------------------------------------------------------------------------
# FR-5.4 — tier from score
# ---------------------------------------------------------------------------


class TestAssignTier:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (-1.0, "SKIP"),
            (0.0, "SKIP"),
            (1.99, "SKIP"),
            (2.0, "WATCH"),      # boundary → higher band
            (2.49, "WATCH"),
            (2.5, "TAKE"),       # boundary → higher band
            (2.99, "TAKE"),
            (3.0, "STRONG"),     # boundary → higher band
            (5.0, "STRONG"),
        ],
    )
    def test_bands(self, score: float, expected: str) -> None:
        assert tiering.assign_tier(score) == expected

    def test_sendable_excludes_skip(self) -> None:
        assert "SKIP" not in tiering.SENDABLE_TIERS
        assert tiering.SENDABLE_TIERS == frozenset({"WATCH", "TAKE", "STRONG"})


# ---------------------------------------------------------------------------
# FR-5.5 — confirmations
# ---------------------------------------------------------------------------


def _confirm(**over) -> dict:
    """Default = all 5 pass; override individual inputs."""
    base = dict(
        vol_spike=3.0,          # ≥ 2.0  → C1 pass
        nifty_is_above=True,    # C2 pass
        t1_move_pct=0.05,       # ≤ 0.12 → C3 pass
        liquidity_ok=True,      # C4 pass
        no_corporate_action=True,  # C5 pass
    )
    base.update(over)
    return tiering.evaluate_confirmations(**base)


class TestConfirmations:
    def test_all_pass(self) -> None:
        c = _confirm()
        assert c == {"C1": True, "C2": True, "C3": True, "C4": True, "C5": True}
        assert tiering.count_passed(c) == 5

    def test_c1_volume_threshold(self) -> None:
        assert _confirm(vol_spike=2.0)["C1"] is True       # exactly 2× passes
        assert _confirm(vol_spike=1.99)["C1"] is False
        assert _confirm(vol_spike=None)["C1"] is False     # missing → fail

    def test_c2_regime_none_fails(self) -> None:
        assert _confirm(nifty_is_above=None)["C2"] is False
        assert _confirm(nifty_is_above=False)["C2"] is False

    def test_c3_extension_threshold(self) -> None:
        assert _confirm(t1_move_pct=0.12)["C3"] is True     # exactly 12% passes
        assert _confirm(t1_move_pct=0.1201)["C3"] is False
        assert _confirm(t1_move_pct=None)["C3"] is False

    def test_c4_liquidity_none_fails(self) -> None:
        assert _confirm(liquidity_ok=None)["C4"] is False
        assert _confirm(liquidity_ok=False)["C4"] is False

    def test_c5_corporate_action(self) -> None:
        assert _confirm(no_corporate_action=False)["C5"] is False

    def test_count_passed_partial(self) -> None:
        c = _confirm(vol_spike=None, t1_move_pct=None)  # C1, C3 fail
        assert tiering.count_passed(c) == 3


# ---------------------------------------------------------------------------
# FR-5.6 — sizing matrix
# ---------------------------------------------------------------------------


class TestDecideSizeR:
    def test_strong_full_position(self) -> None:
        # ≥2.5σ & 5/5 → 1.0R
        assert tiering.decide_size_r(2.6, _confirm()) == 1.0
        assert tiering.decide_size_r(3.5, _confirm()) == 1.0

    def test_take_four_of_five_half(self) -> None:
        # ≥2.5σ & 4/5 → 0.5R  (drop a SOFT confirmation, keep C2+C4)
        c = _confirm(vol_spike=None)  # C1 fails, 4/5, C2+C4 intact
        assert tiering.count_passed(c) == 4
        assert tiering.decide_size_r(2.7, c) == 0.5

    def test_watch_five_of_five_half(self) -> None:
        # ≥2.0σ & 5/5 → 0.5R
        assert tiering.decide_size_r(2.1, _confirm()) == 0.5

    def test_watch_four_of_five_skip(self) -> None:
        # ≥2.0σ & <5 → skip
        c = _confirm(vol_spike=None)  # 4/5
        assert tiering.decide_size_r(2.1, c) is None

    def test_take_three_of_five_skip(self) -> None:
        # ≥2.5σ but only 3/5 (two soft fails) → not covered by 1.0/0.5 rows → skip
        c = _confirm(vol_spike=None, t1_move_pct=None)  # C1+C3 fail, 3/5
        assert tiering.count_passed(c) == 3
        assert tiering.decide_size_r(2.8, c) is None

    def test_c2_failure_is_non_negotiable_skip(self) -> None:
        # Even a 4/5 STRONG with C2 failed → skip.
        c = _confirm(nifty_is_above=False)  # C2 fail
        assert tiering.count_passed(c) == 4
        assert tiering.decide_size_r(3.5, c) is None

    def test_c4_failure_is_non_negotiable_skip(self) -> None:
        c = _confirm(liquidity_ok=False)  # C4 fail
        assert tiering.decide_size_r(3.5, c) is None

    def test_both_hard_pass_but_low_count_skip(self) -> None:
        # C2+C4 pass, but only 2 total (C1,C3,C5 fail) → skip.
        c = _confirm(vol_spike=None, t1_move_pct=None, no_corporate_action=False)
        assert c["C2"] is True and c["C4"] is True
        assert tiering.count_passed(c) == 2
        assert tiering.decide_size_r(3.5, c) is None


# ---------------------------------------------------------------------------
# Config alignment — thresholds come from config, not hardcoded
# ---------------------------------------------------------------------------


class TestConfigAlignment:
    def test_tier_floors_match_config(self) -> None:
        assert config.TIER_THRESHOLDS["WATCH"][0] == 2.0
        assert config.TIER_THRESHOLDS["TAKE"][0] == 2.5
        assert config.TIER_THRESHOLDS["STRONG"][0] == 3.0
