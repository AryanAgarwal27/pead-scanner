# ruff: noqa: E501
# Long lines in this module are deliberate: most are Pydantic Field `description=`
# strings and a system-instruction prompt that are sent verbatim to Gemini and
# read naturally as prose. Re-wrapping would obscure the intent without changing
# behaviour.
"""Gemini PDF parser — Phase 3.

Extracts standardized financial numbers from quarterly result PDFs filed on
NSE/BSE. Fallback chain (FR-3.5):

    Gemini 2.5 Flash-Lite   (primary, separate quota bucket)
        ↓ retry-with-backoff on transient 429 / 5xx (Retry-After aware), then
        ↓ demote on exhausted retries / ValidationError / column-validation
    Gemini 2.5 Flash        (secondary, separate quota bucket)
        ↓ same retry-then-demote behaviour
    regex parser            (last resort — see src.sources.regex_parser)

A per-filing cumulative backoff ceiling caps total sleep across both tiers, so
a row stuck behind a repeated Retry-After is deferred to the next run rather
than burning the job's wall clock. See parse_pdf for the throttle/budget hook.

The caller (src.pipeline.enricher) decides whether to invoke the regex tier —
this module surfaces a `ParseFailure` exception with the last error so the
enricher can branch deterministically.

Caching is the CALLER'S responsibility (filings.parsed_at IS NOT NULL gate).
This module never reads/writes the database — it's a pure function of
(pdf_bytes, expected_quarter) → ParsedFiling.

--------------------------------------------------------------------------
Why raw-value extraction + Python-side unit conversion (instead of asking
Gemini to convert in its head):

Live testing 4 production PDFs revealed Gemini 2.5 Flash-Lite reliably
EXTRACTS the right cell values but UNRELIABLY APPLIES the unit-conversion
arithmetic it was told to do — even naming the (correct) rule in `notes`
while emitting raw, un-converted numbers. LLMs are unreliable at silent
arithmetic. Fix: schema asks for `revenue_raw / pat_raw / ...` PLUS a
single `unit` enum; Python multiplies by the appropriate factor. This
makes the conversion deterministic and auditable.

Hallucination defenses (in order of strength):
    1. Column validation — Python verifies the model identified a QUARTER
       column for the expected quarter end-date. If `source_table` and
       `column_label` don't both reference a quarter AND the expected date,
       we discard the extraction (confidence='failed') and try the next tier.
    2. Pydantic schema enforcement — required fields, Literal-constrained
       `unit` enum. Bad JSON → ValidationError → next tier.
    3. Sanity bounds in Crore-space: revenue ≤ ₹10 lakh Cr, |EPS| ≤ 1e4,
       OPM ∈ [-100, 100]. Out-of-range → coerced to None + confidence='low'.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from src import config
from src.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schema — what Gemini is asked to return.
# ---------------------------------------------------------------------------


class GeminiResponse(BaseModel):
    """LLM-facing schema. ALL monetary fields are RAW (as printed in the PDF).
    Python applies the unit conversion using the `unit` enum.
    """

    # --- Provenance (used for hard column validation) ----------------------
    source_table: str = Field(
        ...,
        description=(
            "EXACT heading of the financial results TABLE you extracted from, as printed in the PDF. "
            "Must reference 'Quarter' or '3 months' AND the expected quarter-end date. "
            "Example: 'Statement of Standalone Ind AS financial results for the quarter and year "
            "ended March 31, 2026'."
        ),
    )
    column_label: str = Field(
        ...,
        description=(
            "EXACT label of the COLUMN you read the headline numbers from, as printed at the top of "
            "that column. Must refer to a single QUARTER, not a year. "
            "Examples: 'Quarter ended March 31, 2026 (Audited)', '3 months ended 31.03.2026', "
            "'Quarter ended 30 June 2025'."
        ),
    )

    # --- Raw numbers — exactly as printed, NO unit conversion --------------
    revenue_raw: float | None = Field(
        default=None,
        description=(
            "Revenue from Operations / Net Sales / Sales-Income from operations, EXACTLY as printed "
            "in the cell. DO NOT CONVERT UNITS. If the cell shows '3,426.25' return 3426.25; "
            "if it shows '(1,030.50)' return -1030.50."
        ),
    )
    pat_raw: float | None = Field(
        default=None,
        description=(
            "Profit After Tax / 'Profit for the period' attributable to owners, EXACTLY as printed. "
            "DO NOT CONVERT UNITS. Negative values (losses) printed in parens should be negative."
        ),
    )
    operating_profit_raw: float | None = Field(
        default=None,
        description=(
            "Operating Profit / EBITDA / 'Profit before exceptional items, interest, tax, "
            "depreciation' — whichever the filing labels as the operating-profit line — for the "
            "selected quarter column, EXACTLY as printed. NO unit conversion. Null if the filing "
            "shows no explicit operating-profit line."
        ),
    )
    eps_basic_raw: float | None = Field(
        default=None,
        description=(
            "BASIC EPS for the selected quarter, in ₹ per share, EXACTLY as printed. "
            "NOT diluted. NOT year-to-date. NOT annualized."
        ),
    )
    revenue_yoy_raw: float | None = Field(
        default=None,
        description=(
            "Revenue from Operations for the PRIOR-YEAR SAME QUARTER comparator column "
            "(e.g. 'Quarter ended March 31, 2025' for a Q4-FY26 filing), EXACTLY as printed. "
            "NO unit conversion. Null if the comparator column isn't shown."
        ),
    )
    pat_yoy_raw: float | None = Field(
        default=None,
        description=(
            "PAT for the PRIOR-YEAR SAME QUARTER comparator column, EXACTLY as printed. "
            "NO unit conversion. Null if not shown."
        ),
    )

    # --- Single unit declaration — applies to all monetary raw fields above
    unit: Literal["crores", "lakhs", "millions", "rupees"] = Field(
        ...,
        description=(
            "The unit of measurement printed at the top of the financial results table — usually "
            "in parentheses or in a sub-heading. Look for '(Rs. in Lakhs)', '(Rs. in Crores)', "
            "'(Rs. in Million)', '(₹ in Lakhs)'. Pick exactly one. This applies to revenue_raw, "
            "pat_raw, operating_profit_raw, revenue_yoy_raw, and pat_yoy_raw. EPS is NEVER in these "
            "units — it is always ₹ per share."
        ),
    )

    # --- Categorical flags -------------------------------------------------
    is_consolidated: bool | None = Field(
        default=None,
        description=(
            "True if the table you extracted from is the CONSOLIDATED results; False if STANDALONE; "
            "Null if the filing doesn't clearly indicate."
        ),
    )
    has_exceptional_items: bool = Field(
        default=False,
        description=(
            "True iff the results table shows a NON-ZERO line labelled 'Exceptional items' or "
            "'Extraordinary items' for the selected quarter column."
        ),
    )

    # --- Self-reported quality --------------------------------------------
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description=(
            "'high' = the source_table, column_label, unit, and all numeric cells were unambiguous; "
            "'medium' = some interpretation required; "
            "'low' = document hard to read or numbers ambiguous."
        ),
    )
    notes: str | None = Field(
        default=None,
        description="Brief caveat, e.g. 'standalone table only, no consolidated shown'. Null if none.",
    )


# What the caller actually persists. Fields mirror filings table columns.
@dataclass
class ParsedFiling:
    revenue_cr: float | None
    pat_cr: float | None
    eps: float | None                # ₹ per share (Basic, this quarter)
    opm_pct: float | None            # percent
    revenue_yoy_pct: float | None    # absolute ₹ Cr for prior-year same quarter (BRD §6.1 column name is misleading)
    pat_yoy_pct: float | None        # absolute ₹ Cr for prior-year same quarter
    is_consolidated: bool | None
    has_exceptional_items: bool
    confidence: Literal["high", "medium", "low", "failed"]
    notes: str | None
    parser_used: Literal["gemini-flash-lite", "gemini-flash", "regex"]


class ParseFailure(Exception):
    """Raised when BOTH Gemini tiers exhausted (rate-limited, errored, schema-failed,
    or column-validation-failed). Carries the last underlying error so the caller
    can decide whether to fall back to regex (Phase 3 default) or skip the filing.
    """

    def __init__(self, message: str, last_error: Exception | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """\
You are a financial-data extraction agent for Indian listed-company quarterly results PDFs.

ABSOLUTE RULES (violation = invalid response):
  1. DO NOT compute, convert, or transform any number. Return numbers EXACTLY as printed.
  2. DO NOT apply unit conversion. Report the raw cell value and the unit separately.
  3. DO NOT extract from YEAR-ENDED columns. Extract only from a single-QUARTER column.
  4. DO NOT extract from press releases, auditor opinions, or supplementary annexures.
     Use only the main 'Statement of Financial Results' table.
  5. If the cell value cannot be located, return null. NEVER guess.

COLUMN SELECTION:
  Indian quarterly result tables typically show 5 columns:
    [Quarter ended <recent>] [Quarter ended <prev qtr>] [Quarter ended <yoy>]
    [Year ended <recent>] [Year ended <prev yoy>]
  You must pick the FIRST column (Quarter ended <recent>). Report its exact
  printed label in `column_label`. If a Year-ended column has identical
  end-dates (true for Q4 filings where year-end == Q4-end), still pick the
  Quarter column — its header will say 'Quarter' or '3 months', not 'Year'.

CONSOLIDATED vs STANDALONE:
  If the PDF contains BOTH a standalone results table and a consolidated
  results table, PREFER the CONSOLIDATED one and set is_consolidated=true.
  If only one is shown, use it and set is_consolidated accordingly.

UNIT FIELD:
  Indian filings always state the unit (it's a SEBI requirement). Look near
  the top of the table for parenthetical text like '(Rs. in Lakhs)',
  '(Rs. in Crores)', '(Rs. in Million)', '(₹ in Lakhs)', '(Amount in ₹)'.
  Report it in the `unit` field. Pick exactly one.

EPS:
  Use the BASIC EPS row. If both Basic and Diluted are shown as separate
  rows, take Basic ONLY. EPS is reported in ₹ per share — NEVER apply the
  monetary unit conversion to EPS.

EXCEPTIONAL ITEMS:
  has_exceptional_items = true ONLY if the results table contains a line
  explicitly labelled 'Exceptional items' or 'Extraordinary items' with a
  non-zero value in the selected quarter column. If the line is blank, '-',
  or zero, set false.

Return JSON matching the provided schema. No prose, no markdown.
"""


# ---------------------------------------------------------------------------
# Client (lazy)
# ---------------------------------------------------------------------------

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY must be set in the environment.")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_RETRY_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def parse_pdf(
    pdf_bytes: bytes,
    expected_quarter: str,
    *,
    on_dispatch: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_total_backoff_seconds: float | None = None,
) -> ParsedFiling:
    """Parse a quarterly-result PDF via Gemini, with Flash-Lite → Flash fallback.

    Each tier retries on transient 429/5xx with backoff BEFORE demoting to the
    next tier (Phase 3 rate-limit fix): a momentary rate limit becomes a wait,
    not a permanent regex demotion. Validation / column-validation failures are
    deterministic, so they demote immediately (no retry).

    Args:
        pdf_bytes: raw PDF content.
        expected_quarter: the quarter label this filing should be reporting
            (e.g. 'Q4-FY26'). Used to validate that Gemini selected the
            correct column (not a year-end column, not a wrong quarter).
        on_dispatch: called immediately before EACH Gemini API dispatch (every
            tier attempt AND every retry). The enricher passes a hook that
            counts the call against the daily RPD budget — so the budget counts
            actual API CALLS across both tiers and all retries, not filings.
        sleep: injectable sleeper (tests pass a fake to capture backoff waits).
        max_total_backoff_seconds: per-filing cumulative backoff ceiling. Once
            total sleep across all tiers/retries would exceed this, stop backing
            off and demote/give up — deferring the row to the next run rather
            than burning wall-clock behind a repeated Retry-After. Defaults to
            config.GEMINI_MAX_TOTAL_BACKOFF_SECONDS.

    Raises:
        ParseFailure: if BOTH Gemini tiers fail (HTTP, schema, column
            validation, or backoff-ceiling deferral). Caller should then try
            regex_parser.parse_pdf.
    """
    if max_total_backoff_seconds is None:
        max_total_backoff_seconds = config.GEMINI_MAX_TOTAL_BACKOFF_SECONDS
    last_error: Exception | None = None
    total_slept = 0.0

    for model_name, parser_tag in (
        (config.GEMINI_PRIMARY_MODEL, "gemini-flash-lite"),
        (config.GEMINI_FALLBACK_MODEL, "gemini-flash"),
    ):
        for attempt in range(1, config.GEMINI_RETRY_MAX_ATTEMPTS + 1):
            if on_dispatch is not None:
                on_dispatch()
            try:
                return _call_gemini(pdf_bytes, expected_quarter, model_name, parser_tag)
            except (genai_errors.ClientError, genai_errors.ServerError) as e:
                last_error = e
                status = getattr(e, "code", None) or getattr(e, "status_code", None)
                if status not in _RETRY_HTTP_CODES:
                    log.error(f"[{model_name}] non-retriable HTTP {status}: {e}")
                    raise ParseFailure(f"{model_name}: {e}", last_error=e) from e
                if attempt >= config.GEMINI_RETRY_MAX_ATTEMPTS:
                    log.warning(
                        f"[{model_name}] HTTP {status} — {attempt} attempts exhausted, demoting tier"
                    )
                    break
                wait = _backoff_wait(e, attempt)
                if total_slept + wait > max_total_backoff_seconds:
                    log.warning(
                        f"[{model_name}] HTTP {status} — per-filing backoff ceiling "
                        f"({max_total_backoff_seconds:.0f}s) reached after {total_slept:.0f}s; "
                        f"demoting without further wait (defer to next run)"
                    )
                    break
                log.warning(
                    f"[{model_name}] HTTP {status} — backoff {wait:.0f}s "
                    f"(attempt {attempt}/{config.GEMINI_RETRY_MAX_ATTEMPTS})"
                )
                sleep(wait)
                total_slept += wait
            except ValidationError as e:
                last_error = e
                log.warning(f"[{model_name}] Pydantic validation failed — demoting: {e}")
                break
            except _ColumnValidationFailure as e:
                last_error = e
                log.warning(f"[{model_name}] column validation failed — demoting: {e}")
                break
            except Exception as e:  # noqa: BLE001 — last-resort log + demote
                last_error = e
                log.exception(f"[{model_name}] unexpected error — demoting")
                break

    raise ParseFailure(
        "Both Gemini tiers (Flash-Lite + Flash) failed", last_error=last_error
    )


# ---------------------------------------------------------------------------
# Backoff helpers — prefer the server's Retry-After / RetryInfo, else exponential.
# ---------------------------------------------------------------------------


def _backoff_wait(error: Exception, attempt: int) -> float:
    """Seconds to wait before the next retry. Server-suggested delay wins;
    otherwise exponential (base × 2^(attempt-1)). Capped at the per-wait max."""
    server = _retry_after_seconds(error)
    wait = server if server is not None else config.GEMINI_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return min(wait, config.GEMINI_BACKOFF_MAX_WAIT_SECONDS)


def _retry_after_seconds(error: Exception) -> float | None:
    """Extract a server-suggested retry delay from a genai error, if present.

    Checks (1) the HTTP `Retry-After` header on the underlying response, then
    (2) Google's `RetryInfo.retryDelay` (e.g. '57s') in the error details body.
    Returns None if neither is available/parseable (caller uses exponential).
    """
    resp = getattr(error, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is not None:
        try:
            ra = headers.get("Retry-After") or headers.get("retry-after")
        except AttributeError:
            ra = None
        secs = _parse_retry_after_value(ra)
        if secs is not None:
            return secs

    for item in _iter_error_detail_items(getattr(error, "details", None)):
        if isinstance(item, dict) and "RetryInfo" in str(item.get("@type", "")):
            secs = _parse_retry_after_value(item.get("retryDelay"))
            if secs is not None:
                return secs
    return None


def _iter_error_detail_items(details: object):
    """Yield detail dicts from a genai error 'details' payload, which may be
    {'error': {'details': [...]}}, {'details': [...]}, or a bare list."""
    if isinstance(details, dict):
        err = details.get("error")
        if isinstance(err, dict) and isinstance(err.get("details"), list):
            yield from err["details"]
        if isinstance(details.get("details"), list):
            yield from details["details"]
    elif isinstance(details, list):
        yield from details


def _parse_retry_after_value(value: object) -> float | None:
    """Parse a Retry-After-style value: numeric seconds (57 / '57') or a Google
    duration string ('57s', '1.5s'). Returns float seconds, or None if absent
    or non-numeric (e.g. an HTTP-date form we don't handle)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    s = str(value).strip().lower()
    if s.endswith("s"):
        s = s[:-1]
    try:
        secs = float(s)
    except ValueError:
        return None
    return secs if secs >= 0 else None


# ---------------------------------------------------------------------------
# Internal — single Gemini call + validation + unit conversion
# ---------------------------------------------------------------------------


class _ColumnValidationFailure(Exception):
    """Raised when Gemini's source_table/column_label don't reference both
    'quarter' AND the expected quarter-end date. Caught by parse_pdf to
    trigger the next-tier fallback."""


def _call_gemini(
    pdf_bytes: bytes, expected_quarter: str, model_name: str, parser_tag: str
) -> ParsedFiling:
    client = _get_client()

    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

    response = client.models.generate_content(
        model=model_name,
        contents=[pdf_part, f"Expected quarter: {expected_quarter}. Extract per the schema."],
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=GeminiResponse,
            temperature=0.0,
            # Gemini 2.5 models spend max_output_tokens on internal "thinking"
            # tokens. At the old 1024 cap, gemini-2.5-flash (the fallback tier)
            # burned the budget thinking and truncated the JSON mid-string
            # ("EOF while parsing"), so the fallback could never rescue a
            # primary-tier miss. Disable thinking — this is deterministic
            # structured extraction at temp=0, not a reasoning task — and give
            # the JSON ample headroom.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            max_output_tokens=2048,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, GeminiResponse):
        gem = parsed
    else:
        gem = GeminiResponse.model_validate_json(response.text or "")

    # Hard validation: did Gemini pick a quarter column for the right date?
    _validate_columns(gem.source_table, gem.column_label, expected_quarter)

    return _to_parsed_filing(gem, parser_tag)


# ---------------------------------------------------------------------------
# Column validation
# ---------------------------------------------------------------------------

# Indian fiscal-quarter end-dates (FY ends March).
_QUARTER_END_MONTH_DAY = {1: (6, 30), 2: (9, 30), 3: (12, 31), 4: (3, 31)}
_MONTH_NAMES = {
    3: ("March", "Mar"),
    6: ("June", "Jun"),
    9: ("September", "Sep", "Sept"),
    12: ("December", "Dec"),
}
_QUARTER_RE = re.compile(r"Q([1-4])-FY(\d{2})")
_QUARTER_WORD_RE = re.compile(r"quarter|3\s*months|three\s*months", re.IGNORECASE)


def _quarter_to_end_date(quarter_label: str) -> date:
    """'Q4-FY26' -> date(2026, 3, 31). Q4 ends in the FY's year; Q1-Q3 end in FY-1.
    (FY26 = April 2025 – March 2026; Q1-FY26 ends Jun 2025, Q4-FY26 ends Mar 2026.)
    """
    m = _QUARTER_RE.fullmatch(quarter_label)
    if not m:
        raise ValueError(f"unrecognized quarter label: {quarter_label!r}")
    q = int(m.group(1))
    fy_year = 2000 + int(m.group(2))
    month, day = _QUARTER_END_MONTH_DAY[q]
    year = fy_year if q == 4 else fy_year - 1
    return date(year, month, day)


def _expected_date_strings(quarter_label: str) -> list[str]:
    """Generate the date-string variants we'll accept in source_table/column_label."""
    d = _quarter_to_end_date(quarter_label)
    month_names = _MONTH_NAMES[d.month]
    yy = d.year % 100
    variants: list[str] = []
    for mn in month_names:
        variants.append(f"{mn} {d.day}, {d.year}")
        variants.append(f"{mn} {d.day} {d.year}")
        variants.append(f"{d.day} {mn} {d.year}")
        variants.append(f"{d.day} {mn}, {d.year}")
        variants.append(f"{d.day}{_ordinal_suffix(d.day)} {mn} {d.year}")
    # Numeric: 31.03.2026, 31/03/2026, 31-03-2026, 2026-03-31, plus 2-digit years
    variants.extend([
        f"{d.day:02d}.{d.month:02d}.{d.year}",
        f"{d.day:02d}/{d.month:02d}/{d.year}",
        f"{d.day:02d}-{d.month:02d}-{d.year}",
        f"{d.year}-{d.month:02d}-{d.day:02d}",
        f"{d.day:02d}.{d.month:02d}.{yy:02d}",
        f"{d.day:02d}/{d.month:02d}/{yy:02d}",
    ])
    return variants


def _ordinal_suffix(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _validate_columns(source_table: str, column_label: str, expected_quarter: str) -> None:
    """Raise _ColumnValidationFailure if the model didn't pick a quarter column
    for the expected end-date."""
    combined = f"{source_table} || {column_label}"
    if not _QUARTER_WORD_RE.search(combined):
        raise _ColumnValidationFailure(
            f"neither source_table nor column_label contains 'quarter' / '3 months' "
            f"(got source_table={source_table!r}, column_label={column_label!r})"
        )
    lower = combined.lower()
    expected = _expected_date_strings(expected_quarter)
    if not any(v.lower() in lower for v in expected):
        raise _ColumnValidationFailure(
            f"expected one of {expected[:3]}... to appear in source_table/column_label "
            f"(got source_table={source_table!r}, column_label={column_label!r})"
        )


# ---------------------------------------------------------------------------
# Unit conversion + sanity bounds
# ---------------------------------------------------------------------------

# Multipliers to convert raw value → ₹ Crores.
#   1 Cr = 100 Lakh = 10 Million = 10^7 Rupees
_UNIT_TO_CR: dict[str, float] = {
    "crores": 1.0,
    "lakhs": 0.01,
    "millions": 0.1,
    "rupees": 1e-7,
}

# Sanity bounds in CRORE space.
_MAX_REVENUE_CR = 1e7        # ₹10 lakh crore — larger than any single Indian company's annual revenue
_MAX_PAT_CR = 1e7
_MAX_ABS_EPS = 1e4
_OPM_MIN, _OPM_MAX = -100.0, 100.0


def _to_parsed_filing(gem: GeminiResponse, parser_tag: str) -> ParsedFiling:
    """Apply unit conversion, compute OPM, sanity-bound, and assemble ParsedFiling."""
    mult = _UNIT_TO_CR[gem.unit]

    revenue_cr_raw = None if gem.revenue_raw is None else gem.revenue_raw * mult
    pat_cr_raw = None if gem.pat_raw is None else gem.pat_raw * mult
    op_profit_cr = None if gem.operating_profit_raw is None else gem.operating_profit_raw * mult
    revenue_yoy_cr = None if gem.revenue_yoy_raw is None else gem.revenue_yoy_raw * mult
    pat_yoy_cr = None if gem.pat_yoy_raw is None else gem.pat_yoy_raw * mult

    # OPM computed in Python — never trust the LLM to do division.
    opm_pct_raw: float | None
    if op_profit_cr is not None and revenue_cr_raw is not None and revenue_cr_raw > 0:
        opm_pct_raw = (op_profit_cr / revenue_cr_raw) * 100
    else:
        opm_pct_raw = None

    out_of_range: list[str] = []
    revenue_cr = _bound(revenue_cr_raw, 0, _MAX_REVENUE_CR, "revenue_cr", out_of_range)
    pat_cr = _bound(pat_cr_raw, -_MAX_PAT_CR, _MAX_PAT_CR, "pat_cr", out_of_range)
    eps = _bound(gem.eps_basic_raw, -_MAX_ABS_EPS, _MAX_ABS_EPS, "eps", out_of_range)
    opm_pct = _bound(opm_pct_raw, _OPM_MIN, _OPM_MAX, "opm_pct", out_of_range)
    revenue_yoy = _bound(revenue_yoy_cr, 0, _MAX_REVENUE_CR, "revenue_yoy", out_of_range)
    pat_yoy = _bound(pat_yoy_cr, -_MAX_PAT_CR, _MAX_PAT_CR, "pat_yoy", out_of_range)

    confidence: Literal["high", "medium", "low", "failed"] = gem.confidence
    notes = gem.notes
    if out_of_range:
        log.warning(
            f"[{parser_tag}] sanity bounds tripped on {out_of_range}; confidence -> low"
        )
        confidence = "low"
        notes = f"{notes or ''} [bounded:{','.join(out_of_range)}]".strip()

    return ParsedFiling(
        revenue_cr=revenue_cr,
        pat_cr=pat_cr,
        eps=eps,
        opm_pct=opm_pct,
        revenue_yoy_pct=revenue_yoy,
        pat_yoy_pct=pat_yoy,
        is_consolidated=gem.is_consolidated,
        has_exceptional_items=gem.has_exceptional_items,
        confidence=confidence,
        notes=notes,
        parser_used=parser_tag,  # type: ignore[arg-type]
    )


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
