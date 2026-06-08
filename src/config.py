"""Project configuration.

All non-secret constants in this module mirror BRD §9.2. If the BRD changes,
update this file — it is the single source of truth in code.

Secrets are loaded lazily from environment variables (or a local `.env` via
python-dotenv). Missing env vars resolve to `None` rather than raising at import
time, so test collection and tooling do not require production credentials.
Individual jobs are responsible for validating that the secrets they need are
present before they run.

`# TODO: phase N` markers indicate constants not yet consumed by any job in the
current phase — useful for grepping "what is wired up vs. deferred."
"""

import os
from pathlib import Path as _Path

from dotenv import load_dotenv

# Best-effort load of a local .env file; silently no-ops if none exists.
load_dotenv()


# ---------------------------------------------------------------------------
# Secrets (from environment) — used by Phase 0 smoke-test workflow onwards.
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")
SUPABASE_URL: str | None = os.getenv("SUPABASE_URL")
SUPABASE_KEY: str | None = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")


# ---------------------------------------------------------------------------
# Source failover order (BRD §3.1, §5.1).
# ---------------------------------------------------------------------------
SOURCES_ORDER = ["NSE", "BSE", "TRENDLYNE"]  # wired in src.pipeline.detector (Phase 2)


# ---------------------------------------------------------------------------
# Day-0 polling (Phase 1, BRD §3.2 FR-2.4).
# ---------------------------------------------------------------------------
POLL_BATCH_THRESHOLD = 10  # if >N new filings land in one poll, condense to a batched message


# ---------------------------------------------------------------------------
# Multi-source resilience (Phase 2, BRD §3.7).
# ---------------------------------------------------------------------------
SOURCE_FETCH_TIMEOUT_SECONDS = 30          # per-source HTTP budget (used by retry helper)
HEARTBEAT_PROBE_TIMEOUT_SECONDS = 10       # tighter budget for daily heartbeat probes
ERROR_ALERT_COOLDOWN_MINUTES = 60          # FR-7.2: at most one alert per source per hour


# ---------------------------------------------------------------------------
# Hard filters (BRD §3.4 FR-4.3) — wired in src.pipeline.filterer (Phase 4).
# ---------------------------------------------------------------------------
MIN_MARKET_CAP_CR = 500
MIN_DAILY_TURNOVER_CR = 5
MIN_LISTING_YEARS = 2

# Parser-confidence floor: only filings parsed at these confidence levels are
# admitted to the ranking cohort. 'low' and 'failed' are excluded. NULL
# (Phase 2 filings predating LLM parsing) is also excluded — those should be
# re-enriched, not ranked.
PARSER_CONFIDENCE_FLOOR: frozenset[str] = frozenset({"high", "medium"})

# Paths to the manually-maintained ban / surveillance lists (BRD §3.4 FR-4.3).
# Both files ship as empty header-only templates; the operator appends rows
# per the maintenance instructions in README. Format documented in
# src/pipeline/banlists.py.
_DATA_DIR = _Path(__file__).resolve().parent / "data"
FNO_BAN_CSV = _DATA_DIR / "fno_ban.csv"
ASM_GSM_CSV = _DATA_DIR / "asm_gsm.csv"


# ---------------------------------------------------------------------------
# Composite PEAD scoring weights (BRD §3.4 FR-4.1).
# Wired in src.pipeline.scorer (Phase 4). Weights are renormalized on the
# fly across whichever components are non-NULL for a given row, so a stock
# with missing SUE/Margin (typical for BSE-only) is still scored on the
# 3 price-derived components.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "sue":    0.35,
    "rev":    0.20,
    "ear":    0.25,
    "vol":    0.15,
    "margin": 0.05,
}

# A row must have at least this many non-NULL z components to be ranked.
# Score on 2 components is too noisy. The 3-component floor matches the
# minimum coverage (EAR + Vol_Spike + Rev_Growth_YoY) we get from yfinance
# + parser even when Screener fundamentals are missing.
RANK_MIN_COMPONENTS = 3

# A cohort with fewer than this many filings does not produce a ranking
# (z-score noise dominates signal at very small N). Job logs and exits 0.
RANK_MIN_COHORT_SIZE = 2


# ---------------------------------------------------------------------------
# Signal generation parameters (BRD §3.5).
# ---------------------------------------------------------------------------
TOP_N = 25                       # ranking cap — Phase 4 (rank_eod); signal gen — Phase 5
STOP_PCT_CAP = 0.05              # Phase 5: stop = tighter of (T+1 low, -5% from entry)
TARGET_R_MULTIPLE = 1.5          # Phase 5: T1 = entry + 1.5 × (entry - stop)
ENTRY_WINDOW_DAYS = 5            # TODO: phase 6 (entry-trigger expiry)
MAX_HOLD_DAYS = 60               # Phase 5: T2 descriptor in message; enforced phase 6
TRAILING_EMA = 20                # Phase 5: T2 descriptor in message; enforced phase 6


# ---------------------------------------------------------------------------
# Z-score cohort window (BRD §3.4 FR-4.2).
# ---------------------------------------------------------------------------
COHORT_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# Signal tiering thresholds (BRD §3.5 FR-5.4).
# ---------------------------------------------------------------------------
TIER_THRESHOLDS = {              # Phase 5: src.pipeline.tiering.assign_tier
    "SKIP":   (0.0, 2.0),
    "WATCH":  (2.0, 2.5),
    "TAKE":   (2.5, 3.0),
    "STRONG": (3.0, 99.0),
}


# ---------------------------------------------------------------------------
# Confirmation checklist thresholds (BRD §3.5 FR-5.5).
# ---------------------------------------------------------------------------
CONF_VOLUME_MULTIPLIER = 2.0              # Phase 5: C1 — T+1 vol ≥ 2× 30-day avg
CONF_MAX_EXTENSION_PCT = 0.12             # Phase 5: C3 — T+1 move ≤ 12% (not extended)
CONF_MAX_LIQUIDITY_PCT = 0.10             # Phase 5: C4 — nominal 1.0R ≤ 10% of 30d turnover
CONF_CORPORATE_ACTION_WINDOW_DAYS = 5     # Phase 5: C5 — no split/bonus/div ex-date ±5 trading days


# ---------------------------------------------------------------------------
# Position sizing (BRD §3.5 FR-5.6).
# ---------------------------------------------------------------------------
DEFAULT_RISK_PER_TRADE_PCT = 0.01         # Phase 5: R = 1% of portfolio per trade
PORTFOLIO_VALUE_INR = 1_000_000           # Phase 5 — update before going live


# ---------------------------------------------------------------------------
# Concentration limits (BRD §3.5 FR-5.7).
# ---------------------------------------------------------------------------
MAX_OPEN_POSITIONS = 12                   # Phase 5: concentration flag (FR-5.7)
MAX_PER_SECTOR = 4                        # Phase 5: concentration flag (FR-5.7)
MAX_PEAD_ALLOCATION_PCT = 0.25            # Phase 5: concentration flag (FR-5.7)


# ---------------------------------------------------------------------------
# Gemini PDF parser (BRD §3.3 FR-3.4 / FR-3.5).
# ---------------------------------------------------------------------------
GEMINI_PRIMARY_MODEL = "gemini-2.5-flash-lite"   # TODO: phase 3
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"       # TODO: phase 3
GEMINI_MAX_RETRIES = 2                           # TODO: phase 3
