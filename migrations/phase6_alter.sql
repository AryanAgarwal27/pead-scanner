-- Phase 6 migration: position-tracker schema.
-- Apply once via Supabase SQL Editor. Idempotent — safe to re-run.
--
-- Adds:
--   1. positions table — one row per signal, written by the Phase 6 tracker
--      (jobs/track_positions.py → src.pipeline.tracker). Records the simulated
--      lifecycle of each sent signal against real daily price bars (BRD §6.4).
--
-- Design notes:
--   * signal_id PK / FK → signals(id): exactly one position row per signal.
--     The tracker UPSERTs (on_conflict=signal_id), so daily re-runs overwrite
--     the row with the latest replay rather than appending.
--   * exit_reason ∈ {'STOP','TRAIL','TIME_EXPIRY'} for closed rows (the BRD's
--     'T1' value is not a FINAL reason under the partial-exit model: T1 books
--     50% and the remainder always exits via TRAIL/TIME_EXPIRY — or the whole
--     position via STOP pre-T1). t1_hit_at records when/if the 50% was booked.
--   * pnl_pct is BLENDED for a position that reached T1: 0.5×(T1/entry−1) +
--     0.5×(exit_close/entry−1). A pre-T1 stop realizes the full (stop/entry−1).
--   * signals.status (plain TEXT, no CHECK constraint) gains two new terminal
--     values written by the tracker: CLOSED_TRAIL and CLOSED_TIME_EXPIRY,
--     alongside the existing PENDING_ENTRY/ACTIVE/CLOSED_STOP/EXPIRED. No DDL
--     needed for that — documented here for the record.
--
-- Pre-conditions:
--   - Phase 5 schema applied (signals table present).

------------------------------------------------------------------------------
-- 1. positions: simulated lifecycle of each sent signal (BRD §6.4).
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    signal_id       BIGINT PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    entry_filled_at DATE,                 -- first bar high ≥ entry within the window
    t1_hit_at       DATE,                 -- bar Target 1 booked (50%); NULL if never reached
    exit_at         DATE,                 -- final close-out bar; NULL while still open
    exit_price      NUMERIC,              -- stop_price (STOP) or remainder exit close (TRAIL/TIME)
    exit_reason     TEXT,                 -- 'STOP' | 'TRAIL' | 'TIME_EXPIRY'
    pnl_pct         NUMERIC,              -- realized (closed) or unrealized (active); blended post-T1
    max_favorable   NUMERIC,              -- best (high/entry − 1) reached during the hold
    max_adverse     NUMERIC,              -- worst (low/entry − 1) during the hold
    days_held       INT,                  -- trading days from entry (0 on entry day)
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS positions_exit_at_idx
    ON positions (exit_at DESC);
