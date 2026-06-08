-- Phase 4 migration: scoring + ranking layer schema changes.
-- Apply once via Supabase SQL Editor. Idempotent — safe to re-run.
--
-- Adds:
--   1. metrics.avg_30d_turnover_cr — populated lazily by the Phase 4 filterer
--      the first time it touches a row (avoids re-touching Phase 3 enrichment
--      code while Phase 3 backfill is still running). TODO: phase 6 — move
--      this into the enricher so the filterer doesn't need to write.
--   2. fundamentals.listed_long_enough — cached result of the yfinance
--      "history goes back ≥730d" listing-age check (BRD §3.4 FR-4.3). Boolean
--      so the filterer can short-circuit without a second yfinance fetch on
--      subsequent runs.
--   3. rankings table — one row per (run_date, filing) for the top-N ranking.
--      Daily re-runs overwrite via UNIQUE(run_date, filing_id). Phase 5's
--      signal generator will SELECT FROM rankings WHERE run_date = today.
--
-- Pre-conditions:
--   - Phase 3 schema applied (filings, metrics, fundamentals, source_health).

------------------------------------------------------------------------------
-- 1. metrics: 30-day average turnover (₹ crore), lazy-populated.
------------------------------------------------------------------------------
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS avg_30d_turnover_cr NUMERIC;

------------------------------------------------------------------------------
-- 2. fundamentals: listing-age cache.
--    NULL = never checked. TRUE/FALSE = result of last yfinance probe.
--    Filterer treats NULL as "needs probe"; on yfinance fetch failure the
--    column stays NULL and the filing is INCLUDED (fail-open per Phase 4
--    decision — listing-age is a soft filter for borderline cases).
------------------------------------------------------------------------------
ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS listed_long_enough BOOLEAN;

------------------------------------------------------------------------------
-- 3. rankings: daily top-N output of the Phase 4 ranker.
--    UNIQUE(run_date, filing_id) — same filing can appear in multiple daily
--    rankings (the cohort changes each day as new filings land and old ones
--    age out of the 7-day window). One row per (run_date, filing).
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rankings (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,                       -- IST date of the rank_eod run
    filing_id       BIGINT NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    symbol_nse      TEXT NOT NULL,                       -- canonical NSE ticker post-dedup
    rank            INT NOT NULL,                        -- 1..N within run_date (1 = highest score)
    pead_score      NUMERIC NOT NULL,                    -- weighted composite, BRD §3.4 FR-4.1
    n_components    INT NOT NULL,                        -- 3..5 — how many z components contributed (3-5 because RANK_MIN_COMPONENTS=3)
    z_sue           NUMERIC,
    z_rev           NUMERIC,
    z_ear           NUMERIC,
    z_vol           NUMERIC,
    z_margin        NUMERIC,
    cohort_size     INT NOT NULL,                        -- cohort size used for z-normalization (pre-filter, post-dedup)
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, filing_id)
);

CREATE INDEX IF NOT EXISTS rankings_run_date_rank_idx
    ON rankings (run_date, rank);
CREATE INDEX IF NOT EXISTS rankings_filing_id_idx
    ON rankings (filing_id);
