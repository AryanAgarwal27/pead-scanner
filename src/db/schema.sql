-- Canonical Supabase schema for pead-scanner (Phases 0–4).
-- Idempotent — safe to apply to an existing database.
-- This file mirrors BRD §6 and is the single source of truth in code.
--
-- For applying phase deltas to a DB that already has the prior-phase tables,
-- use migrations/phaseN_alter.sql — those files contain ONLY the deltas.
--
-- Future phases (5+) will add: signals (§6.3), positions (§6.4).

------------------------------------------------------------------------------
-- §6.1 filings — one row per quarterly result filing (deduped by symbol+quarter)
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filings (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL,
    company_name          TEXT NOT NULL,
    quarter               TEXT NOT NULL,             -- e.g. 'Q3-FY26'
    filing_time           TIMESTAMPTZ NOT NULL,
    source                TEXT NOT NULL,             -- 'NSE' | 'BSE' | 'TRENDLYNE'
    filing_url            TEXT,
    revenue_cr            NUMERIC,
    pat_cr                NUMERIC,
    eps                   NUMERIC,
    opm_pct               NUMERIC,
    revenue_yoy_pct       NUMERIC,                   -- Phase 3
    pat_yoy_pct           NUMERIC,                   -- Phase 3
    is_consolidated       BOOLEAN,
    has_exceptional_items BOOLEAN,
    parser_used           TEXT,                      -- 'gemini-flash-lite' | 'gemini-flash' | 'regex'
    parser_confidence     TEXT,                      -- 'high' | 'medium' | 'low' | 'failed'
    raw_payload           JSONB,
    parsed_at             TIMESTAMPTZ,               -- non-null = LLM parse cached (FR-3.6 "never re-parse")
    alerted_at            TIMESTAMPTZ,
    UNIQUE (symbol, quarter)
);

CREATE INDEX IF NOT EXISTS filings_parsed_at_null_idx
    ON filings (filing_time DESC)
    WHERE parsed_at IS NULL;
CREATE INDEX IF NOT EXISTS filings_parsed_at_idx
    ON filings (parsed_at);

------------------------------------------------------------------------------
-- §6.2 metrics — one row per filing with the 5 PEAD component metrics
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics (
    filing_id           BIGINT PRIMARY KEY REFERENCES filings(id) ON DELETE CASCADE,
    sue_proxy           NUMERIC,
    rev_growth_yoy      NUMERIC,
    vol_spike           NUMERIC,
    ear                 NUMERIC,
    margin_delta        NUMERIC,
    avg_30d_turnover_cr NUMERIC,                  -- Phase 4: lazy-populated by filterer (TODO: phase 6 — move to enricher)
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

------------------------------------------------------------------------------
-- §6.5 source_health — per-source observability per polling/heartbeat run
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_health (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL,                -- 'NSE'|'BSE'|'TRENDLYNE'|'POLL'|'<NAME>-heartbeat'
    ok              BOOLEAN NOT NULL,
    error_msg       TEXT,                         -- also encodes 'latency_ms=N', '[ALERTED] …', 'dry_run' markers
    records_found   INT
);

CREATE INDEX IF NOT EXISTS source_health_run_at_idx
    ON source_health (run_at DESC);
CREATE INDEX IF NOT EXISTS source_health_source_run_at_idx
    ON source_health (source, run_at DESC);

------------------------------------------------------------------------------
-- §6.6 fundamentals — nightly Screener.in cache (Phase 3, Option A).
--   See BRD §6.6 for the full rationale on NSE-ticker-only PK.
--   on_screener=false rows are a negative cache; last_404_at drives a 30-day
--   TTL before the screener-cache job re-checks Screener for the ticker.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol             TEXT PRIMARY KEY,             -- NSE ticker, e.g. 'HDFCBANK'
    company_name       TEXT,
    market_cap_cr      NUMERIC,
    sector             TEXT,
    quarterly_pat      JSONB,                        -- [{"quarter":"Q3-FY26","value":123.4}, ...] newest first, up to 8
    quarterly_rev      JSONB,
    quarterly_opm      JSONB,
    on_screener        BOOLEAN NOT NULL,
    last_404_at        TIMESTAMPTZ,
    listed_long_enough BOOLEAN,                      -- Phase 4: cached yfinance history-≥730d check; NULL = never probed
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS fundamentals_fetched_at_idx
    ON fundamentals (fetched_at);

------------------------------------------------------------------------------
-- Phase 4: rankings — daily top-N output of jobs/rank_eod.py.
--   See migrations/phase4_alter.sql for the full design rationale.
--   UNIQUE(run_date, filing_id) — daily re-runs overwrite that day's rows.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rankings (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    filing_id       BIGINT NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    symbol_nse      TEXT NOT NULL,
    rank            INT NOT NULL,
    pead_score      NUMERIC NOT NULL,
    n_components    INT NOT NULL,
    z_sue           NUMERIC,
    z_rev           NUMERIC,
    z_ear           NUMERIC,
    z_vol           NUMERIC,
    z_margin        NUMERIC,
    cohort_size     INT NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, filing_id)
);

CREATE INDEX IF NOT EXISTS rankings_run_date_rank_idx
    ON rankings (run_date, rank);
CREATE INDEX IF NOT EXISTS rankings_filing_id_idx
    ON rankings (filing_id);
