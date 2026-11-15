"""Compile/execute pipeline with lint enforcement and plugin hooks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..models.ast import SemanticView
from ..models.query import CompiledQuery, Filter, QueryResult, SemanticQueryRequest
from ..plugins.base import CompilerPlugin
from .guardrails import CompileConfig
from .resolver import Graph, build_graph
from .sql_builder import build_dimension_values_query, build_query

if TYPE_CHECKING:
    from ..linter import LintConfig


def _ensure_valid(view, lint_config):
    from ..linter import ensure_valid  # deferred: the linter itself uses compiler.resolver

    return ensure_valid(view, lint_config)

__all__ = [
    "CompileConfig",
    "Graph",
    "build_graph",
    "compile_dimension_values",
    "compile_semantic_query",
    "execute_semantic_query",
]


def compile_semantic_query(
    view: SemanticView,
    request: SemanticQueryRequest | dict,
    dialect: str | None = None,
    plugins: Iterable[CompilerPlugin] = (),
    config: CompileConfig | None = None,
    lint_config: LintConfig | None = None,
) -> CompiledQuery:
    if isinstance(request, dict):
        request = SemanticQueryRequest.model_validate(request)
    _ensure_valid(view, lint_config)  # ERROR findings block compilation
    for p in plugins:
        request = p.before_compile(view, request) or request
    compiled = build_query(view, request, dialect or view.dialect, config or CompileConfig())
    for p in plugins:
        compiled = p.after_compile(view, request, compiled) or compiled
    return compiled


def compile_dimension_values(
    view: SemanticView,
    dimension: str,
    search: str | None = None,
    filters: Iterable[Filter | dict] = (),
    limit: int = 50,
    dialect: str | None = None,
    config: CompileConfig | None = None,
    lint_config: LintConfig | None = None,
) -> CompiledQuery:
    _ensure_valid(view, lint_config)
    flts = [f if isinstance(f, Filter) else Filter.model_validate(f) for f in filters]
    return build_dimension_values_query(
        view, dimension, search, flts, limit, dialect or view.dialect, config or CompileConfig()
    )


def execute_semantic_query(
    view: SemanticView,
    request: SemanticQueryRequest | dict,
    connector,
    plugins: Iterable[CompilerPlugin] = (),
    config: CompileConfig | None = None,
    lint_config: LintConfig | None = None,
) -> QueryResult:
    if isinstance(request, dict):
        request = SemanticQueryRequest.model_validate(request)
    plugins = list(plugins)
    compiled = compile_semantic_query(
        view, request, dialect=connector.dialect, plugins=plugins, config=config, lint_config=lint_config
    )
    rows = connector.execute(compiled.sql)
    cfg = config or CompileConfig()
    result = QueryResult(
        columns=compiled.columns,
        rows=rows,
        truncated=len(rows) >= (request.limit or cfg.default_limit),
        sql=compiled.sql,
    )
    for p in plugins:
        p.on_query_executed(view, request, compiled, result)
    return result
