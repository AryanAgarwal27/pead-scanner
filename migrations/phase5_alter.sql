-- Phase 5 migration: signal-generation layer schema changes.
-- Apply once via Supabase SQL Editor. Idempotent — safe to re-run.
--
-- Adds:
--   1. signals table — one row per filing that clears tier + sizing rules and
--      is actually sent to Telegram (BRD §3.5, §6.3). The Phase 5 signal
--      generator (jobs/generate_signals.py → src.pipeline.signaler) reads the
--      day's rankings, computes entry/stop/T1 levels, runs the tiering +
--      confirmation checklist, and inserts the survivors here.
--
-- Design notes:
--   * UNIQUE(filing_id) — a filing is signalled AT MOST ONCE, ever. A filing
--     can appear in multiple daily rankings (the 7-day cohort re-forms each
--     day), but PEAD acts on it a single time. The generator skips any filing
--     that already has a signal row, so re-runs (CI retry, same-day reruns)
--     never double-send. This mirrors the filings.alerted_at idempotency idiom
--     from Phase 1's poll_filings.
--   * Only SENT signals are persisted. SKIP-tier (<2.0σ) and sizing-skipped
--     candidates (≥2.0σ but <5 confirmations, or C2/C4 hard-fail) are logged
--     and dropped — they never reach this table. So `signals` == the set of
--     trades the operator was actually told to consider.
--   * No target2_price column: T+1's "Target 2" (FR-5.1) is a TRAILING stop on
--     the 20-EMA with a 60-day max hold, not a static price knowable at signal
--     time. The signal message renders it as a descriptor; the trailing logic
--     itself is Phase 6 (tracker) territory.
--   * status default PENDING_ENTRY (BRD §6.3 / §8 Phase 5 acceptance). The
--     ACTIVE/CLOSED_*/EXPIRED transitions are written by Phase 6's tracker.
--
-- Pre-conditions:
--   - Phase 4 schema applied (filings, metrics, fundamentals, rankings).

------------------------------------------------------------------------------
-- 1. signals: tiered trade signals, one per filing (BRD §6.3).
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id                   BIGSERIAL PRIMARY KEY,
    filing_id            BIGINT REFERENCES filings(id) ON DELETE CASCADE,
    symbol               TEXT NOT NULL,                 -- canonical NSE ticker
    rank                 INT NOT NULL,                  -- rank within the run's top-N
    pead_score           NUMERIC NOT NULL,              -- composite z-score (σ), from rankings
    tier                 TEXT NOT NULL,                 -- 'WATCH' | 'TAKE' | 'STRONG'
    confirmations        JSONB NOT NULL,                -- {"C1": true, "C2": true, ...}
    confirmations_passed INT NOT NULL,
    suggested_size_r     NUMERIC NOT NULL,              -- 0.5 or 1.0 (R = risk per trade)
    entry_price          NUMERIC NOT NULL,              -- high of T+1 candle
    stop_price           NUMERIC NOT NULL,              -- tighter of (T+1 low, -5% from entry)
    target1_price        NUMERIC NOT NULL,              -- entry + 1.5 × (entry - stop)
    risk_reward          NUMERIC NOT NULL,              -- (target1 - entry) / (entry - stop)
    signal_sent_at       TIMESTAMPTZ NOT NULL,
    status               TEXT NOT NULL DEFAULT 'PENDING_ENTRY',
                         -- PENDING_ENTRY | ACTIVE | CLOSED_T1 | CLOSED_STOP | EXPIRED
    UNIQUE (filing_id)
);

CREATE INDEX IF NOT EXISTS signals_status_idx
    ON signals (status);
CREATE INDEX IF NOT EXISTS signals_sent_at_idx
    ON signals (signal_sent_at DESC);
