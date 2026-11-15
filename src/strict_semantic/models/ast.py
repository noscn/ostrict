"""Canonical Internal Semantic AST (Pydantic v2).

Both parsers (Snowflake DDL and the native YAML) must produce *equal* instances
of these models for equivalent inputs. Expressions are stored as canonical SQL
strings (normalized once through sqlglot) so equality is well defined;
``parse_expr`` returns a fresh sqlglot tree on demand.

A semantic view is a blueprint only: nothing here is ever materialized.
"""

from __future__ import annotations

import difflib
from functools import lru_cache
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from sqlglot import exp

from .errors import ERR_UNKNOWN_FIELD, CompilerError

GRAINS = ("hour", "day", "week", "month", "quarter", "year")


@lru_cache(maxsize=4096)
def _parse(sql: str, dialect: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read=dialect or None)


def canonical(sql: str, dialect: str) -> str:
    """Normalize an expression through sqlglot so both parsers agree byte for byte."""
    return _parse(sql, dialect).sql(dialect=dialect or None)


class Partition(BaseModel):
    """FinOps hint: ``column`` is this table's physical partition column, bounded by
    filters on the view-wide dimension ``dimension`` within
    [value - lookback_days, value + lookahead_days]."""

    model_config = ConfigDict(extra="forbid")

    column: str
    dimension: str
    lookback_days: int = 0
    lookahead_days: int = 0


class LogicalTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source: str  # physical table reference, or a SELECT (YAML only)
    primary_key: tuple[str, ...] = ()
    comment: str = ""
    primary_date: str | None = None  # from 'Primary date: <col>.'
    snapshot: bool = False  # from 'Point-in-time snapshot.'
    partition: Partition | None = None
    filter: str | None = None  # always-on row predicate (YAML only)


class Relationship(BaseModel):
    """A directed many-to-one edge: child (fk columns) references parent's full PK."""

    model_config = ConfigDict(extra="forbid")

    name: str
    child: str
    child_columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]


class Fact(BaseModel):
    """A reusable row-level expression on one table; referenced by metrics by name."""

    model_config = ConfigDict(extra="forbid")

    table: str
    name: str
    expr: str
    comment: str = ""


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    name: str
    expr: str
    comment: str = ""
    synonyms: tuple[str, ...] = ()
    grains: tuple[str, ...] = ()  # non-empty marks a time dimension
    time_type: Literal["date", "timestamp"] = "date"
    values: tuple[str, ...] = ()  # declared enumeration from 'Values: ...'

    @property
    def is_time(self) -> bool:
        return bool(self.grains)


class Metric(BaseModel):
    """An aggregate on one table — or, when ``table`` is None, a derived metric
    that arithmetically combines other metrics (SV-R5.8)."""

    model_config = ConfigDict(extra="forbid")

    table: str | None = None
    name: str
    expr: str
    comment: str = ""
    synonyms: tuple[str, ...] = ()
    non_additive_by: str | None = None

    @property
    def is_derived(self) -> bool:
        return self.table is None


Exposed = Dimension | Metric


class SemanticView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dialect: str = "snowflake"  # dialect the stored expressions are written in
    comment: str = ""
    ai_sql_generation: str = ""
    tables: dict[str, LogicalTable] = Field(default_factory=dict)
    relationships: tuple[Relationship, ...] = ()
    facts: tuple[Fact, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    metrics: tuple[Metric, ...] = ()

    _field_map: dict | None = PrivateAttr(default=None)

    # ---- expressions ------------------------------------------------------

    def parse_expr(self, sql: str) -> exp.Expression:
        return _parse(sql, self.dialect).copy()  # copy: the cached tree is shared

    # ---- lookups ----------------------------------------------------------

    def exposed(self) -> list[Exposed]:
        return [*self.dimensions, *self.metrics]

    def facts_of(self, table: str) -> dict[str, Fact]:
        return {f.name: f for f in self.facts if f.table == table}

    def _map(self) -> dict[str, Exposed]:
        if self._field_map is None:
            m: dict[str, Exposed] = {}
            for f in self.exposed():
                for key in (f.name, *f.synonyms):
                    m.setdefault(key, f)  # duplicates are the linter's job (SV-R6.4/R8.5)
            self._field_map = m
        return self._field_map

    def _unknown(self, ref: str, kind: str, names: list[str]) -> CompilerError:
        close = difflib.get_close_matches(ref, names, n=3)
        hint = f"Did you mean: {', '.join(close)}?" if close else f"Available: {', '.join(sorted(names)[:20])}"
        return CompilerError(ERR_UNKNOWN_FIELD, f"unknown {kind} '{ref}' in view '{self.name}'. {hint}")

    def find_dimension(self, ref: str) -> tuple[Dimension, str | None]:
        """Resolve a dimension by name or synonym; a ``__grain`` suffix selects a time grain."""
        base, grain = ref.strip(), None
        if "__" in base:
            head, _, tail = base.rpartition("__")
            if tail in GRAINS:
                base, grain = head, tail
        f = self._map().get(base)
        if not isinstance(f, Dimension):
            names = [d.name for d in self.dimensions]
            raise self._unknown(ref, "dimension", names)
        if grain and grain not in f.grains:
            have = ", ".join(f.grains) if f.grains else "none (not a time dimension)"
            raise CompilerError(
                ERR_UNKNOWN_FIELD, f"dimension '{f.name}' has no grain '{grain}'; available grains: {have}"
            )
        return f, grain

    def find_metric(self, ref: str) -> Metric:
        f = self._map().get(ref.strip())
        if not isinstance(f, Metric):
            raise self._unknown(ref, "metric", [m.name for m in self.metrics])
        return f

    def base_metrics(self, metric: Metric, _stack: tuple[str, ...] = ()) -> list[Metric]:
        """The aggregate metrics a (possibly derived) metric is computed from, in order."""
        if not metric.is_derived:
            return [metric]
        if metric.name in _stack:
            chain = " -> ".join((*_stack, metric.name))
            raise CompilerError(ERR_UNKNOWN_FIELD, f"circular derived metric: {chain}")
        out: list[Metric] = []
        for col in self.parse_expr(metric.expr).find_all(exp.Column):
            ref = self.find_metric(col.name)
            for m in self.base_metrics(ref, (*_stack, metric.name)):
                if m not in out:
                    out.append(m)
        return out
