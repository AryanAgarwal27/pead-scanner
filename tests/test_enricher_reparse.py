"""Unit tests for the Phase 3 rate-limit-fix additions to the enricher:
    - _GeminiGate: inter-call spacing (RPM) + daily CALL budget
    - _select_reparse / _select_by_ids: selection-mode SQL
    - _invalidate_for_reparse: per-row invalidation order
    - enrich_pending(reparse=True): the budget-trip-mid-run failure mode —
      untouched rows are NOT invalidated, so they survive intact for next run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.pipeline import enricher as E
from src.pipeline.enricher import EnrichOutcome, _GeminiGate

# ---------------------------------------------------------------------------
# _GeminiGate
# ---------------------------------------------------------------------------


class TestGeminiGate:
    def test_budget_counts_calls_and_exhausts(self) -> None:
        gate = _GeminiGate(limit=3, target_rpm=0, sleep=lambda _s: None)  # rpm=0 → no spacing
        assert not gate.exhausted
        gate.on_dispatch()
        gate.on_dispatch()
        assert gate.used == 2 and not gate.exhausted
        gate.on_dispatch()
        assert gate.used == 3 and gate.exhausted     # >= limit

    def test_spacing_sleeps_remaining_interval(self) -> None:
        now = [0.0]
        slept: list[float] = []
        gate = _GeminiGate(
            limit=100, target_rpm=60,            # min_interval = 1.0s
            sleep=slept.append, clock=lambda: now[0],
        )
        gate.on_dispatch()           # first call: no sleep, last=0
        now[0] = 0.3                 # only 0.3s elapsed
        gate.on_dispatch()           # must sleep the remaining 0.7s
        assert slept == [0.7]

    def test_no_sleep_when_interval_satisfied(self) -> None:
        now = [0.0]
        slept: list[float] = []
        gate = _GeminiGate(limit=100, target_rpm=60, sleep=slept.append, clock=lambda: now[0])
        gate.on_dispatch()
        now[0] = 5.0                 # plenty of time passed
        gate.on_dispatch()
        assert slept == []           # no throttle needed


# ---------------------------------------------------------------------------
# Selection-mode SQL — a fake query recorder
# ---------------------------------------------------------------------------


@dataclass
class _RecQuery:
    db: _RecDB
    table: str
    calls: list[tuple] = field(default_factory=list)
    data: list[dict] = field(default_factory=list)

    def _rec(self, *call) -> _RecQuery:
        self.calls.append(call)
        return self

    def select(self, cols: str) -> _RecQuery:
        return self._rec("select", cols)

    def gte(self, c, v) -> _RecQuery:
        return self._rec("gte", c, v)

    def lt(self, c, v) -> _RecQuery:
        return self._rec("lt", c, v)

    def or_(self, expr) -> _RecQuery:
        return self._rec("or", expr)

    def in_(self, c, v) -> _RecQuery:
        return self._rec("in", c, v)

    def eq(self, c, v) -> _RecQuery:
        return self._rec("eq", c, v)

    def update(self, p) -> _RecQuery:
        return self._rec("update", p)

    def delete(self) -> _RecQuery:
        return self._rec("delete")

    def order(self, c) -> _RecQuery:
        return self._rec("order", c)

    def execute(self):
        self.db.executed.append((self.table, list(self.calls)))
        return type("Resp", (), {"data": self.data})()


@dataclass
class _RecDB:
    canned: dict[str, list[dict]] = field(default_factory=dict)
    executed: list[tuple] = field(default_factory=list)
    queries: list[_RecQuery] = field(default_factory=list)

    def table(self, name: str) -> _RecQuery:
        q = _RecQuery(self, name, data=self.canned.get(name, []))
        self.queries.append(q)
        return q


class TestSelectionSql:
    def test_reparse_uses_or_filter(self) -> None:
        db = _RecDB(canned={"filings": [{"id": 1}]})
        out = E._select_reparse(db, window_days=14)
        assert out == [{"id": 1}]
        q = db.queries[0]
        or_calls = [c for c in q.calls if c[0] == "or"]
        assert or_calls == [("or", "parser_used.eq.regex,parser_confidence.in.(low,failed)")]
        # window applied as a gte on filing_time
        assert any(c[0] == "gte" and c[1] == "filing_time" for c in q.calls)

    def test_by_ids_bypasses_window(self) -> None:
        db = _RecDB(canned={"filings": [{"id": 7}, {"id": 8}]})
        out = E._select_by_ids(db, [7, 8])
        assert out == [{"id": 7}, {"id": 8}]
        q = db.queries[0]
        assert ("in", "id", [7, 8]) in q.calls
        assert not any(c[0] in ("gte", "lt") for c in q.calls)   # no window bound

    def test_invalidate_nulls_parsed_at_then_deletes_metrics(self) -> None:
        db = _RecDB()
        E._invalidate_for_reparse(db, 42)
        # Two statements, in order: filings UPDATE parsed_at=None, then metrics DELETE.
        assert db.executed[0][0] == "filings"
        assert ("update", {"parsed_at": None}) in db.executed[0][1]
        assert ("eq", "id", 42) in db.executed[0][1]
        assert db.executed[1][0] == "metrics"
        assert ("delete",) in db.executed[1][1]
        assert ("eq", "filing_id", 42) in db.executed[1][1]


# ---------------------------------------------------------------------------
# Budget-trip-mid-reparse failure mode (answer to review point C)
# ---------------------------------------------------------------------------


class TestReparseBudgetTrip:
    def test_unreached_rows_are_not_invalidated(self, monkeypatch) -> None:
        """With budget=2 and 5 reparse targets, exactly 2 rows are processed +
        invalidated; the other 3 are never touched (so they survive intact and
        are re-selected next run — not orphaned)."""
        rows = [{"id": i, "symbol": f"S{i}", "source": "NSE", "quarter": "Q4-FY26",
                 "filing_time": "2026-05-14T10:00:00+00:00", "parsed_at": "x"} for i in range(1, 6)]
        monkeypatch.setattr(E, "_log_aged_out", lambda _db: None)
        monkeypatch.setattr(E, "_select_reparse", lambda _db, *, window_days=None: rows)

        invalidated: list[int] = []
        monkeypatch.setattr(E, "_invalidate_for_reparse",
                            lambda _db, fid: invalidated.append(fid))

        def fake_process_one(_db, f, *, dry_run, gate=None):
            gate.on_dispatch()   # simulate one real Gemini call → consumes budget
            return EnrichOutcome(filing_id=f["id"], symbol=f["symbol"],
                                 parser_used="gemini-flash-lite", parser_confidence="high",
                                 metrics_inserted=True, z_check_tripped=False)

        monkeypatch.setattr(E, "_process_one", fake_process_one)

        gate = _GeminiGate(limit=2, target_rpm=0, sleep=lambda _s: None)
        outcomes = E.enrich_pending(_RecDB(), reparse=True, gate=gate)

        assert len(outcomes) == 2                 # only 2 processed before budget tripped
        assert invalidated == [1, 2]              # rows 3,4,5 NEVER invalidated
        assert gate.used == 2

    def test_budget_check_precedes_invalidation(self, monkeypatch) -> None:
        """If the gate is already exhausted on entry, ZERO rows are invalidated."""
        rows = [{"id": 1, "symbol": "S1", "source": "NSE", "quarter": "Q4-FY26",
                 "filing_time": "2026-05-14T10:00:00+00:00", "parsed_at": "x"}]
        monkeypatch.setattr(E, "_log_aged_out", lambda _db: None)
        monkeypatch.setattr(E, "_select_reparse", lambda _db, *, window_days=None: rows)
        invalidated: list[int] = []
        monkeypatch.setattr(E, "_invalidate_for_reparse",
                            lambda _db, fid: invalidated.append(fid))
        monkeypatch.setattr(E, "_process_one",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))

        gate = _GeminiGate(limit=0, target_rpm=0, sleep=lambda _s: None)  # already exhausted
        outcomes = E.enrich_pending(_RecDB(), reparse=True, gate=gate)
        assert outcomes == []
        assert invalidated == []
