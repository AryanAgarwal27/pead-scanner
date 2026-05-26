# PEAD Scanner

**Owner:** Aryan Agarwal
**Status:** Work in progress — see [BRD.md](BRD.md) for the full specification.

Automated detection and scoring of quarterly result filings on Indian exchanges (NSE/BSE),
designed to exploit the Post-Earnings Announcement Drift (PEAD) anomaly. The system polls
filings, computes a composite PEAD score, ranks signals, and notifies via Telegram. It runs
entirely on free infrastructure (GitHub Actions + Supabase free tier).

See [BRD.md](BRD.md) for goals, architecture, data model, and phased delivery plan.

## Current phase

Phase 2 — multi-source resilience. The [poll-filings workflow](.github/workflows/poll-filings.yml) runs every 15 minutes during market hours (Mon–Fri, 03:00–14:59 UTC ≈ 08:30–20:30 IST) and now polls **NSE + BSE in parallel** (per BRD §3.1 FR-1.2, both are primary). If both primaries error in the same run, the [detector](src/pipeline/detector.py) falls back to [Trendlyne](src/sources/trendlyne.py). A daily [heartbeat workflow](.github/workflows/heartbeat.yml) sends a status snapshot at 09:00 IST. Source failures are alerted to Telegram with a 1-per-hour-per-source cooldown.

Enrichment, scoring, and signal generation are deferred to Phase 3+.

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
