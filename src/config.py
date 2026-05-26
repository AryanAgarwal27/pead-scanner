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
# Hard filters (BRD §3.4 FR-4.3).
# ---------------------------------------------------------------------------
MIN_MARKET_CAP_CR = 500          # TODO: phase 4
MIN_DAILY_TURNOVER_CR = 5        # TODO: phase 4
MIN_LISTING_YEARS = 2            # TODO: phase 4


# ---------------------------------------------------------------------------
# Composite PEAD scoring weights (BRD §3.4 FR-4.1).
# ---------------------------------------------------------------------------
WEIGHTS = {                      # TODO: phase 4
    "sue":    0.35,
    "rev":    0.20,
    "ear":    0.25,
    "vol":    0.15,
    "margin": 0.05,
}


# ---------------------------------------------------------------------------
# Signal generation parameters (BRD §3.5).
# ---------------------------------------------------------------------------
TOP_N = 25                       # TODO: phase 4/5
STOP_PCT_CAP = 0.05              # TODO: phase 5
TARGET_R_MULTIPLE = 1.5          # TODO: phase 5
ENTRY_WINDOW_DAYS = 5            # TODO: phase 5/6
MAX_HOLD_DAYS = 60               # TODO: phase 5/6
TRAILING_EMA = 20                # TODO: phase 6


# ---------------------------------------------------------------------------
# Z-score cohort window (BRD §3.4 FR-4.2).
# ---------------------------------------------------------------------------
COHORT_WINDOW_DAYS = 7           # TODO: phase 4


# ---------------------------------------------------------------------------
# Signal tiering thresholds (BRD §3.5 FR-5.4).
# ---------------------------------------------------------------------------
TIER_THRESHOLDS = {              # TODO: phase 5
    "SKIP":   (0.0, 2.0),
    "WATCH":  (2.0, 2.5),
    "TAKE":   (2.5, 3.0),
    "STRONG": (3.0, 99.0),
}


# ---------------------------------------------------------------------------
# Confirmation checklist thresholds (BRD §3.5 FR-5.5).
# ---------------------------------------------------------------------------
CONF_VOLUME_MULTIPLIER = 2.0              # TODO: phase 5
CONF_MAX_EXTENSION_PCT = 0.12             # TODO: phase 5
CONF_MAX_LIQUIDITY_PCT = 0.10             # TODO: phase 5
CONF_CORPORATE_ACTION_WINDOW_DAYS = 5     # TODO: phase 5


# ---------------------------------------------------------------------------
# Position sizing (BRD §3.5 FR-5.6).
# ---------------------------------------------------------------------------
DEFAULT_RISK_PER_TRADE_PCT = 0.01         # TODO: phase 5
PORTFOLIO_VALUE_INR = 1_000_000           # TODO: phase 5 — update before going live


# ---------------------------------------------------------------------------
# Concentration limits (BRD §3.5 FR-5.7).
# ---------------------------------------------------------------------------
MAX_OPEN_POSITIONS = 12                   # TODO: phase 6
MAX_PER_SECTOR = 4                        # TODO: phase 6
MAX_PEAD_ALLOCATION_PCT = 0.25            # TODO: phase 6


# ---------------------------------------------------------------------------
# Gemini PDF parser (BRD §3.3 FR-3.4 / FR-3.5).
# ---------------------------------------------------------------------------
GEMINI_PRIMARY_MODEL = "gemini-2.5-flash-lite"   # TODO: phase 3
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"       # TODO: phase 3
GEMINI_MAX_RETRIES = 2                           # TODO: phase 3
