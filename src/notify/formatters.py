"""Telegram message templates for Phase 1 Day-0 alerts.

Every Phase 1 alert is the FR-2.3 'parsing in progress' variant — Phase 3 will
populate headline numbers by extending this module. The single-filing template
is used when ≤ POLL_BATCH_THRESHOLD new filings land in one poll; otherwise
batched messages are used (FR-2.4).
"""

from src.sources.bse import BseFiling
from src.utils import time_utils

TELEGRAM_MAX_LEN = 4000  # actual cap is 4096; leave headroom

# Markdown legacy parse_mode special chars we escape inside dynamic strings.
_MD_ESCAPE_CHARS = ("*", "_", "[", "]", "`")


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
