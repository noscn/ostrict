"""Golden Questions eval runner (SV-R10.2): deterministic grading, no LLM judge.

A cases file pins the semantic view, an optional DuckDB seed, and a list of
cases. `answer` cases compile, execute and compare rows exactly (order-free,
numbers as floats, dates as ISO strings); `refuse` and `clarify` cases expect a
specific error code (clarify marks requests an agent should repair, e.g. a typo
with suggestions).

    view: ./ecommerce.sql
    seed: ./seed_ecommerce.sql
    cases:
      - id: totals_january
        family: revenue
        kind: answer            # answer | refuse | clarify
        question: "What was total revenue in January 2026?"
        request: { metrics: [revenue], filters: [...] }
        expect:
          rows: [{ revenue: 94.0 }]
      - id: fanout
        kind: refuse
        request: { metrics: [customer_count], dimensions: [channel] }
        expect: { error: ERR_FANOUT_JOIN_FORBIDDEN, message_contains: ancestor }
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from ..compiler import compile_semantic_query, execute_semantic_query
from ..connectors.duckdb import DuckDBConnector
from ..models.errors import CompilerError
from ..parsers import load_view


@dataclass
class CaseResult:
    id: str
    kind: str
    passed: bool
    detail: str = ""


@dataclass
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def render(self) -> str:
        lines = [
            f"{'PASS' if r.passed else 'FAIL'}  {r.id} ({r.kind})" + (f": {r.detail}" if r.detail else "")
            for r in self.results
        ]
        n = sum(r.passed for r in self.results)
        return "\n".join([*lines, f"{n}/{len(self.results)} golden cases passed"])


def _norm(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        # engines disagree on whether DATE_TRUNC returns a date or a midnight
        # timestamp; expectations are written as plain dates
        if v.tzinfo is None and v.time() == dt.time.min:
            return v.date().isoformat()
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


def _rowset(rows: list[dict]) -> list[str]:
    return sorted(json.dumps({k: _norm(v) for k, v in r.items()}, sort_keys=True, default=str) for r in rows)


def run_eval(cases_path: str | Path, seed: str | None = None) -> EvalReport:
    path = Path(cases_path)
    spec = yaml.safe_load(path.read_text())
    base = path.parent
    view = load_view(base / spec["view"])
    seed_path = Path(seed) if seed else (base / spec["seed"] if spec.get("seed") else None)
    connector = DuckDBConnector(seed_sql=seed_path.read_text() if seed_path else None)

    report = EvalReport()
    for case in spec.get("cases", []):
        cid, kind = str(case.get("id")), case.get("kind", "answer")
        expect = case.get("expect") or {}
        try:
            if kind == "answer":
                result = execute_semantic_query(view, case["request"], connector)
                got, want = _rowset(result.rows), _rowset(expect.get("rows", []))
                if got == want:
                    report.results.append(CaseResult(cid, kind, True))
                else:
                    report.results.append(CaseResult(cid, kind, False, f"rows {got} != expected {want}"))
            else:  # refuse | clarify: the compiler must say no, with the exact code
                try:
                    compile_semantic_query(view, case["request"], dialect=connector.dialect)
                    report.results.append(CaseResult(cid, kind, False, "compiled but a refusal was expected"))
                except CompilerError as e:
                    want_code = expect.get("error")
                    needle = expect.get("message_contains", "")
                    said = f"{e} {e.hint}".lower()
                    if e.code == want_code and needle.lower() in said:
                        report.results.append(CaseResult(cid, kind, True))
                    else:
                        report.results.append(
                            CaseResult(cid, kind, False, f"got [{e.code}] {e}; expected [{want_code}] ~'{needle}'")
                        )
        except Exception as e:  # noqa: BLE001 - a crashing case is a failing case
            report.results.append(CaseResult(cid, kind, False, f"{type(e).__name__}: {e}"))
    return report
