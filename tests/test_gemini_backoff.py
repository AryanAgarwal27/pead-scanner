"""Unit tests for the Gemini retry-with-backoff path (Phase 3 rate-limit fix).

Covers src.sources.gemini_parser.parse_pdf:
    - transient 429 → retry SAME tier (with backoff) → succeed
    - Retry-After header / RetryInfo.retryDelay honored over exponential
    - exhausting a tier's attempts → demote to the next tier
    - per-filing cumulative backoff ceiling → stop waiting (defer)
    - non-retriable HTTP → raise immediately (no retry, no demote)
    - on_dispatch fired once per ACTUAL API call (budget counts calls, not filings)

No network: _call_gemini is monkeypatched. sleep is injected to capture waits.
"""

from __future__ import annotations

import pytest

from src import config
from src.sources import gemini_parser as G
from src.sources.gemini_parser import ParsedFiling, ParseFailure

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAPIError(Exception):
    """Mimics google.genai errors.ClientError/ServerError enough for the code:
    has `.code`, optional `.response.headers`, and `.details`."""

    def __init__(self, code: int, *, retry_after=None, retry_delay=None) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code
        self.response = None
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"Retry-After": retry_after}})()
        self.details = None
        if retry_delay is not None:
            self.details = {
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": retry_delay,
                        }
                    ]
                }
            }


def _ok(tag: str = "gemini-flash-lite") -> ParsedFiling:
    return ParsedFiling(
        revenue_cr=100.0, pat_cr=10.0, eps=1.0, opm_pct=15.0,
        revenue_yoy_pct=None, pat_yoy_pct=None,
        is_consolidated=False, has_exceptional_items=False,
        confidence="high", notes=None, parser_used=tag,  # type: ignore[arg-type]
    )


@pytest.fixture
def patch_errors(monkeypatch):
    """Make the parser treat _FakeAPIError as a retriable genai ClientError."""
    monkeypatch.setattr(G.genai_errors, "ClientError", _FakeAPIError)
    # Keep ServerError distinct so the except tuple still works.
    class _OtherServerError(Exception):
        pass
    monkeypatch.setattr(G.genai_errors, "ServerError", _OtherServerError)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetrySameTier:
    def test_429_then_success_same_tier(self, monkeypatch, patch_errors) -> None:
        calls = {"n": 0}
        waits: list[float] = []

        def fake_call(pdf, q, model, tag):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeAPIError(429)
            return _ok(tag)

        monkeypatch.setattr(G, "_call_gemini", fake_call)
        dispatches = {"n": 0}
        out = G.parse_pdf(
            b"%PDF", "Q4-FY26",
            on_dispatch=lambda: dispatches.__setitem__("n", dispatches["n"] + 1),
            sleep=waits.append,
        )
        assert out.parser_used == "gemini-flash-lite"   # succeeded on retry of tier 1
        assert calls["n"] == 2
        assert dispatches["n"] == 2                      # one per actual call
        assert len(waits) == 1                           # one backoff between the two
        assert waits[0] == config.GEMINI_BACKOFF_BASE_SECONDS  # 2 × 2^0

    def test_exponential_backoff_growth(self, monkeypatch, patch_errors) -> None:
        waits: list[float] = []
        monkeypatch.setattr(G, "_call_gemini",
                            lambda *a: (_ for _ in ()).throw(_FakeAPIError(503)))
        with pytest.raises(ParseFailure):
            G.parse_pdf(b"%PDF", "Q4-FY26", sleep=waits.append)
        # First tier: attempts 1..3 sleep (attempt 4 = no sleep, demote), then
        # second tier same. Exponential base 2: 2, 4, 8.
        base = config.GEMINI_BACKOFF_BASE_SECONDS
        assert waits[:3] == [base * 1, base * 2, base * 4]


class TestRetryAfterHonored:
    def test_retry_after_header_wins_over_exponential(self, monkeypatch, patch_errors) -> None:
        waits: list[float] = []
        calls = {"n": 0}

        def fake_call(pdf, q, model, tag):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeAPIError(429, retry_after="7")
            return _ok(tag)

        monkeypatch.setattr(G, "_call_gemini", fake_call)
        G.parse_pdf(b"%PDF", "Q4-FY26", sleep=waits.append)
        assert waits == [7.0]   # honored the header, not the 2s exponential

    def test_retry_info_delay_string_parsed(self, monkeypatch, patch_errors) -> None:
        waits: list[float] = []
        calls = {"n": 0}

        def fake_call(pdf, q, model, tag):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeAPIError(429, retry_delay="13s")
            return _ok(tag)

        monkeypatch.setattr(G, "_call_gemini", fake_call)
        G.parse_pdf(b"%PDF", "Q4-FY26", sleep=waits.append)
        assert waits == [13.0]


class TestDemoteAndCeiling:
    def test_exhaust_tier1_demotes_to_tier2(self, monkeypatch, patch_errors) -> None:
        seen_models: list[str] = []

        def fake_call(pdf, q, model, tag):
            seen_models.append(model)
            if model == config.GEMINI_PRIMARY_MODEL:
                raise _FakeAPIError(429)
            return _ok("gemini-flash")

        monkeypatch.setattr(G, "_call_gemini", fake_call)
        out = G.parse_pdf(b"%PDF", "Q4-FY26", sleep=lambda _s: None)
        assert out.parser_used == "gemini-flash"
        # Tier 1 dispatched GEMINI_RETRY_MAX_ATTEMPTS times before demoting.
        assert seen_models.count(config.GEMINI_PRIMARY_MODEL) == config.GEMINI_RETRY_MAX_ATTEMPTS
        assert config.GEMINI_FALLBACK_MODEL in seen_models

    def test_backoff_ceiling_stops_waiting(self, monkeypatch, patch_errors) -> None:
        """A row stuck behind Retry-After:60 must not exceed the cumulative
        ceiling — total sleep across the whole call stays <= ceiling."""
        waits: list[float] = []
        monkeypatch.setattr(
            G, "_call_gemini",
            lambda *a: (_ for _ in ()).throw(_FakeAPIError(429, retry_after="60")),
        )
        with pytest.raises(ParseFailure):
            G.parse_pdf(b"%PDF", "Q4-FY26", sleep=waits.append,
                        max_total_backoff_seconds=90)
        assert sum(waits) <= 90
        # 60 then would-be-120 > 90 → ceiling stops it after one 60s wait per tier.
        assert all(w == 60.0 for w in waits)


class TestNonRetriable:
    def test_non_retriable_raises_immediately(self, monkeypatch, patch_errors) -> None:
        calls = {"n": 0}

        def fake_call(pdf, q, model, tag):
            calls["n"] += 1
            raise _FakeAPIError(400)   # 400 not in _RETRY_HTTP_CODES

        monkeypatch.setattr(G, "_call_gemini", fake_call)
        with pytest.raises(ParseFailure):
            G.parse_pdf(b"%PDF", "Q4-FY26", sleep=lambda _s: None)
        assert calls["n"] == 1   # no retry, no demote


# ---------------------------------------------------------------------------
# Retry-After value parsing (pure)
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("57", 57.0),
            ("57s", 57.0),
            ("1.5s", 1.5),
            (30, 30.0),
            (None, None),
            ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date form unsupported → None
            ("", None),
            (-5, None),
        ],
    )
    def test_parse(self, value, expected) -> None:
        assert G._parse_retry_after_value(value) == expected
