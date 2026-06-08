"""Unit tests for src.pipeline.scorer — pure functions, no I/O.

Covers BRD §3.4 FR-4.1 formula and the edge-case policy locked in the
Phase 4 plan:
    - cohort < 2 non-NULL values for a component → component z = None
    - σ == 0 → component z = 0 for every non-NULL row
    - row missing a component → weights renormalize over present components
    - row with <RANK_MIN_COMPONENTS non-NULL z → composite score = None
"""

from __future__ import annotations

import math

import pytest

from src import config
from src.pipeline.scorer import score_cohort


def _row(
    filing_id: int,
    *,
    sue: float | None = 0.0,
    rev: float | None = 0.0,
    ear: float | None = 0.0,
    vol: float | None = 1.0,
    margin: float | None = 0.0,
) -> dict:
    return {
        "filing_id": filing_id,
        "symbol_nse": f"SYM{filing_id}",
        "sue_proxy": sue,
        "rev_growth_yoy": rev,
        "ear": ear,
        "vol_spike": vol,
        "margin_delta": margin,
    }


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_cohort(self) -> None:
        assert score_cohort([]) == []

    def test_single_row_cohort_yields_no_zscores(self) -> None:
        # With N=1, stdev is undefined → every z component is None → composite None.
        out = score_cohort([_row(1, sue=1.0, rev=0.5, ear=0.02, vol=2.0, margin=3.0)])
        assert len(out) == 1
        assert out[0].pead_score is None
        assert out[0].n_components == 0
        assert all(getattr(out[0], f) is None for f in
                   ("z_sue", "z_rev", "z_ear", "z_vol", "z_margin"))

    def test_zero_variance_component_yields_zero_z(self) -> None:
        # SUE identical across cohort → σ=0 → z_sue = 0 for all; other components vary.
        rows = [
            _row(1, sue=10.0, rev=0.1, ear=0.01, vol=1.0, margin=0.0),
            _row(2, sue=10.0, rev=0.5, ear=0.05, vol=2.0, margin=2.0),
            _row(3, sue=10.0, rev=0.9, ear=0.10, vol=3.0, margin=4.0),
        ]
        out = score_cohort(rows)
        assert all(r.z_sue == 0.0 for r in out)
        # rev / ear / vol / margin should have real z values
        assert all(r.z_rev is not None for r in out)


# ---------------------------------------------------------------------------
# Composite formula
# ---------------------------------------------------------------------------


class TestComposite:
    def test_two_row_cohort_z_and_composite(self) -> None:
        rows = [
            _row(1, sue=2.0, rev=0.10, ear=0.01, vol=1.5, margin=1.0),
            _row(2, sue=4.0, rev=0.30, ear=0.05, vol=2.5, margin=3.0),
        ]
        out = score_cohort(rows)
        # Manual z: with N=2 ddof=1 stdev, z values are ±1/√2 by symmetry.
        # We just check sign correctness + composite uses all 5 components.
        assert out[0].n_components == 5
        assert out[1].n_components == 5
        assert out[0].pead_score is not None
        assert out[1].pead_score is not None
        # Row 2 has higher values in every component → higher composite.
        assert out[1].pead_score > out[0].pead_score

    def test_weights_renormalize_when_components_drop(self) -> None:
        """BSE-only-style row: SUE + Margin missing → score uses only Rev/EAR/Vol."""
        rows = [
            # Row 0: all 5 components, used to give cohort variance.
            _row(0, sue=0.0, rev=-0.1, ear=-0.02, vol=1.0, margin=-1.0),
            # Row 1: full
            _row(1, sue=2.0, rev=0.10, ear=0.01, vol=1.5, margin=1.0),
            # Row 2: missing SUE + Margin → renormalize weights of rev/ear/vol
            _row(2, sue=None, rev=0.30, ear=0.05, vol=2.5, margin=None),
        ]
        out = score_cohort(rows)
        r2 = out[2]
        assert r2.n_components == 3
        assert r2.z_sue is None
        assert r2.z_margin is None
        assert r2.pead_score is not None

        # Composite for r2 = (w_rev*z_rev + w_ear*z_ear + w_vol*z_vol) / (w_rev+w_ear+w_vol)
        w = config.WEIGHTS
        total_w = w["rev"] + w["ear"] + w["vol"]
        expected = (
            w["rev"] * r2.z_rev + w["ear"] * r2.z_ear + w["vol"] * r2.z_vol
        ) / total_w
        assert r2.pead_score == pytest.approx(expected)

    def test_below_min_components_yields_none(self) -> None:
        """Row with only 2 non-NULL z components → score=None (RANK_MIN_COMPONENTS=3)."""
        rows = [
            _row(0, sue=0.0, rev=0.0, ear=0.0, vol=1.0, margin=0.0),
            _row(1, sue=2.0, rev=0.1, ear=0.01, vol=1.5, margin=1.0),
            _row(2, sue=None, rev=None, ear=0.05, vol=2.5, margin=None),
        ]
        out = score_cohort(rows)
        r2 = out[2]
        assert r2.n_components == 2
        assert r2.pead_score is None

    def test_all_components_missing_yields_none(self) -> None:
        rows = [
            _row(0, sue=0.0, rev=0.0, ear=0.0, vol=1.0, margin=0.0),
            _row(1, sue=2.0, rev=0.1, ear=0.01, vol=1.5, margin=1.0),
            _row(2, sue=None, rev=None, ear=None, vol=None, margin=None),
        ]
        out = score_cohort(rows)
        r2 = out[2]
        assert r2.n_components == 0
        assert r2.pead_score is None
        assert all(getattr(r2, f) is None for f in
                   ("z_sue", "z_rev", "z_ear", "z_vol", "z_margin"))


# ---------------------------------------------------------------------------
# Determinism + ordering
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        rows = [
            _row(i, sue=float(i), rev=float(i) * 0.1, ear=float(i) * 0.01,
                 vol=1.0 + i * 0.5, margin=float(i)) for i in range(10)
        ]
        a = score_cohort(rows)
        b = score_cohort(rows)
        for x, y in zip(a, b, strict=True):
            assert x == y

    def test_preserves_input_order(self) -> None:
        rows = [_row(i, sue=float(i), rev=float(10 - i)) for i in range(5)]
        out = score_cohort(rows)
        assert [s.filing_id for s in out] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Coercion of Supabase string NUMERICs
# ---------------------------------------------------------------------------


class TestNumericCoercion:
    def test_string_numerics_accepted(self) -> None:
        rows = [
            _row(0, sue=0.0, rev=0.0, ear=0.0, vol=1.0, margin=0.0),
            {
                "filing_id": 1,
                "symbol_nse": "STR1",
                "sue_proxy": "2.0",
                "rev_growth_yoy": "0.1",
                "ear": "0.01",
                "vol_spike": "1.5",
                "margin_delta": "1.0",
            },
        ]
        out = score_cohort(rows)
        assert out[1].pead_score is not None
        assert math.isfinite(out[1].pead_score)
