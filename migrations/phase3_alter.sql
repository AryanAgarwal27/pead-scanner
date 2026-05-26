-- Phase 3 migration: enrichment layer schema changes.
-- Apply once via Supabase SQL Editor. Idempotent — safe to re-run.
--
-- Adds:
--   1. Two YoY-comparator columns to `filings` (the LLM parser will populate
--      these when the filing PDF shows the prior-year-same-quarter values).
--   2. New `fundamentals` table — nightly Screener.in cache, NSE-ticker-only
--      (Option A; see BRD §6.6). BSE-only stocks are intentionally absent and
--      handled via the on_screener=false code path in src/pipeline/enricher.py.
--
-- Pre-conditions (must already exist in the database):
--   - `filings` table with the Phase 0/1/2 columns AND the financial columns
--     from BRD §6.1 (revenue_cr, pat_cr, eps, opm_pct, is_consolidated,
--     has_exceptional_items, parser_used, parser_confidence, raw_payload,
--     parsed_at, alerted_at).
--   - `metrics` table per BRD §6.2.
--   - `source_health` table per BRD §6.5.

------------------------------------------------------------------------------
-- 1. filings: add YoY comparator columns.
--    Most quarterly filings show the prior-year-same-quarter comparator on
--    the same page; capturing it during the Gemini parse means Rev_Growth_YoY
--    and Margin_Delta can be computed even when Screener cache is missing.
------------------------------------------------------------------------------
ALTER TABLE filings ADD COLUMN IF NOT EXISTS revenue_yoy_pct NUMERIC;
ALTER TABLE filings ADD COLUMN IF NOT EXISTS pat_yoy_pct     NUMERIC;

------------------------------------------------------------------------------
-- 2. Indexes for the enricher's batch-select patterns.
--    - filings_parsed_at_null_idx: "find filings still needing LLM parsing"
--      (partial index stays small — rows leave the index once parsed).
--    - filings_parsed_at_idx: "find parsed filings ready for metric
--      computation" within the 14-day enrichment window.
------------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS filings_parsed_at_null_idx
    ON filings (filing_time DESC)
    WHERE parsed_at IS NULL;

CREATE INDEX IF NOT EXISTS filings_parsed_at_idx
    ON filings (parsed_at);

------------------------------------------------------------------------------
-- 3. fundamentals: nightly Screener.in cache (BRD §6.6).
--    NSE-ticker-only PK (Option A). BSE-only stocks are NOT stored here.
--    on_screener=false rows are a negative cache; last_404_at gives them a
--    30-day TTL so newly-Screener-indexed stocks get re-checked periodically.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol           TEXT PRIMARY KEY,         -- always an NSE ticker (e.g. 'HDFCBANK')
    company_name     TEXT,
    market_cap_cr    NUMERIC,
    sector           TEXT,
    quarterly_pat    JSONB,                    -- [{"quarter":"Q3-FY26","value":123.4}, ...] newest first, up to 8 items
    quarterly_rev    JSONB,
    quarterly_opm    JSONB,
    on_screener      BOOLEAN NOT NULL,         -- false = Screener 404 for this ticker; respect last_404_at TTL before retry
    last_404_at      TIMESTAMPTZ,              -- when we last received 404 (drives 30-day negative-cache TTL)
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for the screener-cache job's "what's stale?" batch select.
CREATE INDEX IF NOT EXISTS fundamentals_fetched_at_idx
    ON fundamentals (fetched_at);
