# PEAD Scanner

**Owner:** Aryan Agarwal
**Status:** Work in progress — see [BRD.md](BRD.md) for the full specification.

Automated detection and scoring of quarterly result filings on Indian exchanges (NSE/BSE),
designed to exploit the Post-Earnings Announcement Drift (PEAD) anomaly. The system polls
filings, computes a composite PEAD score, ranks signals, and notifies via Telegram. It runs
entirely on free infrastructure (GitHub Actions + Supabase free tier).

See [BRD.md](BRD.md) for goals, architecture, data model, and phased delivery plan.

## Current phase

Phase 1 — BSE Day-0 alerts. Every 15 minutes during market hours (Mon–Fri, 03:00–11:59 UTC ≈ 08:30–17:29 IST) the [poll-filings workflow](.github/workflows/poll-filings.yml) hits the BSE result-category announcement API, derives the reporting quarter, upserts into Supabase, and sends a Telegram alert for each new filing (or a batched summary if >10 land in one poll).

NSE / Trendlyne failover, enrichment, scoring, and signal generation are deferred to later phases.

### Manual run

```bash
# Polls today (IST) by default:
python jobs/poll_filings.py

# Replay a past trading day for testing:
python jobs/poll_filings.py --date 2026-05-23
```

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
