# Business Requirements Document
## PEAD Scanner — Indian Stock Market Earnings Signal System

**Version:** 1.2
**Owner:** Aryan Agarwal
**Repository:** https://github.com/AryanAgarwal27/pead-scanner
**Status:** Draft for Implementation
**Target Stack:** Python 3.11+, GitHub Actions, Supabase (Postgres), Telegram Bot API, Gemini API (free tier)

**Changelog:**
- v1.2 — Added repository URL, Phase 0 bootstrap, explicit first-prompt instructions for Claude Code
- v1.1 — Added LLM-based filing parser (Gemini), signal tiering with confirmation checklist, position sizing tiers
- v1.0 — Initial draft

---

## 0. How to Use This Document with Claude Code

This BRD is structured for incremental delivery. Do **not** ask Claude Code to "build the whole thing." Instead:

1. Start a new Claude Code session inside the local clone of the repo at https://github.com/AryanAgarwal27/pead-scanner
2. Ensure `BRD.md` exists in the project root (this file)
3. For each phase, tell Claude Code: *"Implement Phase N from BRD.md. Stop after acceptance criteria are met. Do not start Phase N+1."*
4. Test, commit, push — only then move to the next phase
5. Use the "Acceptance Criteria" sections as the literal definition of done

Each phase is designed to produce a working, deployable system — not just code that compiles.

### 0.1 First Prompt for Claude Code (Phase 0 Bootstrap)

After running `claude` in the cloned repo, paste this **exact** prompt as your first message:

> Read `BRD.md` fully — it's our specification.
>
> This repo is at https://github.com/AryanAgarwal27/pead-scanner and is currently empty except for this BRD. The following GitHub Actions Secrets are already configured (do not write them anywhere): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`.
>
> Implement **Phase 0 only** (Repository Bootstrap) from BRD.md. Stop after all Phase 0 acceptance criteria are met. Do NOT start Phase 1.
>
> Before writing any code, respond with:
> 1. The exact list of files you plan to create with one-line purposes
> 2. The exact `pyproject.toml` dependencies
> 3. The smoke-test workflow YAML
> 4. Any clarifying questions
>
> Wait for my explicit approval before writing code.

### 0.2 Subsequent Phase Prompts

For Phase N > 0, use this template after starting a fresh Claude Code session:

> Read `BRD.md`. The repo already has Phases 0 through N-1 complete (verify by reading the existing code). Implement **Phase N only**. Stop after acceptance criteria are met.
>
> Before writing code, summarize: (1) files you'll create/modify, (2) any schema changes, (3) any new dependencies, (4) clarifying questions. Wait for my approval.

---

## 1. Project Overview

### 1.1 Problem Statement
Indian listed companies (~5,000) file quarterly results on rolling dates. Retail traders cannot manually monitor all filings or compute earnings-surprise metrics in real time. The result: missed opportunities to exploit the well-documented **Post-Earnings Announcement Drift (PEAD)** anomaly.

### 1.2 Solution
An automated system that:
- Detects quarterly result filings from NSE/BSE in near real-time
- Sends immediate notifications when results are filed (Day 0)
- Computes PEAD score after T+1 close using SUE, EAR, volume, and margin signals
- Generates ranked trade signals with entry, stop-loss, and target levels
- Tracks signal outcomes for continuous performance measurement

### 1.3 Primary User
Single individual retail trader (the document owner) operating in Indian equity markets, with NSE/BSE cash market access and a tolerance for 20–60 day holding periods.

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1)
- ✅ Sub-30-minute latency from result filing to Telegram alert
- ✅ Layered fallback across NSE → BSE → Trendlyne (system never goes fully blind)
- ✅ Daily PEAD-ranked top 20–25 signals delivered after T+1 close
- ✅ Position tracker with hit-rate and average-drift statistics
- ✅ Run entirely on free infrastructure (GitHub Actions + Supabase free tier)
- ✅ Operational visibility: heartbeat, error alerts, source-health dashboard

### 2.2 Non-Goals (explicitly out of scope for v1)
- ❌ Auto-trading / broker API integration (Zerodha Kite, etc.)
- ❌ Options or F&O signals
- ❌ Intraday strategies
- ❌ Multi-user support, authentication, billing
- ❌ Mobile app (Telegram is the UI)
- ❌ Pre-earnings prediction or analyst-estimate sourcing
- ❌ Backtesting framework (separate project)

---

## 3. Functional Requirements

### 3.1 Data Source Layer

**FR-1.1** System SHALL poll BSE corporate filings as primary source.
**FR-1.2** System SHALL poll NSE corporate filings as supplementary primary source (different filings may arrive at different exchanges first).
**FR-1.3** On failure of either primary, system SHALL fall back to Trendlyne announcement feed.
**FR-1.4** System SHALL deduplicate filings by `(symbol, quarter, filing_type)` so the same result isn't alerted twice.
**FR-1.5** System SHALL respect each source's rate limits with exponential backoff on 4xx/5xx responses.
**FR-1.6** Screener.in SHALL only be used for nightly fundamental enrichment, NEVER for real-time detection (etiquette + delay reasons).

### 3.2 Immediate Alert Layer (Day 0)

**FR-2.1** Within 30 minutes of a quarterly result filing, system SHALL send a Telegram message to the configured chat.
**FR-2.2** Alert SHALL include: company name, symbol, filing time, headline numbers (Revenue, PAT, EPS — if parseable), YoY % changes, and a link to the original filing PDF.
**FR-2.3** If headline numbers cannot be parsed within 5 minutes, system SHALL still send a "results filed, parsing in progress" alert.
**FR-2.4** Alerts SHALL be batched if more than 10 results land in a 5-minute window (avoid Telegram spam).

### 3.3 Enrichment Layer (T+0 evening + T+1 daytime)

**FR-3.1** For each new filing, system SHALL fetch 8 quarters of historical PAT, Revenue, and OPM from Screener.in cache.
**FR-3.2** System SHALL fetch OHLCV data for T-30 through T+1 from yfinance.
**FR-3.3** System SHALL compute the following metrics:
  - `SUE_proxy = (PAT_curr - PAT_yoy) / std_dev(last_8_qtr_PAT)`
  - `Rev_Growth_YoY = (Rev_curr / Rev_yoy) - 1`
  - `Vol_Spike = Vol_T+1 / avg(Vol_T-30 to T-1)`
  - `EAR = (Close_T+1 / Close_T-1 - 1) - (Nifty_T+1 / Nifty_T-1 - 1)`
  - `Margin_Delta = OPM_curr - OPM_yoy`

**FR-3.4** Filing PDFs SHALL be parsed by an LLM (Gemini API, free tier) to extract structured numbers (Revenue, PAT, EPS, OPM, exceptional items flag).
**FR-3.5** Parser SHALL follow a fallback chain: Gemini 2.5 Flash-Lite (primary) → Gemini 2.5 Flash (secondary, separate quota) → regex-based parser (last resort).
**FR-3.6** Parsed numbers SHALL be cached in the `filings` table by `filing_id` and never re-parsed unless explicitly invalidated.
**FR-3.7** LLM responses SHALL conform to a Pydantic JSON schema; parse failures SHALL trigger fallback rather than poisoning downstream metrics.
**FR-3.8** Filings flagged with `has_exceptional_items=True` by the LLM SHALL be marked for manual review and excluded from the top-25 ranking by default.

### 3.4 Scoring & Ranking Layer (T+1 close)

**FR-4.1** System SHALL compute a composite PEAD score using z-scored components:
```
pead_score = 0.35 * z(SUE_proxy)
           + 0.20 * z(Rev_Growth_YoY)
           + 0.25 * z(EAR)
           + 0.15 * z(Vol_Spike)
           + 0.05 * z(Margin_Delta)
```
**FR-4.2** Z-scores SHALL be computed within the cohort of all results filed in the trailing 7 days.
**FR-4.3** Hard filters SHALL be applied (stocks failing any are dropped):
  - Market cap ≥ ₹500 Cr
  - 30-day avg daily turnover ≥ ₹5 Cr
  - Not in current F&O ban list
  - Not in NSE ASM or GSM list
  - Listed for ≥ 2 years (need historical comparables)
**FR-4.4** Top 25 by `pead_score` SHALL be selected as the daily signal set.

### 3.5 Signal Generation Layer (T+1 close)

**FR-5.1** For each top-25 signal, system SHALL compute:
  - **Entry trigger**: high of T+1 daily candle
  - **Stop-loss**: low of T+1 daily candle (or -5% from entry, whichever is tighter)
  - **Target 1**: entry + 1.5 × (entry - stop) — book 50%
  - **Target 2**: trailing stop on 20-EMA, max hold 60 trading days
**FR-5.2** Signal message SHALL be sent via Telegram after market close (target: 4:30 PM IST).
**FR-5.3** Signal message format SHALL include: rank, score in σ, all 5 component metrics, entry/stop/T1/T2 levels, risk/reward ratio, tier, confirmation checklist results, and suggested position size.

**FR-5.4 Signal Tiering** — System SHALL assign each signal a tier based on PEAD score:

| Tier | PEAD Score (σ) | Default Action |
|---|---|---|
| SKIP | < 2.0 | Do not send signal |
| WATCH | 2.0 – 2.5 | Send signal, half position |
| TAKE | 2.5 – 3.0 | Send signal, full position |
| STRONG | > 3.0 | Send signal, full position + flag for review |

**FR-5.5 Confirmation Checklist** — For every TAKE/STRONG signal, system SHALL verify all 5 confirmations and report pass/fail count in the message:
  - **C1 Volume**: T+1 volume ≥ 2x 30-day average
  - **C2 Market Regime**: Nifty 50 close > Nifty 50 50-DMA
  - **C3 Consolidation**: T+1 move ≤ 12% from prior close (not over-extended)
  - **C4 Liquidity Headroom**: Intended position size ≤ 10% of 30-day avg daily turnover
  - **C5 No Corporate Action**: No split/bonus/dividend ex-date within ±5 trading days

**FR-5.6 Position Sizing Tiers** — System SHALL suggest position size based on score × confirmations:

| Score | Confirmations Passed | Suggested Position |
|---|---|---|
| ≥ 2.5σ | 5 of 5 | 1.0R |
| ≥ 2.5σ | 4 of 5 | 0.5R |
| ≥ 2.0σ | 5 of 5 | 0.5R |
| ≥ 2.0σ | < 5 | Skip |
| Any | C2 or C4 failed | Skip (non-negotiable) |

Where R = configurable risk per trade (default 1% of portfolio).

**FR-5.7 Concentration Limits** — System SHALL flag (not block) if:
  - Total open PEAD positions > 12
  - Same-sector positions > 4
  - Total PEAD capital allocation > 25% of portfolio

Flags appear in the daily summary; user makes the final call.

### 3.6 Position Tracker

**FR-6.1** Every signal sent SHALL be persisted with status `PENDING_ENTRY`.
**FR-6.2** Daily at 4:00 PM IST, system SHALL update each open signal:
  - Check if entry triggered (high ≥ entry_price within 5 trading days of signal; else expire)
  - Check if stop hit
  - Check if T1 hit
  - Update unrealized P&L
  - Apply trailing stop logic if past T1
**FR-6.3** Closed positions SHALL move to status `CLOSED` with final P&L recorded.
**FR-6.4** Daily summary Telegram message SHALL include: open positions count, today's P&L, MTD P&L, hit rate over last 50 signals.

### 3.7 Operational

**FR-7.1** Daily at 9:00 AM IST, system SHALL send a heartbeat message confirming all sources are reachable.
**FR-7.2** On any source failure during a polling cycle, system SHALL log the error and send a single alert (no spam).
**FR-7.3** All source-tier transitions (NSE→BSE→Trendlyne) SHALL be logged with timestamps for debugging.

---

## 4. Non-Functional Requirements

| ID | Category | Requirement |
|---|---|---|
| NFR-1 | Cost | Total infrastructure cost ≤ ₹0/month (free tiers only) |
| NFR-2 | Latency | Filing → Telegram alert ≤ 30 minutes (p95) |
| NFR-3 | Reliability | System uptime ≥ 95% during market hours (9:15 AM – 3:30 PM IST) |
| NFR-4 | Idempotency | Re-running any job SHALL NOT duplicate alerts or signals |
| NFR-5 | Observability | Every run SHALL log start, source used, records processed, errors |
| NFR-6 | Secrets | No tokens or credentials in source code; use GitHub Actions Secrets |
| NFR-7 | Data Retention | All filings, signals, positions retained indefinitely (small data) |
| NFR-8 | Testability | Core scoring functions SHALL have unit tests (≥80% coverage) |

---

## 5. Technical Architecture

### 5.1 High-Level Flow

```
        ┌─────────────────────────────────────────────────────┐
        │              GitHub Actions Scheduler                │
        │  ┌────────────┐  ┌────────────┐  ┌──────────────┐  │
        │  │ poll-15min │  │ enrich-eod │  │ track-daily  │  │
        │  └────────────┘  └────────────┘  └──────────────┘  │
        └──────────┬─────────────┬──────────────┬─────────────┘
                   v             v              v
        ┌──────────────────────────────────────────────────────┐
        │                  Source Adapters                     │
        │  NSE  →  BSE  →  Trendlyne (failover chain)          │
        │  Screener.in (nightly only)                          │
        │  yfinance (price data)                               │
        └──────────────────────┬───────────────────────────────┘
                               v
        ┌──────────────────────────────────────────────────────┐
        │              Supabase Postgres (free)                │
        │  filings | metrics | signals | positions | heartbeats│
        └──────────────────────┬───────────────────────────────┘
                               v
        ┌──────────────────────────────────────────────────────┐
        │                  Telegram Bot                        │
        │  alerts | signals | daily summary | errors           │
        └──────────────────────────────────────────────────────┘
```

### 5.2 Job Schedule (all times IST)

| Job | Cron (UTC) | Frequency | Purpose |
|---|---|---|---|
| `poll-filings` | `*/15 3-11 * * 1-5` | Every 15 min, market days | Detect new filings, send Day-0 alerts |
| `enrich-eod` | `0 12 * * 1-5` | 5:30 PM IST | Compute metrics for today's filings |
| `generate-signals` | `15 12 * * 1-5` | 5:45 PM IST | Rank top 25, send signal messages |
| `track-positions` | `30 10 * * 1-5` | 4:00 PM IST | Update open positions, daily summary |
| `heartbeat` | `30 3 * * 1-5` | 9:00 AM IST | Source health check |
| `screener-cache` | `0 18 * * *` | 11:30 PM IST daily | Refresh fundamentals cache |

### 5.3 Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11 | Ecosystem for nsepython, yfinance, pandas |
| Scheduler | GitHub Actions | Free, version-controlled, secrets-managed |
| Database | Supabase Postgres | 500MB free, real Postgres, easy SQL |
| Notifications | Telegram Bot API | Free, instant, supports formatting |
| LLM Parser | Gemini 2.5 Flash-Lite (primary), Flash (fallback) | Free tier, native PDF support, 1,000+250 RPD |
| Data libs | `nsepython`, `bsedata`, `yfinance`, `requests`, `pandas`, `google-generativeai`, `pydantic` | All free, maintained |
| Testing | `pytest`, `pytest-cov` | Standard |
| Linting | `ruff` | Fast, strict, modern |

---

## 6. Data Model

### 6.1 `filings` table
```sql
CREATE TABLE filings (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL,
    company_name          TEXT NOT NULL,
    quarter               TEXT NOT NULL,           -- e.g. 'Q3-FY26'
    filing_time           TIMESTAMPTZ NOT NULL,
    source                TEXT NOT NULL,           -- 'NSE' | 'BSE' | 'TRENDLYNE'
    filing_url            TEXT,
    revenue_cr            NUMERIC,
    pat_cr                NUMERIC,
    eps                   NUMERIC,
    opm_pct               NUMERIC,
    revenue_yoy_pct       NUMERIC,
    pat_yoy_pct           NUMERIC,
    is_consolidated       BOOLEAN,
    has_exceptional_items BOOLEAN,
    parser_used           TEXT,                    -- 'gemini-flash-lite' | 'gemini-flash' | 'regex'
    parser_confidence     TEXT,                    -- LLM's own caveats
    raw_payload           JSONB,
    parsed_at             TIMESTAMPTZ,
    alerted_at            TIMESTAMPTZ,
    UNIQUE (symbol, quarter)
);
```

### 6.2 `metrics` table
```sql
CREATE TABLE metrics (
    filing_id       BIGINT PRIMARY KEY REFERENCES filings(id),
    sue_proxy       NUMERIC,
    rev_growth_yoy  NUMERIC,
    vol_spike       NUMERIC,
    ear             NUMERIC,
    margin_delta    NUMERIC,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 6.3 `signals` table
```sql
CREATE TABLE signals (
    id              BIGSERIAL PRIMARY KEY,
    filing_id       BIGINT REFERENCES filings(id),
    symbol          TEXT NOT NULL,
    rank            INT NOT NULL,
    pead_score      NUMERIC NOT NULL,
    tier            TEXT NOT NULL,             -- 'WATCH' | 'TAKE' | 'STRONG'
    confirmations   JSONB NOT NULL,            -- {"C1": true, "C2": true, ...}
    confirmations_passed INT NOT NULL,
    suggested_size_r NUMERIC NOT NULL,         -- 0.5 or 1.0
    entry_price     NUMERIC NOT NULL,
    stop_price      NUMERIC NOT NULL,
    target1_price   NUMERIC NOT NULL,
    risk_reward     NUMERIC NOT NULL,
    signal_sent_at  TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING_ENTRY',
                    -- PENDING_ENTRY | ACTIVE | CLOSED_T1 | CLOSED_STOP | EXPIRED
    UNIQUE (filing_id)
);
```

### 6.4 `positions` table
```sql
CREATE TABLE positions (
    signal_id       BIGINT PRIMARY KEY REFERENCES signals(id),
    entry_filled_at DATE,
    exit_at         DATE,
    exit_price      NUMERIC,
    exit_reason     TEXT,     -- 'T1' | 'STOP' | 'TRAIL' | 'TIME_EXPIRY'
    pnl_pct         NUMERIC,
    max_favorable   NUMERIC,  -- best price reached during hold
    max_adverse     NUMERIC,  -- worst price during hold
    days_held       INT
);
```

### 6.5 `source_health` table
```sql
CREATE TABLE source_health (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL,
    ok              BOOLEAN NOT NULL,
    error_msg       TEXT,
    records_found   INT
);
```

---

## 7. Repository Structure

```
pead-scanner/
├── .github/
│   └── workflows/
│       ├── poll-filings.yml
│       ├── enrich-eod.yml
│       ├── generate-signals.yml
│       ├── track-positions.yml
│       └── heartbeat.yml
├── src/
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py              # FilingsSource Protocol
│   │   ├── nse.py
│   │   ├── bse.py
│   │   ├── trendlyne.py
│   │   ├── screener.py          # nightly only
│   │   ├── gemini_parser.py     # LLM PDF parsing with fallback chain
│   │   └── yfinance_adapter.py
│   ├── pipeline/
│   │   ├── detector.py          # orchestrates source failover
│   │   ├── enricher.py          # computes metrics
│   │   ├── scorer.py            # PEAD composite score
│   │   ├── filterer.py          # hard filters
│   │   ├── tiering.py           # signal tiering + confirmation checks
│   │   └── signaler.py          # entry/stop/target + position sizing
│   ├── tracking/
│   │   └── tracker.py           # position lifecycle
│   ├── notify/
│   │   ├── telegram.py
│   │   └── formatters.py        # message templates
│   ├── db/
│   │   ├── client.py
│   │   ├── schema.sql
│   │   └── models.py
│   ├── utils/
│   │   ├── time_utils.py        # IST handling
│   │   ├── retry.py
│   │   └── logging.py
│   └── config.py
├── jobs/
│   ├── poll_filings.py
│   ├── enrich_eod.py
│   ├── generate_signals.py
│   ├── track_positions.py
│   └── heartbeat.py
├── tests/
│   ├── test_scorer.py
│   ├── test_filterer.py
│   ├── test_signaler.py
│   └── fixtures/
├── pyproject.toml
├── README.md
└── BRD.md                       # this document
```

---

## 8. Phased Delivery Plan

### Phase 0 — Repository Bootstrap

**Goal:** Initialize the repo with proper Python project structure and verify all credentials are reachable from CI.

**Prerequisites (already done before starting Phase 0):**
- [x] GitHub repo created at https://github.com/AryanAgarwal27/pead-scanner
- [x] GitHub Actions Secrets configured: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`
- [x] Supabase project provisioned (URL + service-role key in hand)
- [x] Telegram bot created via @BotFather and chat ID obtained
- [x] Gemini API key obtained from Google AI Studio
- [x] Claude Code installed and authenticated locally

**Scope:**
- `.gitignore` (Python defaults + `.env`, `*.db`, `__pycache__/`, `.pytest_cache/`, `.venv/`, `.ruff_cache/`)
- `pyproject.toml` with project metadata, minimal initial dependencies (`requests`, `python-dotenv`, `supabase`, `pytest`, `pytest-cov`, `ruff`) — phase-specific libs added later
- `README.md` — project description, link to BRD.md, "work in progress" disclaimer, local setup steps
- `.env.example` — committed template showing required env var names (no values)
- `src/__init__.py`, `tests/__init__.py` — empty package markers
- `src/config.py` — loads env vars via `python-dotenv`, exposes the constants table from BRD §9.2
- `.github/workflows/smoke-test.yml` — runs on every push, asserts all 5 secrets are non-empty and that `pytest` exits cleanly (does NOT print secret values to logs)
- `tests/test_smoke.py` — trivial test ensuring `pytest` collects and runs
- Initial commit with message `Phase 0: bootstrap`, pushed to `main`

**Acceptance Criteria:**
- [ ] All files above committed and pushed to https://github.com/AryanAgarwal27/pead-scanner
- [ ] `.gitignore` correctly excludes `.env` and Python artifacts (verified by `git status` showing clean tree after creating a sample `.env`)
- [ ] `python -m pytest` runs locally and passes (at least the smoke test)
- [ ] `ruff check .` passes with zero issues
- [ ] Smoke-test GitHub Action passes on push — visible green check in Actions tab
- [ ] Smoke-test confirms all 5 secrets are accessible from CI (without exposing values)
- [ ] README has working "How to run locally" section, plus a prominent link to `BRD.md`
- [ ] No secrets, tokens, or credentials anywhere in committed code or history

---

### Phase 1 — Foundation (Day 0 alerts via BSE)

**Goal:** A working pipeline that detects today's BSE filings and Telegram-alerts them.

**Scope:**
- Repo scaffolding, `pyproject.toml`, `ruff` config
- Supabase setup + `filings` and `source_health` tables
- `BSESource` adapter
- `TelegramNotifier` with basic message formatter
- `poll_filings.py` job
- GitHub Actions workflow for 15-min polling
- README with setup instructions

**Acceptance Criteria:**
- [ ] Running `python jobs/poll_filings.py` locally pulls today's BSE results
- [ ] New filings inserted into Supabase (no duplicates on re-run)
- [ ] Telegram message received for each new filing
- [ ] GitHub Actions workflow runs on schedule and on `workflow_dispatch`
- [ ] All secrets via GitHub Actions Secrets, none in code
- [ ] `pytest` passes (even if just smoke tests)

---

### Phase 2 — Multi-Source Resilience

**Goal:** Add NSE and Trendlyne fallbacks behind a uniform interface.

**Scope:**
- Define `FilingsSource` Protocol in `sources/base.py`
- Implement `NSESource` and `TrendlyneSource`
- `detector.py` with failover chain: NSE → BSE → Trendlyne
- `source_health` logging on every run
- Heartbeat job
- Error-rate-limited alerting (one error message per source per hour max)

**Acceptance Criteria:**
- [ ] Killing NSE access (e.g., bad URL) causes automatic fallback to BSE, then Trendlyne
- [ ] Heartbeat message received daily at 9 AM IST
- [ ] `source_health` table populated per run
- [ ] No duplicate filings even when sources overlap
- [ ] Unit tests for failover logic

---

### Phase 3 — Enrichment Layer (with LLM PDF parsing)

**Goal:** Compute all 5 PEAD component metrics for each filing, with LLM-based PDF extraction.

**Scope:**
- `yfinance_adapter.py` for OHLCV pulls
- `screener.py` for fundamentals (nightly job)
- `gemini_parser.py` with Pydantic schema, Flash-Lite → Flash → regex fallback chain
- `enricher.py` computing SUE, Rev Growth, Vol Spike, EAR, Margin Delta
- `metrics` table writes
- Nightly `screener-cache` job
- T+1 EOD `enrich-eod` job
- Aggressive caching: never re-parse a filing PDF

**Acceptance Criteria:**
- [ ] For any filing in the last 7 days, querying `metrics` table returns all 5 values
- [ ] EAR correctly excess of Nifty over T-1 to T+1 window
- [ ] SUE uses 8 quarters of historical PAT
- [ ] Gemini parser extracts Revenue/PAT/EPS/OPM with ≥90% field accuracy on a 20-filing test set
- [ ] Rate-limit (HTTP 429) on Gemini triggers automatic fallback to Flash, then regex
- [ ] Filings with `has_exceptional_items=True` excluded from ranking
- [ ] Unit tests for each metric calculation with known inputs/outputs
- [ ] Screener.in cache refresh runs nightly without hitting their site during day

---

### Phase 4 — Scoring & Filtering

**Goal:** Produce a daily top-25 ranked list.

**Scope:**
- `scorer.py` with composite z-score logic
- `filterer.py` with all hard filters
- F&O ban list, ASM/GSM list ingestion (manual CSV upload acceptable in v1)
- Ranking written to a new column or view

**Acceptance Criteria:**
- [ ] After `enrich-eod`, top 25 ranking deterministically computed
- [ ] Hard filters dropping correct stocks (verified with sample data)
- [ ] Z-scores computed within trailing 7-day cohort, not all-time
- [ ] Edge case: fewer than 25 filings in cohort handled gracefully (return what we have)
- [ ] Unit tests with synthetic data

---

### Phase 5 — Signal Generation (with Tiering)

**Goal:** Send tiered PEAD trade signals with confirmation checklist after T+1 close.

**Scope:**
- `tiering.py` — applies tier rules (FR-5.4), runs 5 confirmations (FR-5.5), computes position size (FR-5.6)
- `signaler.py` — entry/stop/T1 levels using T+1 OHLC
- Signal message formatter (markdown, with emojis for at-a-glance scanning)
- `generate-signals` job
- `signals` table writes with tier + confirmation results

**Acceptance Criteria:**
- [ ] Daily at 5:45 PM IST, top-25 Telegram signal messages sent — but only those passing tier rules
- [ ] Each message includes all required fields per FR-5.3 (rank, score, metrics, levels, tier, confirmations, sizing)
- [ ] Hard skip rules enforced: C2 (regime) or C4 (liquidity) failed = no signal sent
- [ ] Risk/reward ratio correctly calculated
- [ ] Concentration flags appear in daily summary when limits breached
- [ ] No duplicate signals if job retries
- [ ] Signal status starts as `PENDING_ENTRY`
- [ ] Unit tests for tiering decision matrix with synthetic inputs

---

### Phase 6 — Position Tracker

**Goal:** Close the feedback loop with P&L tracking.

**Scope:**
- `tracker.py` with state transitions
- Daily 4:00 PM IST update job
- Daily summary message
- Hit rate, avg drift calculations
- Trailing stop logic past T1

**Acceptance Criteria:**
- [ ] All signal state transitions covered: `PENDING_ENTRY → ACTIVE → CLOSED_*`
- [ ] Signals not triggered within 5 trading days marked `EXPIRED`
- [ ] Daily summary message arrives at 4:30 PM IST showing: open count, today's P&L, MTD, hit rate
- [ ] `positions` table reflects accurate exit data
- [ ] Unit tests for state machine

---

## 9. Configuration & Secrets

### 9.1 GitHub Actions Secrets (required)
```
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
SUPABASE_URL
SUPABASE_KEY
GEMINI_API_KEY
```

### 9.2 `config.py` (committed, non-secret)
```python
# Source priority
SOURCES_ORDER = ["NSE", "BSE", "TRENDLYNE"]

# Filters
MIN_MARKET_CAP_CR = 500
MIN_DAILY_TURNOVER_CR = 5
MIN_LISTING_YEARS = 2

# Scoring weights
WEIGHTS = {
    "sue":    0.35,
    "rev":    0.20,
    "ear":    0.25,
    "vol":    0.15,
    "margin": 0.05,
}

# Signal params
TOP_N = 25
STOP_PCT_CAP = 0.05
TARGET_R_MULTIPLE = 1.5
ENTRY_WINDOW_DAYS = 5
MAX_HOLD_DAYS = 60
TRAILING_EMA = 20

# Cohort window for z-score normalization
COHORT_WINDOW_DAYS = 7

# Signal tiering (Section 3.5)
TIER_THRESHOLDS = {
    "SKIP":   (0.0, 2.0),
    "WATCH":  (2.0, 2.5),
    "TAKE":   (2.5, 3.0),
    "STRONG": (3.0, 99.0),
}

# Confirmation thresholds
CONF_VOLUME_MULTIPLIER = 2.0
CONF_MAX_EXTENSION_PCT = 0.12
CONF_MAX_LIQUIDITY_PCT = 0.10
CONF_CORPORATE_ACTION_WINDOW_DAYS = 5

# Position sizing
DEFAULT_RISK_PER_TRADE_PCT = 0.01  # 1% of portfolio
PORTFOLIO_VALUE_INR = 1_000_000     # update this

# Concentration limits
MAX_OPEN_POSITIONS = 12
MAX_PER_SECTOR = 4
MAX_PEAD_ALLOCATION_PCT = 0.25

# Gemini parser
GEMINI_PRIMARY_MODEL = "gemini-2.5-flash-lite"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
GEMINI_MAX_RETRIES = 2
```

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| NSE blocks scraper | High | High | BSE primary fallback, Trendlyne secondary |
| `nsepython` library breaks after NSE site change | High | Med | Pinned version, integration test in CI |
| GitHub Actions cron drift | Med | Low | Idempotent jobs, deduplication keys |
| Telegram bot rate limit hit | Low | Med | Message batching, max 30/sec ceiling |
| Supabase free tier exceeded | Low | Low | Data is small (< 10MB/year est.) |
| Score weights overfit / underperform | High | High | Treat v1 weights as defaults; backtest in v2 |
| Stock split/bonus breaks SUE calc | Med | Med | Use adjusted prices from yfinance; flag corporate actions |
| Filing arrives with no parseable numbers (PDF only) | High | Med | LLM parser handles most cases; regex fallback; manual review alert |
| Gemini free tier RPD exceeded during peak earnings | Med | Med | Two-model bucket (Flash-Lite + Flash = 1,250 RPD); cache aggressively; regex fallback |
| Gemini extracts wrong numbers (hallucination) | Med | High | Pydantic schema enforcement; cross-check vs Screener.in fundamentals; manual review flag for outliers |
| Threshold tuning wrong for live regime | High | Med | Tiered position sizing limits downside; paper-trade first quarter |

---

## 11. Glossary

- **PEAD** — Post-Earnings Announcement Drift; tendency of stocks to drift in the direction of earnings surprise for weeks after a result
- **SUE** — Standardized Unexpected Earnings; (actual − expected) / std-dev of past surprises
- **EAR** — Earnings Announcement Return; 3-day return around announcement, in excess of benchmark
- **OPM** — Operating Profit Margin
- **PAT** — Profit After Tax
- **YoY / QoQ** — Year-over-Year / Quarter-over-Quarter
- **F&O Ban** — NSE list of stocks restricted from new futures/options positions
- **ASM / GSM** — Additional / Graded Surveillance Measures (NSE risk lists)
- **IST** — India Standard Time (UTC+5:30)
- **T+0 / T+1** — Day of filing / next trading day

---

## 12. Out-of-Scope Items Captured for v2+

Backlog of ideas explicitly deferred:
- Backtesting framework using historical Bhavcopy data
- Web dashboard (Streamlit or Next.js)
- Multi-user with auth
- Broker API integration (Zerodha Kite Connect)
- Sector-relative ranking (not just absolute)
- Sentiment overlay from earnings call transcripts
- Walk-forward weight re-optimization
- Short-side signals for negative surprises
- Options-based PEAD plays (long calls on top tier)

---

## 13. Sign-off

| Role | Name | Date |
|---|---|---|
| Product Owner | [you] | |
| Implementation | Claude Code | |

---

*End of document.*
