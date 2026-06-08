"""Composite PEAD score — Phase 4 (BRD §3.4 FR-4.1, FR-4.2).

Pure functions. No I/O. The ranker (src.pipeline.ranker) is the only caller;
it hands in a list of cohort rows (one per filing already past the hard
filters) and gets back z-component decorations + a composite score per row.

Composite formula (BRD §3.4 FR-4.1):

    pead_score = 0.35 * z(SUE_proxy)
               + 0.20 * z(Rev_Growth_YoY)
               + 0.25 * z(EAR)
               + 0.15 * z(Vol_Spike)
               + 0.05 * z(Margin_Delta)

Z-cohort (BRD FR-4.2):
    Every component is z-normalized within the cohort of filings landed in
    the trailing COHORT_WINDOW_DAYS (default 7). That's the *input* cohort
    handed to this module — `score_cohort` does NOT itself decide which
    filings to include.

Edge-case policy (locked in plan):
    * stdev with ddof=1 (sample); cohort < 2 non-NULL values for a component
      → that component's z is None for every row in the cohort
    * σ == 0 for a component (all cohort values identical) → z = 0 for every
      row in the cohort (preserves weighted contribution but adds no rank info)
    * a row's component value is None → that component's z for that row is
      None and its weight is renormalized away from the composite
    * a row with fewer than RANK_MIN_COMPONENTS non-NULL z components after
      the above → composite is None (caller drops it from the ranking)
    * a row with all 5 components None → composite is None

Math note on renormalization: with all 5 components present, the weights
sum to 1.0 and the composite is a literal weighted average. When the SUE
(0.35) and Margin (0.05) components drop out (BSE-only stocks), the
remaining 3 weights (0.20+0.25+0.15 = 0.60) are renormalized to sum to 1.0,
so the composite scale stays comparable across rows. n_components is
persisted alongside the score so operators can audit which rows scored on
which subset.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from src import config

# Map between scorer's component keys and the metrics column names + weight keys.
# Single source of truth so the formula doesn't drift across scorer / DB / config.
_COMPONENT_KEYS: tuple[tuple[str, str], ...] = (
    # (scorer-key, metrics-column-name)
    ("sue",    "sue_proxy"),
    ("rev",    "rev_growth_yoy"),
    ("ear",    "ear"),
    ("vol",    "vol_spike"),
    ("margin", "margin_delta"),
)


@dataclass
class ScoredRow:
    """One row of the cohort after scoring.

    `pead_score` is None when fewer than RANK_MIN_COMPONENTS components
    contributed — caller (ranker) drops those from the top-N output.

    `z_<component>` is None when that component was missing for this row OR
    when the cohort lacked sufficient variance / coverage to compute the z.
    """

    filing_id: int
    symbol_nse: str
    pead_score: float | None
    n_components: int
    z_sue: float | None
    z_rev: float | None
    z_ear: float | None
    z_vol: float | None
    z_margin: float | None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def score_cohort(rows: list[dict[str, Any]]) -> list[ScoredRow]:
    """Score every row in the cohort. Returns one ScoredRow per input row,
    in the same order. `pead_score` may be None for individual rows.

    Each input dict must contain:
        filing_id:        int
        symbol_nse:       str
        sue_proxy:        float | None
        rev_growth_yoy:   float | None
        ear:              float | None
        vol_spike:        float | None
        margin_delta:     float | None
    """
    if not rows:
        return []

    # Per-component cohort z-vectors, indexed by row position.
    z_by_key: dict[str, list[float | None]] = {}
    for key, col in _COMPONENT_KEYS:
        values = [_as_float(r.get(col)) for r in rows]
        z_by_key[key] = _z_normalize(values)

    out: list[ScoredRow] = []
    for i, r in enumerate(rows):
        z_components = {k: z_by_key[k][i] for k, _ in _COMPONENT_KEYS}
        composite, n_present = _composite(z_components)
        out.append(
            ScoredRow(
                filing_id=int(r["filing_id"]),
                symbol_nse=str(r["symbol_nse"]),
                pead_score=composite,
                n_components=n_present,
                z_sue=z_components["sue"],
                z_rev=z_components["rev"],
                z_ear=z_components["ear"],
                z_vol=z_components["vol"],
                z_margin=z_components["margin"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _z_normalize(values: list[float | None]) -> list[float | None]:
    """Z-normalize a single component vector across the cohort.

    Returns a same-length list where:
      * each None input maps to None
      * if <2 non-NULL values present overall, every output is None
      * if stdev == 0 (all non-NULL values identical), every non-NULL output
        is 0.0 (None inputs still map to None)
    """
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return [None] * len(values)

    mu = statistics.fmean(present)
    sigma = statistics.stdev(present)  # sample stdev, ddof=1

    if sigma == 0:
        return [0.0 if v is not None else None for v in values]

    return [((v - mu) / sigma) if v is not None else None for v in values]


def _composite(z: dict[str, float | None]) -> tuple[float | None, int]:
    """Weighted composite with renormalization over present components.

    Returns (score, n_components). score is None when n_components is below
    RANK_MIN_COMPONENTS. n_components is the count of non-NULL z values
    that contributed.
    """
    contributing = {k: v for k, v in z.items() if v is not None}
    n = len(contributing)
    if n < config.RANK_MIN_COMPONENTS:
        return None, n

    total_w = sum(config.WEIGHTS[k] for k in contributing)
    if total_w == 0:                                # config typo guard
        return None, n
    weighted_sum = sum(config.WEIGHTS[k] * v for k, v in contributing.items())
    return weighted_sum / total_w, n


def _as_float(v: Any) -> float | None:
    """Coerce Supabase NUMERIC values (str | float | int | None) to float | None.

    Supabase-py returns NUMERIC columns as `str` in some versions and `float`
    in others — be defensive.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
