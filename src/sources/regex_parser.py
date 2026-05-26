"""Regex-based PDF parser — Phase 3 last-resort fallback.

Used when BOTH Gemini tiers (Flash-Lite + Flash) have failed (rate-limit,
schema failure, or column-validation failure) OR the PDF is too large to
send to Gemini at all (>20 MB or >50 pages per the enricher's size gate).

Design philosophy: this is NOT trying to match Gemini's accuracy. Target is
~50% success rate on Revenue + PAT for PDFs whose text extracts cleanly via
pypdf. When the regex can't find an unambiguous match, it returns a
ParsedFiling with `confidence='failed'` and null numeric fields — the
filing is then excluded from downstream metric computation. The enricher
will log a WARNING so the user can manually review.

Extraction strategy:
    1. Extract text from all pages with pypdf (no OCR — image-only PDFs
       will yield empty strings and we'll fail gracefully).
    2. Detect the unit from a header like '(Rs. in Lakhs)' / '(Rs. in
       Crores)' / '(Rs. in Million)'. Default: crores (most common).
    3. For each known keyword (Revenue, PAT, EPS), find the FIRST line in
       the document containing the keyword and extract the FIRST numeric
       value from it (i.e. the most-recent-quarter column, which is always
       the leftmost data column in Indian results tables).
    4. Apply unit conversion in Python (same multipliers as gemini_parser).
    5. Sanity-bound the result; out-of-range → null.

Why "first line, first number"? Indian results tables follow a strict
template (SEBI mandated): rows in order are Revenue, expenses, profit,
exceptional items, tax, PAT, EPS; columns left-to-right are Q-recent,
Q-prev, Q-yoy, Y-recent, Y-prev. pypdf's text extraction usually preserves
this row + column order when the PDF is text-based. When it doesn't
(image-only or mangled layout), we'd produce garbage — that's what
confidence='failed' is for.

Limitations (documented, not bugs):
    - Cannot detect is_consolidated reliably (often a section header
      far from the data row); default to None.
    - Cannot detect has_exceptional_items reliably; default to False.
    - YoY values are NOT extracted (would require column-position tracking
      that regex can't do robustly). Phase 4 falls back to Screener cache.
"""

from __future__ import annotations

import io
import re

from pypdf import PdfReader

from src.sources.gemini_parser import ParsedFiling
from src.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Multipliers to convert raw value → ₹ Crores (mirrors gemini_parser._UNIT_TO_CR).
_UNIT_TO_CR: dict[str, float] = {
    "crores": 1.0,
    "lakhs": 0.01,
    "millions": 0.1,
    "rupees": 1e-7,
}

# Unit-hint regex. Order matters: try named units before the default.
# Examples we want to match:
#   "Rs. in Lakhs", "Rs in Crores", "(Rs. in Million)", "₹ in Lakhs",
#   "Amount in ₹ Crores", "(₹ in Lakhs)", "amounts in Rupees Million"
_UNIT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("lakhs",    re.compile(r"(?:rs\.?|₹|rupees?)\s*(?:in)?\s*lakhs?", re.IGNORECASE)),
    ("crores",   re.compile(r"(?:rs\.?|₹|rupees?)\s*(?:in)?\s*crores?", re.IGNORECASE)),
    ("millions", re.compile(r"(?:rs\.?|₹|rupees?)\s*(?:in)?\s*millions?", re.IGNORECASE)),
    # Bare "in Lakhs"/"in Crores"/"in Million" without a currency prefix
    ("lakhs",    re.compile(r"\bin\s+lakhs?\b", re.IGNORECASE)),
    ("crores",   re.compile(r"\bin\s+crores?\b", re.IGNORECASE)),
    ("millions", re.compile(r"\bin\s+millions?\b", re.IGNORECASE)),
]

# Keyword patterns. Each entry is (field_name, [pattern, pattern, ...]) — we
# try each pattern in order; first match wins. Patterns are anchored loosely
# so they match the START of a tabular row.
_KEYWORD_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "revenue": [
        re.compile(r"revenue\s+from\s+operations\b", re.IGNORECASE),
        re.compile(r"sales\s*/\s*income\s+from\s+operations\b", re.IGNORECASE),
        re.compile(r"total\s+income\s+from\s+operations\b", re.IGNORECASE),
        re.compile(r"\bnet\s+sales\b", re.IGNORECASE),
    ],
    "pat": [
        # PAT-style lines have many variants. Order: most specific first.
        re.compile(r"net\s+profit\s*/\s*\(?\s*loss\s*\)?\s+for\s+the\s+period", re.IGNORECASE),
        re.compile(r"profit\s*/\s*\(?\s*loss\s*\)?\s+for\s+the\s+period", re.IGNORECASE),
        re.compile(r"profit\s+after\s+tax", re.IGNORECASE),
        re.compile(r"\bnet\s+profit\b", re.IGNORECASE),
    ],
    "eps": [
        # Two-line EPS: 'Earnings per equity share' on one line, 'Basic' on the
        # next. We anchor on 'Basic' and grab the first number AFTER it on
        # the same line.
        re.compile(r"^\s*\(?\s*a\s*\)?\s*basic\b", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^\s*basic\s*\(?\s*(?:rs|₹)\.?\s*\)?", re.IGNORECASE | re.MULTILINE),
    ],
}

# Match a numeric token like "3,426.25" or "(1,030.50)" or "-123.45".
# Allow internal spaces between digits caused by pypdf word-spacing
# artifacts (e.g. "2,73 1.7 1" for "2,731.71").
_NUMBER_RE = re.compile(
    r"\(?\s*-?\s*[\d,][\d,\s]*\.?\d*\s*\)?"
)


# Sanity bounds in CRORE space (same as gemini_parser).
_MAX_REVENUE_CR = 1e7
_MAX_PAT_CR = 1e7
_MAX_ABS_EPS = 1e4


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_pdf(pdf_bytes: bytes) -> ParsedFiling:
    """Best-effort regex extraction.

    Returns a ParsedFiling with parser_used='regex'. If extraction couldn't
    find both revenue and PAT, the numeric fields are None and
    confidence='failed' — the enricher should treat this as a soft skip.
    """
    text = _extract_text(pdf_bytes)
    if not text:
        log.warning("regex parser: pypdf extracted empty text (likely image-only PDF)")
        return _failed("pypdf extracted empty text")

    unit = _detect_unit(text)
    log.info(f"regex parser: detected unit={unit}")

    revenue_raw = _first_match_value(text, _KEYWORD_PATTERNS["revenue"])
    pat_raw = _first_match_value(text, _KEYWORD_PATTERNS["pat"])
    eps_raw = _first_match_value(text, _KEYWORD_PATTERNS["eps"])

    mult = _UNIT_TO_CR[unit]
    revenue_cr = None if revenue_raw is None else revenue_raw * mult
    pat_cr = None if pat_raw is None else pat_raw * mult

    out_of_range: list[str] = []
    revenue_cr = _bound(revenue_cr, 0, _MAX_REVENUE_CR, "revenue_cr", out_of_range)
    pat_cr = _bound(pat_cr, -_MAX_PAT_CR, _MAX_PAT_CR, "pat_cr", out_of_range)
    eps = _bound(eps_raw, -_MAX_ABS_EPS, _MAX_ABS_EPS, "eps", out_of_range)

    if revenue_cr is None and pat_cr is None:
        log.warning("regex parser: extracted neither revenue nor PAT")
        return _failed("no revenue or PAT line matched")

    # confidence='low' is the best we can ever claim from regex (no source-
    # column validation, no consolidated detection).
    confidence = "low"
    notes = (
        f"regex fallback (unit={unit}); "
        f"out_of_range={out_of_range or 'none'}"
    )

    return ParsedFiling(
        revenue_cr=revenue_cr,
        pat_cr=pat_cr,
        eps=eps,
        opm_pct=None,
        revenue_yoy_pct=None,
        pat_yoy_pct=None,
        is_consolidated=None,
        has_exceptional_items=False,
        confidence=confidence,  # type: ignore[arg-type]
        notes=notes,
        parser_used="regex",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_text(pdf_bytes: bytes) -> str:
    """All-pages text via pypdf. Returns '' on failure."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:  # noqa: BLE001
        log.warning(f"regex parser: pypdf failed to open PDF: {e}")
        return ""

    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            chunks.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001
            log.warning(f"regex parser: page {i+1} extraction failed: {e}")
            continue
    return "\n".join(chunks)


def _detect_unit(text: str) -> str:
    """Detect the table's unit-of-measurement. Default 'crores' when ambiguous.

    Strategy: scan the first ~30% of the document (where the results-table
    header sits) and return the first unit-hint match. Falls back to a
    full-document scan if nothing found.
    """
    head = text[: max(2000, len(text) // 3)]
    for unit_name, pattern in _UNIT_PATTERNS:
        if pattern.search(head):
            return unit_name
    for unit_name, pattern in _UNIT_PATTERNS:
        if pattern.search(text):
            return unit_name
    log.info("regex parser: no unit hint found; defaulting to crores")
    return "crores"


def _first_match_value(text: str, patterns: list[re.Pattern[str]]) -> float | None:
    """For each keyword pattern, find the first line containing it and return
    the FIRST numeric value AFTER the keyword on that line. None if no
    pattern matches or no number is found on any matching line.
    """
    for pat in patterns:
        for line_match in pat.finditer(text):
            # Take the same line, from keyword onwards, up to the next newline.
            start = line_match.start()
            newline_idx = text.find("\n", start)
            chunk = text[start : newline_idx if newline_idx != -1 else len(text)]
            # Search after the keyword itself, not before.
            after_keyword = chunk[line_match.end() - start :]
            value = _first_number(after_keyword)
            if value is not None:
                return value
    return None


def _first_number(s: str) -> float | None:
    """Extract the first numeric token in s. Handles thousands separators,
    parenthesized negatives, and pypdf's stray internal spaces in numbers."""
    for m in _NUMBER_RE.finditer(s):
        token = m.group(0).strip()
        if not token:
            continue
        is_neg = token.startswith("(") or token.startswith("-")
        cleaned = (
            token.replace("(", "").replace(")", "").replace(",", "").replace(" ", "")
        )
        if cleaned in ("", "-", "."):
            continue
        cleaned = cleaned.lstrip("-")
        try:
            val = float(cleaned)
        except ValueError:
            continue
        # Filter out tiny tokens that are almost certainly schedule numbers
        # (like "1" in "Note 1") rather than financial values. Real financial
        # values in any unit will almost never appear as bare single digits
        # without a decimal in the FIRST column of the results table.
        if "." not in cleaned and val < 100:
            continue
        return -val if is_neg else val
    return None


def _bound(
    value: float | None,
    lo: float,
    hi: float,
    field_name: str,
    out_of_range_log: list[str],
) -> float | None:
    if value is None:
        return None
    if value < lo or value > hi:
        out_of_range_log.append(field_name)
        return None
    return value


def _failed(reason: str) -> ParsedFiling:
    """Construct an all-null failed ParsedFiling with a diagnostic note."""
    return ParsedFiling(
        revenue_cr=None,
        pat_cr=None,
        eps=None,
        opm_pct=None,
        revenue_yoy_pct=None,
        pat_yoy_pct=None,
        is_consolidated=None,
        has_exceptional_items=False,
        confidence="failed",
        notes=f"regex: {reason}",
        parser_used="regex",
    )
