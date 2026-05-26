# PEAD Scanner

**Owner:** Aryan Agarwal
**Status:** Work in progress — see [BRD.md](BRD.md) for the full specification.

Automated detection and scoring of quarterly result filings on Indian exchanges (NSE/BSE),
designed to exploit the Post-Earnings Announcement Drift (PEAD) anomaly. The system polls
filings, computes a composite PEAD score, ranks signals, and notifies via Telegram. It runs
entirely on free infrastructure (GitHub Actions + Supabase free tier).

See [BRD.md](BRD.md) for goals, architecture, data model, and phased delivery plan.

## Current phase

Phase 3 — enrichment layer. On top of the Phase 2 detector, the system now parses
filing PDFs via Gemini (with a regex fallback), pulls historical fundamentals from
Screener.in nightly, fetches OHLCV from yfinance, and computes the 5 PEAD component
metrics (SUE, Rev Growth YoY, Vol Spike, EAR, Margin Delta) into the [metrics](src/db/schema.sql)
table. Scoring, filtering, and signal generation are deferred to Phase 4+.

### Jobs

| Job | Workflow | Schedule | Purpose |
|---|---|---|---|
| poll-filings | [.github/workflows/poll-filings.yml](.github/workflows/poll-filings.yml) | every 15 min, 08:30–20:30 IST, Mon–Fri | NSE + BSE primaries, Trendlyne fallback, Day-0 Telegram alerts |
| heartbeat | [.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) | 09:00 IST, Mon–Fri | Daily source-health snapshot |
| enrich-eod | [.github/workflows/enrich-eod.yml](.github/workflows/enrich-eod.yml) | 20:30 IST, Mon–Fri | Gemini PDF parse + 5 PEAD metric computation |
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
