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

from src.pipeline import enricher as E
from src.sources import gemini_parser as G
from src.sources.gemini_parser import ParsedFiling

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
