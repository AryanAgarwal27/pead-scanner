"""Phase 3 parser-quality fixes.

Fix A:
    BUG 2 — _call_gemini disables thinking + raises max_output_tokens so the
            gemini-2.5-flash fallback stops truncating its JSON.
    BUG 3 — a tripped Z-CHECK downgrades a high/medium parse to 'low' before it
            is persisted, so a standalone-vs-consolidated mis-extraction can't
            clear Phase 4's confidence floor and feed a bogus signal.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.pipeline import enricher as E
from src.sources import gemini_parser as G
from src.sources.gemini_parser import ParsedFiling, _ColumnValidationFailure, _validate_columns

# ---------------------------------------------------------------------------
# BUG 2 — generation config
# ---------------------------------------------------------------------------


class TestGenerationConfig:
    def test_call_gemini_disables_thinking_and_raises_cap(self, monkeypatch) -> None:
        captured = {}

        class _FakeModels:
            def generate_content(self, *, model, contents, config):
                captured["config"] = config
                resp = MagicMock()
                resp.parsed = G.GeminiResponse(
                    source_table="Quarter ended March 31, 2026",
                    column_label="Quarter ended 31.03.2026",
                    column_period="quarter",
                    revenue_raw=100.0,
                    pat_raw=10.0,
                    unit="crores",
                    confidence="high",
                )
                return resp

        class _FakeClient:
            models = _FakeModels()

        monkeypatch.setattr(G, "_get_client", lambda: _FakeClient())
        # Isolate the config assertion from the validator/schema (Fix B changes those).
        monkeypatch.setattr(G, "_validate_columns", lambda *a, **k: None)

        G._call_gemini(b"%PDF", "Q4-FY26", "gemini-2.5-flash", "gemini-flash")

        cfg = captured["config"]
        assert cfg.thinking_config is not None
        assert cfg.thinking_config.thinking_budget == 0   # thinking disabled
        assert cfg.max_output_tokens == 2048               # raised from 1024


# ---------------------------------------------------------------------------
# BUG 3 — Z-CHECK confidence downgrade
# ---------------------------------------------------------------------------


def _high_parse() -> ParsedFiling:
    return ParsedFiling(
        revenue_cr=23.7, pat_cr=6.2, eps=1.0, opm_pct=12.0,
        revenue_yoy_pct=None, pat_yoy_pct=None,
        is_consolidated=False, has_exceptional_items=False,
        confidence="high", notes=None, parser_used="gemini-flash-lite",
    )


def _wire_process_one(monkeypatch, *, parse: ParsedFiling, z_tripped: bool) -> dict:
    """Stub a fresh-parse _process_one run; capture what _persist_parse receives."""
    monkeypatch.setattr(E, "_parse_filing", lambda f, q, *, gate=None: parse)
    monkeypatch.setattr(
        E, "_load_fundamentals",
        lambda db, t: {"on_screener": True,
                       "quarterly_rev": [{"quarter": "Q4-FY26", "value": 206.0}]},
    )
    monkeypatch.setattr(E, "_z_check", lambda *a: z_tripped)
    monkeypatch.setattr(E.yfa, "fetch_ohlcv", lambda *a: None)   # defer metrics → early return
    monkeypatch.setattr(E.yfa, "fetch_nifty", lambda *a: None)
    captured: dict = {}
    monkeypatch.setattr(
        E, "_persist_parse",
        lambda db, fid, parsed: captured.update(
            confidence=parsed.confidence, notes=parsed.notes
        ),
    )
    return captured


def _filing() -> dict:
    return {
        "id": 227, "symbol": "MCLOUD", "source": "NSE", "quarter": "Q4-FY26",
        "filing_time": "2026-05-14T10:00:00+00:00", "parsed_at": None,
    }


class TestValidateColumns:
    """Fix B (BUG 1): validation trusts the model's column_period declaration,
    not a literal 'quarter' substring — so a Q4 quarter column inside a
    'year ended' statement with a bare-date label is accepted."""

    def test_quarter_period_with_date_passes(self) -> None:
        # The Marksans-type case that the old word-match wrongly rejected.
        _validate_columns(
            "quarter",
            "STATEMENT OF AUDITED STANDALONE FINANCIAL RESULTS FOR THE YEAR ENDED 31 MARCH 2026",
            "31 Mar 2026 (AUDITED)",   # bare date, no 'quarter' token
            "Q4-FY26",
        )  # must not raise

    def test_year_period_is_rejected(self) -> None:
        with pytest.raises(_ColumnValidationFailure, match="column_period='year'"):
            _validate_columns(
                "year",
                "STATEMENT ... FOR THE YEAR ENDED 31 MARCH 2026",
                "31 Mar 2026 (Audited)",
                "Q4-FY26",
            )

    def test_half_year_period_is_rejected(self) -> None:
        with pytest.raises(_ColumnValidationFailure):
            _validate_columns(
                "half_year",
                "PROFIT & LOSS FOR THE HALF AND YEAR ENDED 31ST MARCH, 2026",
                "31st March, 2026",
                "Q4-FY26",
            )

    def test_quarter_period_wrong_date_rejected(self) -> None:
        # Right period, but the column references a different quarter-end date.
        with pytest.raises(_ColumnValidationFailure, match="expected one of"):
            _validate_columns(
                "quarter",
                "Quarter ended 31 December 2025",
                "31.12.2025",
                "Q4-FY26",   # expects 31 March 2026
            )


class TestConsolidatedDivergenceGuard:
    """Fix B divergence guard: a parse that passed STANDALONE numbers while a
    materially-divergent CONSOLIDATED PAT exists in the same filing is downgraded
    to 'low' (so Phase 4's {high,medium} floor excludes it). Uses the model's
    in-PDF consolidated_pat_raw / standalone_pat_raw cross-check fields."""

    @staticmethod
    def _resp(**over) -> G.GeminiResponse:
        base = dict(
            source_table="Quarter ended 31 March 2026",
            column_label="31.03.2026 (Audited)",
            column_period="quarter",
            revenue_raw=200.0,
            pat_raw=23.7,
            unit="crores",
            confidence="high",
        )
        base.update(over)
        return G.GeminiResponse(**base)

    # --- the helper in isolation -------------------------------------------
    def test_standalone_used_and_diverges_returns_note(self) -> None:
        r = self._resp(is_consolidated=False, standalone_pat_raw=23.7, consolidated_pat_raw=206.0)
        note = G._consolidated_divergence_note(r)
        assert note is not None and "diverge" in note

    def test_standalone_used_within_threshold_no_note(self) -> None:
        # 100 vs 110 -> 9.1% < 25%.
        r = self._resp(is_consolidated=False, standalone_pat_raw=100.0, consolidated_pat_raw=110.0)
        assert G._consolidated_divergence_note(r) is None

    def test_consolidated_used_never_downgrades(self) -> None:
        # Even with a huge gap, if we used the consolidated basis we have the right number.
        r = self._resp(is_consolidated=True, standalone_pat_raw=23.7, consolidated_pat_raw=206.0)
        assert G._consolidated_divergence_note(r) is None

    def test_unknown_basis_with_divergence_downgrades(self) -> None:
        # is_consolidated None is "not confirmed consolidated" -> guard applies.
        r = self._resp(is_consolidated=None, standalone_pat_raw=23.7, consolidated_pat_raw=206.0)
        assert G._consolidated_divergence_note(r) is not None

    def test_only_one_basis_present_no_note(self) -> None:
        assert G._consolidated_divergence_note(
            self._resp(is_consolidated=False, standalone_pat_raw=23.7, consolidated_pat_raw=None)
        ) is None
        assert G._consolidated_divergence_note(
            self._resp(is_consolidated=False, standalone_pat_raw=None, consolidated_pat_raw=206.0)
        ) is None

    def test_both_zero_no_divide_by_zero(self) -> None:
        r = self._resp(is_consolidated=False, standalone_pat_raw=0.0, consolidated_pat_raw=0.0)
        assert G._consolidated_divergence_note(r) is None

    # --- end-to-end through _to_parsed_filing ------------------------------
    def test_to_parsed_filing_downgrades_high_to_low(self) -> None:
        r = self._resp(
            confidence="high", is_consolidated=False,
            standalone_pat_raw=23.7, consolidated_pat_raw=206.0,
        )
        parsed = G._to_parsed_filing(r, "gemini-flash-lite")
        assert parsed.confidence == "low"
        assert "cons-divergence" in (parsed.notes or "")

    def test_to_parsed_filing_keeps_high_when_aligned(self) -> None:
        r = self._resp(
            confidence="high", is_consolidated=True,
            standalone_pat_raw=23.7, consolidated_pat_raw=206.0,
        )
        parsed = G._to_parsed_filing(r, "gemini-flash-lite")
        assert parsed.confidence == "high"


class TestZCheckDowngrade:
    def test_tripped_high_confidence_downgraded_to_low(self, monkeypatch) -> None:
        captured = _wire_process_one(monkeypatch, parse=_high_parse(), z_tripped=True)
        E._process_one(MagicMock(), _filing(), dry_run=False)
        assert captured["confidence"] == "low"          # persisted as low
        assert "z-check downgrade" in (captured["notes"] or "")

    def test_not_tripped_keeps_high_confidence(self, monkeypatch) -> None:
        captured = _wire_process_one(monkeypatch, parse=_high_parse(), z_tripped=False)
        E._process_one(MagicMock(), _filing(), dry_run=False)
        assert captured["confidence"] == "high"          # untouched when check passes

    def test_already_low_is_left_alone(self, monkeypatch) -> None:
        low = _high_parse()
        low.confidence = "low"
        captured = _wire_process_one(monkeypatch, parse=low, z_tripped=True)
        E._process_one(MagicMock(), _filing(), dry_run=False)
        assert captured["confidence"] == "low"
        # No spurious downgrade note appended to an already-low parse.
        assert "z-check downgrade" not in (captured["notes"] or "")

    def test_downgrade_happens_before_persist(self, monkeypatch) -> None:
        """Regression: the persisted object must already carry 'low' — i.e. the
        Z-CHECK runs and mutates BEFORE _persist_parse, not after."""
        parse = _high_parse()
        captured = _wire_process_one(monkeypatch, parse=parse, z_tripped=True)
        E._process_one(MagicMock(), _filing(), dry_run=False)
        assert captured["confidence"] == "low"
