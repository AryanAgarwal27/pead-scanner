"""Telegram message templates.

Phase 1 Day-0 alerts (format_single_filing / format_batched) are the FR-2.3
'parsing in progress' variant. Phase 5 adds the tiered trade-signal message
(format_signal, FR-5.3) and the run-summary with concentration flags
(format_signal_summary, FR-5.7).

The single-filing template is used when ≤ POLL_BATCH_THRESHOLD new filings land
in one poll; otherwise batched messages are used (FR-2.4).
"""

from typing import Any

from src import config
from src.sources.bse import BseFiling
from src.utils import time_utils

TELEGRAM_MAX_LEN = 4000  # actual cap is 4096; leave headroom

# Markdown legacy parse_mode special chars we escape inside dynamic strings.
_MD_ESCAPE_CHARS = ("*", "_", "[", "]", "`")

# Tier → emoji + whether it warrants a manual-review flag (STRONG, FR-5.4).
_TIER_EMOJI = {"WATCH": "👀", "TAKE": "✅", "STRONG": "🔥"}

# Confirmation key → short human label for the checklist (FR-5.5).
_CONFIRMATION_LABELS = {
    "C1": "Volume ≥2×",
    "C2": "Mkt regime",
    "C3": "Not extended",
    "C4": "Liquidity",
    "C5": "No corp action",
}


def _escape_md(s: str) -> str:
    out = s
    for ch in _MD_ESCAPE_CHARS:
        out = out.replace(ch, "\\" + ch)
    return out


def format_single_filing(f: BseFiling) -> str:
    lines = [
        "🔔 *Quarterly Result Filed* (BSE)",
        "",
        f"*Company:* {_escape_md(f.company_name)}",
        f"*Symbol:* {_escape_md(f.symbol)}",
        f"*Quarter:* {_escape_md(f.quarter)}",
        f"*Filed:* {time_utils.format_ist(f.filing_time)}",
        "",
        "_Headline numbers parsing in progress — will update when available._",
        "",
    ]
    if f.filing_url:
        lines.append(f"[View filing]({f.filing_url})")
    else:
        lines.append("_(PDF link not yet available on BSE)_")
    return "\n".join(lines)


def format_batched(filings: list[BseFiling]) -> list[str]:
    """Render >POLL_BATCH_THRESHOLD filings into one or more messages.

    Splits on line boundary if a single message would exceed TELEGRAM_MAX_LEN.
    """
    if not filings:
        return []
    header = f"🔔 *{len(filings)} Quarterly Results Filed* (BSE) — last 15 min\n\n"
    footer = (
        "\n\n_Headline numbers parsing in progress for all — "
        "individual updates to follow once parser ships._"
    )

    bullets: list[str] = []
    for f in filings:
        link = f"[PDF]({f.filing_url})" if f.filing_url else "_(no PDF)_"
        bullets.append(
            f"• {_escape_md(f.company_name)} "
            f"({_escape_md(f.symbol)}) — {_escape_md(f.quarter)} — {link}"
        )

    messages: list[str] = []
    current = header
    for line in bullets:
        if len(current) + len(line) + 1 + len(footer) > TELEGRAM_MAX_LEN and current != header:
            messages.append(current.rstrip() + footer)
            current = header
        current += line + "\n"
    if current != header:
        messages.append(current.rstrip() + footer)
    return messages


# ---------------------------------------------------------------------------
# Phase 5 — tiered trade signal (FR-5.3)
# ---------------------------------------------------------------------------


def _z_or_dash(v: float | None) -> str:
    """Render a z-component. NULL (component absent for this row, since
    n_components can be 3–5) shows as an em dash — never None/crash."""
    return "—" if v is None else f"{v:+.2f}"


def format_signal(s: dict[str, Any]) -> str:
    """Render one tiered trade signal (BRD §3.5 FR-5.3).

    Includes: rank, score (σ), all 5 z-component metrics, entry/stop/T1/T2,
    risk:reward, tier, the 5-point confirmation checklist, and suggested size.

    `s` is the signaler payload dict (entry_price/stop_price/target1_price/
    risk_reward/tier/confirmations/confirmations_passed/suggested_size_r/rank/
    pead_score/symbol and the `_z` sub-dict of z components).
    """
    tier = s["tier"]
    emoji = _TIER_EMOJI.get(tier, "•")
    symbol = _escape_md(str(s["symbol"]))
    score = float(s["pead_score"])
    size_r = float(s["suggested_size_r"])
    risk_inr = config.DEFAULT_RISK_PER_TRADE_PCT * config.PORTFOLIO_VALUE_INR
    z = s.get("_z") or {}

    lines = [
        f"{emoji} *#{s['rank']} {symbol}* — {tier} ({score:.2f}σ)",
    ]
    if tier == "STRONG":
        lines.append("🔎 _Flagged for manual review (STRONG tier)._")
    lines += [
        "",
        "*Levels*",
        f"  Entry: {s['entry_price']:.2f}",
        f"  Stop:  {s['stop_price']:.2f}",
        f"  T1:    {s['target1_price']:.2f}  (R:R {float(s['risk_reward']):.2f})",
        f"  T2:    Trail {config.TRAILING_EMA}-EMA, max {config.MAX_HOLD_DAYS}d",
        "",
        "*Components (σ)*",
        f"  SUE {_z_or_dash(z.get('z_sue'))}  •  Rev {_z_or_dash(z.get('z_rev'))}  "
        f"•  EAR {_z_or_dash(z.get('z_ear'))}  •  Vol {_z_or_dash(z.get('z_vol'))}  "
        f"•  Margin {_z_or_dash(z.get('z_margin'))}",
        "",
        f"*Confirmations* ({s['confirmations_passed']}/5)",
    ]
    confirmations = s.get("confirmations") or {}
    for key in ("C1", "C2", "C3", "C4", "C5"):
        mark = "✅" if confirmations.get(key) else "❌"
        lines.append(f"  {mark} {key} {_CONFIRMATION_LABELS[key]}")
    lines += [
        "",
        f"*Size:* {size_r:g}R (₹{risk_inr:,.0f} risk/trade)",
    ]
    return "\n".join(lines)


def format_signal_summary(summary: Any) -> str:
    """Render the end-of-run summary with concentration flags (FR-5.7).

    `summary` is a SignalSummary (duck-typed to avoid importing the signaler).
    """
    by_tier = getattr(summary, "by_tier", {}) or {}
    tier_bits = "  ".join(
        f"{t}={by_tier.get(t, 0)}" for t in ("STRONG", "TAKE", "WATCH") if by_tier.get(t)
    ) or "none"

    lines = [
        f"📊 *PEAD Signals — {summary.run_date.isoformat()}*",
        "",
        f"Sent: *{summary.sent_count}*   ({tier_bits})",
        f"Ranked: {summary.ranked_count}  •  "
        f"skipped: tier {summary.skipped_tier}, sizing {summary.skipped_sizing}, "
        f"data {summary.skipped_data}, already-sent {summary.already_signalled}",
    ]
    if not getattr(summary, "regime_available", True):
        lines.append("⚠️ _Nifty regime unavailable — C2 failed for all; no signals sent._")

    flags = getattr(summary, "flags", []) or []
    lines.append("")
    if flags:
        lines.append("*Concentration flags*")
        lines += [f"  {f}" for f in flags]
    else:
        lines.append("_No concentration limits breached._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 6 — daily position-tracker summary (FR-6.4)
# ---------------------------------------------------------------------------


def _pct_or_dash(v: float | None) -> str:
    """Render a P&L fraction (0.034 → '+3.4%'). None → em dash."""
    return "—" if v is None else f"{v * 100:+.1f}%"


def format_position_summary(summary: Any) -> str:
    """Render the daily tracker summary (BRD §3.6 FR-6.4): open count, today's
    P&L, MTD P&L, and hit rate over the last 50 closed positions.

    `summary` is a TrackSummary (duck-typed to avoid importing the tracker).
    P&L figures are the MEAN per-position realized return of the relevant
    closed cohort (size-agnostic %), not a portfolio-weighted ₹ figure.
    """
    hit = getattr(summary, "hit_rate", None)
    hit_str = "—" if hit is None else f"{hit * 100:.0f}% (n={summary.hit_rate_n})"
    lines = [
        f"📈 *PEAD Positions — {summary.run_date.isoformat()}*",
        "",
        f"Open: *{summary.open_count}*   "
        f"(closed today: {summary.newly_closed}, expired: {summary.newly_expired})",
        f"Today's P&L (avg/closed): {_pct_or_dash(getattr(summary, 'today_pnl_pct', None))}",
        f"MTD P&L (avg/closed):     {_pct_or_dash(getattr(summary, 'mtd_pnl_pct', None))}",
        f"Hit rate (last 50):       {hit_str}",
    ]
    if getattr(summary, "errors", 0):
        lines.append(f"⚠️ _{summary.errors} signal(s) errored — see logs._")
    return "\n".join(lines)
