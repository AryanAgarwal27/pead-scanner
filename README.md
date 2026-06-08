# PEAD Scanner

**Owner:** Aryan Agarwal
**Status:** Work in progress — see [BRD.md](BRD.md) for the full specification.

Automated detection and scoring of quarterly result filings on Indian exchanges (NSE/BSE),
designed to exploit the Post-Earnings Announcement Drift (PEAD) anomaly. The system polls
filings, computes a composite PEAD score, ranks signals, and notifies via Telegram. It runs
entirely on free infrastructure (GitHub Actions + Supabase free tier).

See [BRD.md](BRD.md) for goals, architecture, data model, and phased delivery plan.

## Current phase

Phase 4 — scoring & filtering. On top of Phase 3's enrichment, the system now applies
hard filters (market cap, turnover, F&O ban, ASM/GSM, listing age, parser confidence,
exceptional items), deduplicates cross-source filings (NSE wins over BSE for the same
quarter), z-normalizes each PEAD component within the trailing 7-day cohort, computes
the BRD-weighted composite score, and persists a daily top-25 ranking into the
[rankings](src/db/schema.sql) table. Signal generation, tiering, and position sizing
are deferred to Phase 5+.

### Jobs

| Job | Workflow | Schedule | Purpose |
|---|---|---|---|
| poll-filings | [.github/workflows/poll-filings.yml](.github/workflows/poll-filings.yml) | every 15 min, 08:30–20:30 IST, Mon–Fri | NSE + BSE primaries, Trendlyne fallback, Day-0 Telegram alerts |
| heartbeat | [.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) | 09:00 IST, Mon–Fri | Daily source-health snapshot |
| enrich-eod | [.github/workflows/enrich-eod.yml](.github/workflows/enrich-eod.yml) | 20:30 IST, Mon–Fri | Gemini PDF parse + 5 PEAD metrics + Phase 4 ranking (inline step) |
| screener-cache | [.github/workflows/screener-cache.yml](.github/workflows/screener-cache.yml) | 23:30 IST, daily | Refresh Screener.in fundamentals + BSE↔NSE symbol map |

### Manual run

```bash
# Polls today (IST) by default — NSE + BSE primaries:
python jobs/poll_filings.py

# Replay a past trading day for testing:
python jobs/poll_filings.py --date 2026-05-23

# Dry-run (skip filings writes + Telegram sends; print summary):
python jobs/poll_filings.py --date 2026-05-14 --dry-run

# Heartbeat: probe all sources, send status snapshot:
python jobs/heartbeat.py

# Enrichment: parse PDFs + compute metrics for filings in the last 14 days:
python jobs/enrich_eod.py
python jobs/enrich_eod.py --dry-run

# Ranking (Phase 4): dedup, filter, score, persist daily top-25:
python jobs/rank_eod.py
python jobs/rank_eod.py --dry-run
python jobs/rank_eod.py --as-of 2026-05-26       # historical rerun

# Nightly Screener cache + BSE↔NSE symbol map refresh:
python jobs/screener_cache.py
python jobs/screener_cache.py --symbols HDFCBANK,RELIANCE   # ad hoc subset

# One-shot BSE↔NSE symbol map refresh (also called by screener_cache):
python -m src.sources.symbol_map refresh
```

### First-time Phase 3 setup

Before the first `enrich_eod` run, ensure:

1. Apply [migrations/phase3_alter.sql](migrations/phase3_alter.sql) in Supabase SQL Editor.
2. (Optional but recommended) Bootstrap the BSE↔NSE symbol map so BSE filings can join
   Screener fundamentals: `python -m src.sources.symbol_map refresh`. The
   `screener-cache` job will refresh this weekly thereafter.

### First-time Phase 4 setup

Before the first `rank_eod` run, ensure:

1. Apply [migrations/phase4_alter.sql](migrations/phase4_alter.sql) in Supabase SQL Editor —
   adds `rankings`, `metrics.avg_30d_turnover_cr`, `fundamentals.listed_long_enough`.
2. The ban-list and surveillance-list CSVs ship empty; populate them manually
   (see "Manual list maintenance" below) before the ranking is meaningful.

### Manual list maintenance

The F&O ban and ASM/GSM surveillance lists are not exposed as stable JSON feeds by
NSE, so the BRD-approved v1 approach is to maintain them as committed CSVs. The
filterer reloads them on every run (mtime-cached).

- [src/data/fno_ban.csv](src/data/fno_ban.csv) — columns: `symbol,effective_date,source_url`
  - Source: <https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-information>
    (the daily "Securities banned from trading in F&O segment" notice) — refresh on
    market days the list changes.
- [src/data/asm_gsm.csv](src/data/asm_gsm.csv) — columns: `symbol,list_type,stage,effective_date`
  - Sources:
    - ASM (Additional Surveillance Measure): <https://www.nseindia.com/reports/asm>
    - GSM (Graded Surveillance Measure): <https://www.nseindia.com/reports/gsm>
  - `list_type` is `ASM` or `GSM`; `stage` is informational (1–4 for ASM, 1–4 for GSM).
  - The filter is a flat membership check — both lists cause exclusion regardless of stage.

Symbols are matched on the canonical NSE ticker (uppercased). The filterer never
matches BSE scrip codes against these lists.

## How to run locally

Requires Python 3.11+.

```bash
git clone https://github.com/AryanAgarwal27/pead-scanner.git
cd pead-scanner

python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -e ".[dev]"

cp .env.example .env   # then fill in real values, never commit
# Windows PowerShell: Copy-Item .env.example .env

ruff check .
pytest
```

The `.env` file is gitignored. In CI the same variables are supplied via GitHub Actions
Secrets — never hard-code them.

## Required environment variables

See [.env.example](.env.example). All five are required:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `GEMINI_API_KEY`

## Layout

```
src/        # library code (sources, pipeline, db, notify, utils)
jobs/       # GitHub Actions entry points (added in later phases)
tests/      # pytest suite
.github/workflows/   # CI jobs
BRD.md      # specification — source of truth
```
