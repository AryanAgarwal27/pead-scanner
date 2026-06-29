# Claude Code — Session Primer for pead-scanner

This file orients a new Claude Code session to where this project is, how it's
built, and what's next. Read it first before doing anything in the repo.

---

## 0. Files to give every new Claude Code session

Three sources together give the full picture. The first two auto-load if you're
running Claude Code in this directory; share them manually otherwise.

| File | What it is | When to share |
|---|---|---|
| **`CLAUDE.md`** (this file) | Session orientation, current phase, what to do next | Always |
| **`BRD.md`** | Full Business Requirements Document — *the* specification. Goals, architecture, data model, all 7 phases with acceptance criteria | Always |
| **`README.md`** | Local setup, manual-run commands, per-phase first-time-setup steps | When operating jobs or onboarding |
| `memory/MEMORY.md` + the files it links to | Persistent per-project feedback (commit-trailer policy, push policy, etc.) | Auto-loaded by Claude Code's memory system — no need to share manually |

If you're starting a Claude Code session for this repo, the first message should
typically be:

> Read `CLAUDE.md` and `BRD.md`. Implement Phase N from BRD §8. Stop after
> Phase N acceptance criteria are met. Do NOT start Phase N+1.

The phase-prompt template comes from BRD §0.2 — follow it verbatim. Every phase
expects a plan response first, then approval, then code.

---

## 1. Project at a glance

PEAD Scanner — Indian-equities Post-Earnings Announcement Drift signal system.
Polls NSE/BSE for quarterly result filings, parses them with Gemini, computes a
composite z-score, ranks the top-25 candidates daily, and sends Telegram alerts.

Free-tier-only infrastructure: GitHub Actions + Supabase Postgres + Telegram Bot
API + Gemini API.

Single retail user (the repo owner). Not a multi-tenant product.

For the actual specification — goals, non-goals, FRs/NFRs, data model, risks —
read [BRD.md](BRD.md). Don't try to summarize it from this file; the BRD is the
source of truth.

---

## 2. Current state

**Last completed:** Phase 6 — Position Tracker (commit `a46c812`, pushed).
`src/pipeline/tracker.py` replays each sent signal against real daily OHLC
(entry breakout → 50% at T1 → trail remaining 50% on the 20-EMA → blended P&L),
writes the `positions` table, updates `signals.status`, and sends a daily
summary (open count, today's/MTD P&L, hit rate over last 50). Verified on real
data (AIAENG replay: entry 2026-05-29, T1 booked 2026-06-22, ACTIVE trailing,
+8.8% blended). ⚠️ **DEPLOY STEP PENDING:** `migrations/phase6_alter.sql` is
NOT yet applied to Supabase — apply it in the SQL Editor before the
`track-positions` cron runs, or the job errors on the missing `positions` table.

**Parser-confidence (the old ~94% drop):** resolved by **Fix B** (commit
`7d3c25a`) — `column_period` validation + a consolidated/standalone divergence
guard. Gemini now recovers Q4 "year ended" filings (REFEX/MARKSANS verified).
The backlog re-enrichment is a multi-day grind throttled by Gemini free-tier
429s; a **TEMPORARY** `reparse-backfill` workflow (commit `ff05e28`, daily
03:30 UTC) drains it — **delete that workflow** once `enrich_eod.py --reparse
--window-days 60` reports 0 candidates. BSE filings with expired `AttachLive`
PDF URLs 404 and stay failed (accepted as permanent data loss).

**Signals status:** plumbing (generate→Telegram→persist) verified end-to-end.
The current backfill contains NO genuine ≥2.0σ event (max 1.22σ), so no real
signal has fired — that's expected; real signals arrive organically via the
daily cron. `PORTFOLIO_VALUE_INR` is set to the real ₹1 lakh (commit `c497081`).

**Migrations:** `phase4_alter.sql` + `phase5_alter.sql` are applied;
`phase6_alter.sql` (positions table) is **NOT yet applied** (see above).
**Phase 4 fixes:** `_select_cohort` is now `run_date`-anchored (`fa0f118`) AND
paginated past the 1000-row PostgREST cap (`39cba68`) so dense result-weeks
aren't truncated.

**Phases complete (verify by reading code, not just trusting this file):**

| Phase | Scope | Acceptance reference |
|---|---|---|
| 0 | Repo bootstrap, smoke-test CI, secrets wired | BRD §8 Phase 0 |
| 1 | BSE Day-0 alerts → Supabase + Telegram | BRD §8 Phase 1 |
| 2 | Multi-source resilience (NSE+BSE+Trendlyne failover), heartbeat | BRD §8 Phase 2 |
| 3 | Enrichment — Gemini parser, Screener cache, yfinance, 5 PEAD metrics | BRD §8 Phase 3 |
| 4 | Scoring, hard filters, cross-source dedup, top-25 ranking | BRD §8 Phase 4 |
| 5 | Signal generation with tiering, confirmations, position sizing | BRD §8 Phase 5 |
| 6 | Position tracker — lifecycle sim, P&L/hit-rate, trailing stop | BRD §8 Phase 6 |

**Next:** (1) Apply `phase6_alter.sql` to Supabase. (2) Let the
`reparse-backfill` workflow drain the late-May backlog, then delete it. (3) The
system is feature-complete per the BRD — ongoing operation is the daily crons
(poll → enrich → rank → signals → track) producing organic signals.

---

## 3. How this project is built — the phased workflow

Follow BRD §0 (How to Use This Document with Claude Code). The rules in short:

1. **One phase at a time.** Never start phase N+1 while phase N is open. Even
   "helper code phase N+1 will need" is out of scope — don't pre-build.
2. **Plan first, code after approval.** Every phase starts with a written plan:
   files to create/modify, schema changes, edge cases, clarifying questions.
   The user approves, then code happens.
3. **Acceptance criteria are the definition of done.** BRD §8 lists them per
   phase. Don't declare phase N complete until every box is checked.
4. **Show diff before committing.** The user pushes manually
   (see `memory/project_push_policy.md`).
5. **Local conventions** the user has stated:
   - No `Co-Authored-By` trailer on commits (see
     `memory/feedback_no_ai_coauthor_trailer.md`).
   - Direct `git push origin main` is allowed for this repo
     (see `memory/project_push_policy.md`).

---

## 4. Repo layout

The canonical layout is in [BRD §7](BRD.md#7-repository-structure). Don't
duplicate it here — read the BRD.

A few high-traffic landmarks worth knowing:

- `src/pipeline/` — the per-phase data flow modules (detector, enricher,
  scorer, filterer, ranker, plus the upcoming Phase 5 signaler).
- `src/sources/` — exchange + data-vendor adapters (NSE, BSE, Trendlyne,
  Screener, Gemini parser, yfinance, BSE↔NSE symbol map).
- `src/db/schema.sql` — single canonical schema, mirrors BRD §6. Apply
  `migrations/phaseN_alter.sql` to bring an existing DB up to date.
- `jobs/` — GitHub Actions entry points (one Python file per cron).
- `tests/` — `pytest`, all phases. New phases add their own test files.
- `.github/workflows/` — one YAML per scheduled job; cron times in UTC, all
  derived from the IST table in BRD §5.2.

---

## 5. Tech + conventions at a glance

Stack details are in [BRD §5.3](BRD.md#53-tech-stack); don't re-explain that
table here. A handful of code-level conventions worth knowing before editing:

- **Python 3.11+**, ruff for lint (`ruff check .` must pass), pytest for tests.
- **No secrets in code.** Everything via env vars (`.env` locally, GH Actions
  Secrets in CI). See `.env.example`.
- **Idempotency is mandatory** — every job must be safe to re-run. Most jobs
  enforce this via unique constraints on the relevant Supabase table.
- **Logging via `src.utils.logging.get_logger`** — single line per event,
  stdout-only, deliberately greppable. `propagate=False` (means pytest `caplog`
  doesn't see them; use `capsys` and rebind handlers if testing log output).
- **Time handling:** IST is the user-facing zone; UTC is what we store. See
  `src/utils/time_utils.py`.
- **Supabase NUMERIC** can come back as `str` or `float` depending on
  supabase-py version — always coerce defensively (see
  `src/pipeline/scorer.py:_as_float` for the pattern).

---

## 6. How to know a session is wired correctly

After cloning + `pip install -e ".[dev]"`:

```bash
ruff check .              # must pass with zero issues
pytest                    # must pass — current count: 148 tests
python jobs/poll_filings.py --help    # CLIs should --help cleanly
python jobs/enrich_eod.py --help
python jobs/rank_eod.py --help
```

If any of those fail, fix the environment before doing any work.

---

## 7. What's next — all 7 BRD phases are built

Phases 0–6 are implemented (see the §2 table). There is no Phase 7 in BRD §8;
the system is feature-complete per the BRD. Remaining work is operational, not
phase-building:

- **Apply `migrations/phase6_alter.sql`** to Supabase (positions table) — the
  `track-positions` cron needs it.
- **Drain + delete the temporary `reparse-backfill` workflow** once the late-May
  backlog clears (`enrich_eod.py --reparse --window-days 60` → 0 candidates).
- **Let the daily crons run** (poll → enrich → rank → signals → track) and watch
  for the first organic ≥2.0σ signal to fire and flow into the tracker.

Any genuinely new feature beyond BRD §8 is out of scope until the user asks for
it — flag, don't pre-build (BRD §0).

---

## 8. When you're stuck

- The BRD is the spec — re-read the relevant `FR-` ID before assuming.
- The user prefers concrete questions ("I see X in `filings.parser_confidence`,
  should Phase N treat that as Y?") over open-ended ones ("how should we
  approach this?").
- If something is out of scope per BRD §2.2 (non-goals) or BRD §0 (phasing),
  flag it rather than building it.
