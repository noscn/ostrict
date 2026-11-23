"""Compile/execute pipeline with lint enforcement and plugin hooks."""

from __future__ import annotations

import weakref
from collections import OrderedDict
from collections.abc import Iterable
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

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


# on_model_loaded memos live OUTSIDE the plugin: a plugin may use __slots__ and
# have no __dict__ to write into. Weak keys let a dropped plugin free its memo.
# A plugin that cannot be weakly referenced (or hashed) goes into a small LRU
# that holds the INSTANCE strongly: the strong reference makes id() reuse
# impossible while the entry lives (a dead object's id could otherwise be
# recycled onto a new plugin, silently skipping its on_model_loaded), and the
# bound caps the cost. An evicted plugin just gets on_model_loaded again.
MAX_UNWEAKABLE_PLUGIN_CACHE = 64
_MODELS_SEEN: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_MODELS_SEEN_BY_ID: OrderedDict[int, tuple[CompilerPlugin, dict[int, weakref.ref]]] = OrderedDict()


def _plugin_memo(p: CompilerPlugin) -> dict[int, weakref.ref]:
    try:
        memo = _MODELS_SEEN.get(p)
        if memo is None:
            memo = _MODELS_SEEN[p] = {}
        return memo
    except TypeError:  # __slots__ without __weakref__, or an unhashable plugin
        entry = _MODELS_SEEN_BY_ID.get(id(p))
        if entry is None or entry[0] is not p:
            entry = _MODELS_SEEN_BY_ID[id(p)] = (p, {})
        _MODELS_SEEN_BY_ID.move_to_end(id(p))
        while len(_MODELS_SEEN_BY_ID) > MAX_UNWEAKABLE_PLUGIN_CACHE:
            _MODELS_SEEN_BY_ID.popitem(last=False)
        return entry[1]


def _notify_model_loaded(view: SemanticView, plugins: Iterable[CompilerPlugin]) -> None:
    """Fire on_model_loaded once per (plugin instance, view object).

    A reloaded spec is a new object, so it fires again — matching "model
    loaded". Views are unhashable, so the memo is id-keyed with a weakref
    guard against id reuse."""
    for p in plugins:
        seen = _plugin_memo(p)
        ref = seen.get(id(view))
        if ref is not None and ref() is view:
            continue
        if len(seen) > 64:  # prune refs whose views were garbage-collected
            for k in [k for k, r in seen.items() if r() is None]:
                del seen[k]
        seen[id(view)] = weakref.ref(view)
        p.on_model_loaded(view)

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
    plugins = list(plugins)  # a one-shot iterator must survive every hook pass
    _ensure_valid(view, lint_config)  # ERROR findings block compilation
    _notify_model_loaded(view, plugins)
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
    plugins: Iterable[CompilerPlugin] = (),
    config: CompileConfig | None = None,
    lint_config: LintConfig | None = None,
) -> CompiledQuery:
    plugins = list(plugins)  # a one-shot iterator must survive every hook pass
    _ensure_valid(view, lint_config)
    flts = [f if isinstance(f, Filter) else Filter.model_validate(f) for f in filters]
    # Plugins see value lookups too, or RBAC filters injected in before_compile
    # would not gate get_dimension_values. Hooks run on a synthetic request.
    _notify_model_loaded(view, plugins)
    request = SemanticQueryRequest(dimensions=[dimension], filters=flts, limit=limit)
    for p in plugins:
        request = p.before_compile(view, request) or request
    return build_dimension_values_query(
        view, dimension, search, list(request.filters),
        request.limit if request.limit is not None else limit,
        dialect or view.dialect, config or CompileConfig(),
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
    cfg = config or CompileConfig()
    rows, truncated = _execute_probing_limit(connector, compiled, request.limit or cfg.default_limit)
    result = QueryResult(
        columns=compiled.columns,
        rows=rows,
        truncated=truncated,
        sql=compiled.sql,
    )
    for p in plugins:
        p.on_query_executed(view, request, compiled, result)
    return result


def _execute_probing_limit(
    connector, compiled: CompiledQuery, fallback_limit: int
) -> tuple[list[dict], bool]:
    """Run the compiled query overfetching ONE row past its LIMIT, so ``truncated``
    reports a fact — row N+1 exists — instead of guessing from len(rows) == N.

    The reported/auditable SQL stays ``compiled.sql`` (what explain shows for the
    same request); only the executed statement's LIMIT literal is raised by one,
    and the extra row is dropped before anyone sees it.

    If an ``after_compile`` plugin rewrote the statement past recognition (no
    top-level numeric LIMIT), the request's own limit is enforced at the
    connector instead (``max_rows``), so the result stays bounded and the flag
    stays exact even then."""
    n: int | None = None
    try:
        tree = sqlglot.parse_one(compiled.sql, read=connector.dialect or None)
        lim = tree.args.get("limit")
        if (
            isinstance(lim, exp.Limit)
            and isinstance(lim.expression, exp.Literal)
            and not lim.expression.is_string
        ):
            n = int(lim.expression.this)
    except Exception:
        n = None
    if n is None:
        rows = connector.execute(compiled.sql, max_rows=fallback_limit + 1)
        return rows[:fallback_limit], len(rows) > fallback_limit
    lim.set("expression", exp.Literal.number(n + 1))
    rows = connector.execute(tree.sql(dialect=connector.dialect or None, pretty=True), max_rows=n + 1)
    return rows[:n], len(rows) > n
