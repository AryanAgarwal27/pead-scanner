"""Signal tiering, confirmation checklist, and position sizing — Phase 5.

Pure functions. No I/O. The signaler (src.pipeline.signaler) is the only
caller; it hands in a row's composite score plus the already-fetched
confirmation inputs and gets back a tier, the 5 confirmation booleans, and a
suggested position size (or a skip).

The three decisions, all from BRD §3.5:

  assign_tier(score)                  → FR-5.4 tier matrix
  evaluate_confirmations(inputs)      → FR-5.5 five-point checklist
  decide_size_r(score, confirmations) → FR-5.6 sizing matrix + hard-skip rule

Threshold sources of truth live in src.config (TIER_THRESHOLDS, CONF_*); this
module never hardcodes a number that the BRD parameterizes.
"""

from __future__ import annotations

from src import config

# Confirmation keys, in display order (C1..C5). Single source of truth so the
# message formatter, the JSONB payload, and the tests all agree.
CONFIRMATION_KEYS: tuple[str, ...] = ("C1", "C2", "C3", "C4", "C5")

# The two non-negotiable confirmations: failing EITHER forces a skip
# regardless of score or how many others passed (FR-5.6 last row).
HARD_CONFIRMATIONS: tuple[str, ...] = ("C2", "C4")

# Tiers that are sent (SKIP is never persisted/sent).
SENDABLE_TIERS: frozenset[str] = frozenset({"WATCH", "TAKE", "STRONG"})


# ---------------------------------------------------------------------------
# FR-5.4 — tier from score
# ---------------------------------------------------------------------------


def assign_tier(score: float) -> str:
    """Map a composite PEAD score (σ) to a tier.

    Bands are half-open [lower, upper) per BRD §3.5 FR-5.4:
        SKIP   < 2.0
        WATCH  2.0 – 2.5
        TAKE   2.5 – 3.0
        STRONG > 3.0
    A score on a boundary (e.g. exactly 2.5) lands in the HIGHER band, matching
    "2.5 – 3.0 → TAKE" reading 2.5 as the start of TAKE.
    """
    # Evaluate from the top down so boundary values resolve to the higher tier.
    if score >= config.TIER_THRESHOLDS["STRONG"][0]:        # ≥ 3.0
        return "STRONG"
    if score >= config.TIER_THRESHOLDS["TAKE"][0]:          # ≥ 2.5
        return "TAKE"
    if score >= config.TIER_THRESHOLDS["WATCH"][0]:         # ≥ 2.0
        return "WATCH"
    return "SKIP"


# ---------------------------------------------------------------------------
# FR-5.5 — confirmation checklist
# ---------------------------------------------------------------------------


def evaluate_confirmations(
    *,
    vol_spike: float | None,
    nifty_is_above: bool | None,
    t1_move_pct: float | None,
    liquidity_ok: bool | None,
    no_corporate_action: bool,
) -> dict[str, bool]:
    """Run the 5-point confirmation checklist (BRD §3.5 FR-5.5).

    Args:
        vol_spike:        metrics.vol_spike = Vol_T+1 / avg(T-30..T-1). C1.
        nifty_is_above:   Nifty close > 50-DMA (run-level). C2. None → fail.
        t1_move_pct:      |Close_T+1 / Close_T-1 - 1|, fractional. C3.
        liquidity_ok:     nominal-1.0R position ≤ 10% of 30d turnover. C4.
                          None (no turnover data) → fail.
        no_corporate_action: True if NO split/bonus/dividend ex-date within
                          ±CONF_CORPORATE_ACTION_WINDOW_DAYS of T+1. C5.

    Missing-data policy: any input we cannot evaluate counts as a FAILED
    confirmation (conservative). For the soft confirmations (C1/C3/C5) that
    just lowers the pass count; for the hard ones (C2/C4) it forces a skip via
    decide_size_r.
    """
    c1 = vol_spike is not None and vol_spike >= config.CONF_VOLUME_MULTIPLIER
    c2 = bool(nifty_is_above)
    c3 = t1_move_pct is not None and t1_move_pct <= config.CONF_MAX_EXTENSION_PCT
    c4 = bool(liquidity_ok)
    c5 = bool(no_corporate_action)
    return {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5}


def count_passed(confirmations: dict[str, bool]) -> int:
    return sum(1 for k in CONFIRMATION_KEYS if confirmations.get(k))


# ---------------------------------------------------------------------------
# FR-5.6 — position sizing
# ---------------------------------------------------------------------------


def decide_size_r(score: float, confirmations: dict[str, bool]) -> float | None:
    """Suggested position size in R, or None to SKIP (BRD §3.5 FR-5.6).

        ≥ 2.5σ & 5/5                       → 1.0R
        (≥ 2.5σ & 4/5) OR (≥ 2.0σ & 5/5)   → 0.5R
        anything else                      → skip
        C2 or C4 failed                    → skip (non-negotiable, overrides all)

    Returns 1.0, 0.5, or None.
    """
    # Hard rule first — a failed regime/liquidity gate skips unconditionally.
    if not all(confirmations.get(k) for k in HARD_CONFIRMATIONS):
        return None

    passed = count_passed(confirmations)
    take_floor = config.TIER_THRESHOLDS["TAKE"][0]    # 2.5
    watch_floor = config.TIER_THRESHOLDS["WATCH"][0]  # 2.0

    if score >= take_floor and passed == 5:
        return 1.0
    if (score >= take_floor and passed == 4) or (score >= watch_floor and passed == 5):
        return 0.5
    return None
