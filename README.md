# PEAD Scanner

**Owner:** Aryan Agarwal
**Status:** Work in progress — see [BRD.md](BRD.md) for the full specification.

Automated detection and scoring of quarterly result filings on Indian exchanges (NSE/BSE),
designed to exploit the Post-Earnings Announcement Drift (PEAD) anomaly. The system polls
filings, computes a composite PEAD score, ranks signals, and notifies via Telegram. It runs
entirely on free infrastructure (GitHub Actions + Supabase free tier).

See [BRD.md](BRD.md) for goals, architecture, data model, and phased delivery plan.

## Current phase

Phase 0 — repository bootstrap. No data pipeline is wired up yet.

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
