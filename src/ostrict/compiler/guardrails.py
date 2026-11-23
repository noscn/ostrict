"""FinOps and correctness guardrails applied at compile time.

- SV-R10.7  metric-required (no raw-row dumps)
- SV-R7     snapshot tables need a point-in-time binding
-           partition filter requirement + pruning bounds
-           LIMIT injection and cap
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from ..models.ast import Dimension, LogicalTable, SemanticView
from ..models.errors import (
    ERR_EXCESSIVE_LIMIT,
    ERR_INVALID_FILTER,
    ERR_METRIC_REQUIRED,
    ERR_MISSING_PARTITION_FILTER,
    ERR_SNAPSHOT_AGGREGATION_VIOLATION,
    CompilerError,
)
from ..models.query import Filter

# upper-bound slack per grain: pruning windows must be a superset of the data asked for
_GRAIN_DAYS = {"hour": 1, "day": 1, "week": 7, "month": 31, "quarter": 92, "year": 366}


@dataclass(frozen=True)
class CompileConfig:
    default_limit: int = 1000
    max_limit: int = 10000
    allow_full_scan: bool = False  # authorized admins only: waives the partition requirement


def require_metrics(metrics: list) -> None:
    if not metrics:
        raise CompilerError(
            ERR_METRIC_REQUIRED,
            "a semantic query needs at least one metric; raw-row dumps are not served",
            hint="to discover filter values, use get_dimension_values(dimension, search, limit)",
        )


def resolve_limit(limit: int | None, config: CompileConfig) -> int:
    if limit is None:
        return config.default_limit
    if not isinstance(limit, int) or limit < 1:
        raise CompilerError(ERR_EXCESSIVE_LIMIT, f"limit must be a positive integer, got {limit!r}")
    if limit > config.max_limit:
        raise CompilerError(
            ERR_EXCESSIVE_LIMIT, f"limit {limit} exceeds the configured cap of {config.max_limit}"
        )
    return limit


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shift(value: str, days: int) -> str:
    t = _parse_iso(value) + dt.timedelta(days=days)
    return t.date().isoformat() if len(value) == 10 else t.isoformat(sep=" ")


def _bounds_from(filters: list[tuple[Dimension, str | None, Filter]], dimension: str):
    """(lower, upper) ISO-string bound lists implied by filters on `dimension`."""
    lowers: list[str] = []
    uppers: list[str] = []
    for dim, grain, flt in filters:
        if dim.name != dimension:
            continue
        op, v = flt.op.lower(), flt.value
        slack = _GRAIN_DAYS.get(grain, 0)
        if op == "between" and isinstance(v, (list, tuple)) and len(v) == 2:
            lo, hi = v
            if _parse_iso(lo):
                lowers.append(str(lo))
            if _parse_iso(hi):
                uppers.append(_shift(str(hi), slack))
        elif op in ("=", ">=", ">") and _parse_iso(v):
            lowers.append(str(v))
            if op == "=":
                uppers.append(_shift(str(v), slack))
        elif op in ("<", "<=") and _parse_iso(v):
            uppers.append(str(v))
        elif op == "in" and isinstance(v, (list, tuple)) and v and all(_parse_iso(x) for x in v):
            if len({_parse_iso(x).tzinfo is not None for x in v}) > 1:
                raise CompilerError(
                    ERR_INVALID_FILTER,
                    f"filter on '{flt.field}': 'in' mixes timezone-aware and timezone-naive "
                    "date/time values, which cannot be ordered",
                    hint="write every value in the same form, e.g. all '2026-01-01' "
                    "or all '2026-01-01T00:00:00+00:00'",
                )
            vals = sorted((str(x) for x in v), key=_parse_iso)
            lowers.append(vals[0])
            uppers.append(_shift(vals[-1], slack))
    return lowers, uppers


def snapshot_binding(
    view: SemanticView, table: LogicalTable, dims: list[tuple[Dimension, str | None]],
    dim_filters: list[tuple[Dimension, str | None, Filter]],
) -> None:
    """SV-R7: a snapshot table is queried at a point in time, or per snapshot date."""
    date_dim = next(
        (d for d in view.dimensions if d.table == table.name and d.expr == table.primary_date), None
    )
    if date_dim is None:  # the linter already demands this; defensive
        raise CompilerError(
            ERR_SNAPSHOT_AGGREGATION_VIOLATION,
            f"snapshot table '{table.name}' exposes no dimension on '{table.primary_date}'",
        )
    pinned = any(
        d.name == date_dim.name and f.op == "=" and grain in (None, "day")
        for d, grain, f in dim_filters
    )
    per_date = any(d.name == date_dim.name and grain in (None, "day") for d, grain in dims)
    if not (pinned or per_date):
        raise CompilerError(
            ERR_SNAPSHOT_AGGREGATION_VIOLATION,
            f"metrics on snapshot table '{table.name}' must be pinned to one date "
            f"(filter {date_dim.name} = 'YYYY-MM-DD') or grouped by '{date_dim.name}'; "
            "summing snapshots across dates double-counts",
        )


def partition_predicates(
    view: SemanticView, table: LogicalTable,
    dim_filters: list[tuple[Dimension, str | None, Filter]],
    config: CompileConfig,
) -> list[exp.Expression]:
    """Require a bounding filter on the partition dimension and derive pruning
    predicates on the physical partition column (pushed into the table subquery,
    which is the only placement BigQuery honors for joined tables)."""
    p = table.partition
    if p is None:
        return []
    lowers, uppers = _bounds_from(dim_filters, p.dimension)
    if not lowers and not config.allow_full_scan:
        raise CompilerError(
            ERR_MISSING_PARTITION_FILTER,
            f"table '{table.name}' is partitioned; add a bounding filter on '{p.dimension}' "
            "(=, >=, >, between or in with date values)",
            hint=f"e.g. {{\"field\": \"{p.dimension}\", \"op\": \">=\", \"value\": \"2026-01-01\"}}",
        )
    col = exp.column(p.column, table=table.name)
    out: list[exp.Expression] = []
    for v in lowers:
        out.append(exp.GTE(this=col.copy(), expression=exp.convert(_shift(v, -p.lookback_days))))
    for v in uppers:
        out.append(exp.LTE(this=col.copy(), expression=exp.convert(_shift(v, p.lookahead_days))))
    return out
