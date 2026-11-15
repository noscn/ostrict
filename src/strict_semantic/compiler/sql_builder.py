"""Deterministic compiler: structured request -> one SQL statement, via sqlglot ASTs.

Pipeline (per request):
  1. SV-R10.7: at least one metric; resolve names (synonyms, ``__grain`` suffixes)
  2. expand derived metrics; group base metrics by their home table
  3. SV-R3.4: every dimension and filter must be reachable from EVERY home table
     along child->parent paths (otherwise the join would fan out — refused)
  4. one aggregate subquery per home table (joins sorted by path length then name,
     so the emitted SQL is byte-for-byte identical across runs)
  5. several home tables: drill across via UNION ALL + GROUP BY + MAX — metrics of
     different grains combine without double counting (NULL dimension values group
     correctly, which outer joins would not do)
  6. outer SELECT: derived metrics (safe division), metric filters, ORDER BY, LIMIT
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from ..models.ast import Dimension, Metric, SemanticView
from ..models.errors import (
    ERR_FANOUT_JOIN_FORBIDDEN,
    ERR_INVALID_FILTER,
    ERR_UNKNOWN_FIELD,
    ERR_UNKNOWN_TABLE,
    CompilerError,
)
from ..models.query import FILTER_OPS, CompiledQuery, Filter, SemanticQueryRequest
from .guardrails import (
    CompileConfig,
    partition_predicates,
    require_metrics,
    resolve_limit,
    snapshot_binding,
)
from .resolver import Graph, build_graph

_OPS = {
    "=": exp.EQ,
    "!=": exp.NEQ,
    "<>": exp.NEQ,
    ">": exp.GT,
    ">=": exp.GTE,
    "<": exp.LT,
    "<=": exp.LTE,
}
_SELECT_SOURCE = re.compile(r"^\s*(select|with)\b", re.I)


# --------------------------------------------------------------------------- expressions


def _replace(root: exp.Expression, node: exp.Expression, new: exp.Expression) -> exp.Expression:
    if node is root:
        return new
    node.replace(new)
    return root


def resolve_expr(
    view: SemanticView, expr_sql: str, home: str, _stack: tuple[str, ...] = ()
) -> tuple[exp.Expression, set[str]]:
    """Qualify every column with its logical table alias and inline same-table facts.

    Bare columns belong to ``home``; ``other_table.column`` reaches an ancestor
    (reachability is checked by the caller). Returns (tree, logical tables used).
    """
    tree = view.parse_expr(expr_sql)
    tables = {home}
    for col in list(tree.find_all(exp.Column)):
        owner = col.table or home
        if owner not in view.tables:
            raise CompilerError(
                ERR_UNKNOWN_TABLE, f"expression '{expr_sql}' references unknown table '{owner}'"
            )
        key = f"{owner}.{col.name}"
        fact = next((f for f in view.facts if f.table == owner and f.name == col.name), None)
        if fact is not None and key not in _stack:
            sub, used = resolve_expr(view, fact.expr, owner, (*_stack, key))
            tables |= used
            atomic = col is tree or isinstance(sub, (exp.Column, exp.Literal, exp.Paren))
            tree = _replace(tree, col, sub if atomic else exp.Paren(this=sub))
            continue
        tables.add(owner)
        tree = _replace(tree, col, exp.column(col.this.copy(), table=owner))
    return tree, tables


def _dim_expr(view: SemanticView, dim: Dimension, grain: str | None) -> tuple[exp.Expression, set[str]]:
    tree, tables = resolve_expr(view, dim.expr, dim.table)
    if grain:
        cls = exp.DateTrunc if dim.time_type == "date" else exp.TimestampTrunc
        tree = cls(this=tree, unit=exp.var(grain.upper()))
    return tree, tables


def _condition(tree: exp.Expression, flt: Filter) -> exp.Expression:
    op, v = flt.op.lower(), flt.value
    if op not in FILTER_OPS:
        raise CompilerError(
            ERR_INVALID_FILTER, f"filter on '{flt.field}': unknown op '{flt.op}'", hint=f"use one of {FILTER_OPS}"
        )
    if op in _OPS:
        if v is None:
            raise CompilerError(ERR_INVALID_FILTER, f"filter on '{flt.field}' with '{op}' needs a value (use 'is null')")
        return _OPS[op](this=tree, expression=exp.convert(v))
    if op in ("in", "not in"):
        if not isinstance(v, (list, tuple)) or not v:
            raise CompilerError(ERR_INVALID_FILTER, f"filter on '{flt.field}' with '{op}' needs a non-empty list")
        cond: exp.Expression = exp.In(this=tree, expressions=[exp.convert(x) for x in v])
        return exp.Not(this=cond) if op == "not in" else cond
    if op == "between":
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise CompilerError(ERR_INVALID_FILTER, f"filter on '{flt.field}' with 'between' needs [low, high]")
        return exp.Between(this=tree, low=exp.convert(v[0]), high=exp.convert(v[1]))
    if op in ("like", "not like"):
        cond = exp.Like(this=tree, expression=exp.convert(str(v)))
        return exp.Not(this=cond) if op == "not like" else cond
    cond = exp.Is(this=tree, expression=exp.null())
    return exp.Not(this=cond) if op == "is not null" else cond


def _safe_metric_expr(view: SemanticView, metric: Metric) -> exp.Expression:
    """A requested metric in terms of subquery ``q``'s columns; '/' becomes NULL-safe."""
    if not metric.is_derived:
        return exp.column(metric.name, table="q")
    tree = view.parse_expr(metric.expr)
    for col in list(tree.find_all(exp.Column)):
        sub = _safe_metric_expr(view, view.find_metric(col.name))
        tree = _replace(tree, col, sub if isinstance(sub, exp.Column) else exp.Paren(this=sub))
    for div in list(tree.find_all(exp.Div)):
        tree = _replace(
            tree,
            div,
            exp.Div(
                this=exp.Cast(this=div.this.copy(), to=exp.DataType.build("double")),
                expression=exp.Nullif(this=div.expression.copy(), expression=exp.Literal.number(0)),
            ),
        )
    return tree


# --------------------------------------------------------------------------- sources & joins


def _source(view: SemanticView, table: str, partition_preds: list[exp.Expression]) -> exp.Expression:
    """The table as it appears in FROM/JOIN, aliased by its logical name. Partition
    bounds go inside a derived table — the only placement engines like BigQuery
    honor for required partition filters on joined tables."""
    t = view.tables[table]
    if _SELECT_SOURCE.match(t.source):
        base: exp.Expression = sqlglot.parse_one(t.source, read=view.dialect).subquery(table)
    else:
        base = exp.to_table(t.source, dialect=view.dialect)
        base.set("alias", exp.TableAlias(this=exp.to_identifier(table)))
    if not partition_preds:
        return base
    return exp.select(exp.Star()).from_(base).where(exp.and_(*partition_preds)).subquery(table)


def _guard_reach(
    view: SemanticView, graph: Graph, home: str, used: set[str], what: str, metrics: list[Metric]
) -> None:
    bad = sorted(t for t in used if t != home and t not in graph.reach[home])
    if bad:
        names = ", ".join(m.name for m in metrics) or f"table '{home}'"
        raise CompilerError(
            ERR_FANOUT_JOIN_FORBIDDEN,
            f"{what} lives on '{bad[0]}', which is not an ancestor of '{home}' (base table of {names}); "
            "joining downward or across would duplicate rows",
            hint="query that dimension with metrics based on its own table, or drop it",
        )


# --------------------------------------------------------------------------- aggregates


def _aggregate(view, graph, home, metrics, dims, dim_filters, config):
    """One aggregate SELECT at `home`'s grain. dims: [(Dimension, grain, alias)];
    dim_filters: [(Dimension, grain, Filter)]. Returns (select, used tables, join names)."""
    tables: set[str] = {home}
    projections: list[exp.Expression] = []
    group_keys: list[exp.Expression] = []

    for dim, grain, alias in dims:
        tree, used = _dim_expr(view, dim, grain)
        _guard_reach(view, graph, home, used, f"dimension '{dim.name}'", metrics)
        tables |= used
        projections.append(exp.alias_(tree, alias))
        group_keys.append(tree.copy())

    for m in metrics:
        tree, used = resolve_expr(view, m.expr, home)
        _guard_reach(view, graph, home, used, f"metric '{m.name}'", metrics)
        tables |= used
        projections.append(exp.alias_(tree, m.name))

    conditions: list[exp.Expression] = []
    for dim, grain, flt in dim_filters:
        tree, used = _dim_expr(view, dim, grain)
        _guard_reach(view, graph, home, used, f"filter on '{dim.name}'", metrics)
        tables |= used
        conditions.append(_condition(tree, flt))

    # always-on table filters apply to every involved table (and may pull in ancestors),
    # so a metric's population never depends on which dimensions were requested
    queue = sorted(tables)
    seen_filters: set[str] = set()
    while queue:
        t = queue.pop(0)
        if t in seen_filters:
            continue
        seen_filters.add(t)
        tf = view.tables[t].filter
        if tf:
            tree, used = resolve_expr(view, tf, t)
            _guard_reach(view, graph, home, used, f"table filter on '{t}'", metrics)
            new = sorted(used - tables)
            tables |= used
            queue += new
            conditions.append(tree)

    # FinOps: partition requirements for every involved table
    preds_by_table = {t: partition_predicates(view, view.tables[t], dim_filters, config) for t in sorted(tables)}

    sel = exp.select(*projections).from_(_source(view, home, preds_by_table[home]))
    joined: list[str] = []
    seen: set[str] = {home}
    for t in sorted(tables - {home}, key=lambda t: (len(graph.reach[home][t]), t)):
        for rel in graph.reach[home][t]:
            if rel.parent in seen:
                continue
            seen.add(rel.parent)
            on = exp.and_(
                *[
                    exp.EQ(
                        this=exp.column(fk, table=rel.child),
                        expression=exp.column(pk, table=rel.parent),
                    )
                    for fk, pk in zip(rel.child_columns, rel.parent_columns, strict=True)
                ]
            )
            sel = sel.join(_source(view, rel.parent, preds_by_table[rel.parent]), on=on, join_type="left")
            joined.append(rel.name)
    for c in conditions:
        sel = sel.where(c)
    if group_keys:
        sel = sel.group_by(*group_keys)
    return sel, tables, joined


# --------------------------------------------------------------------------- entry point


def build_query(
    view: SemanticView, request: SemanticQueryRequest, dialect: str, config: CompileConfig
) -> CompiledQuery:
    graph = build_graph(view)
    require_metrics(request.metrics)

    metrics: list[Metric] = []
    for ref in request.metrics:
        m = view.find_metric(ref)
        if m not in metrics:
            metrics.append(m)

    dims: list[tuple[Dimension, str | None, str]] = []
    for ref in request.dimensions:
        dim, grain = view.find_dimension(ref)
        alias = f"{dim.name}__{grain}" if grain else dim.name
        if not any(a == alias for _, _, a in dims):
            dims.append((dim, grain, alias))

    dim_filters: list[tuple[Dimension, str | None, Filter]] = []
    metric_filters: list[tuple[Metric, Filter]] = []
    for flt in request.filters:
        if flt.op.lower() not in FILTER_OPS:
            raise CompilerError(
                ERR_INVALID_FILTER, f"filter on '{flt.field}': unknown op '{flt.op}'", hint=f"use one of {FILTER_OPS}"
            )
        try:
            dim, grain = view.find_dimension(flt.field)
            dim_filters.append((dim, grain, flt))
        except CompilerError:
            try:
                metric_filters.append((view.find_metric(flt.field), flt))
            except CompilerError:
                raise CompilerError(
                    ERR_UNKNOWN_FIELD,
                    f"filter field '{flt.field}' is neither a dimension nor a metric of '{view.name}'",
                ) from None

    base: list[Metric] = []
    for m in [*metrics, *(m for m, _ in metric_filters)]:
        for b in view.base_metrics(m):
            if b not in base:
                base.append(b)

    homes: dict[str, list[Metric]] = {}
    for b in base:
        homes.setdefault(b.table, []).append(b)

    # SV-R7: snapshot bindings checked per home table
    for home in homes:
        t = view.tables[home]
        if t.snapshot:
            snapshot_binding(view, t, [(d, g) for d, g, _ in dims], dim_filters)

    selects: list[tuple[exp.Select, list[Metric]]] = []
    used_tables: set[str] = set()
    join_paths: dict[str, list[str]] = {}
    for home, ms in homes.items():
        sel, used, joined = _aggregate(view, graph, home, ms, dims, dim_filters, config)
        selects.append((sel, ms))
        used_tables |= used
        join_paths[home] = joined

    dim_cols = [exp.column(alias) for _, _, alias in dims]
    if len(selects) == 1:
        core: exp.Select = selects[0][0]
    else:
        branches = []
        for i, (sel, ms) in enumerate(selects):
            own = {m.name for m in ms}
            cols = [*dim_cols] + [
                exp.column(b.name) if b.name in own else exp.alias_(exp.null(), b.name) for b in base
            ]
            branches.append(exp.select(*[c.copy() for c in cols]).from_(sel.subquery(f"g{i}")))
        union: exp.Expression = branches[0]
        for br in branches[1:]:
            union = exp.union(union, br, distinct=False)
        core = exp.select(
            *[c.copy() for c in dim_cols],
            *[exp.alias_(exp.Max(this=exp.column(b.name)), b.name) for b in base],
        ).from_(union.subquery("u"))
        if dim_cols:
            core = core.group_by(*[c.copy() for c in dim_cols])

    outer = exp.select(*[exp.column(alias, table="q") for _, _, alias in dims]).from_(core.subquery("q"))
    for m in metrics:
        outer = outer.select(exp.alias_(_safe_metric_expr(view, m), m.name), append=True)
    for m, flt in metric_filters:
        outer = outer.where(_condition(_safe_metric_expr(view, m), flt))

    columns = [alias for _, _, alias in dims] + [m.name for m in metrics]
    for item in request.order_by:
        desc = item.startswith("-")
        name = item.lstrip("+-")
        if name not in columns:
            raise CompilerError(
                ERR_UNKNOWN_FIELD, f"order_by '{name}' must be one of the requested columns: {', '.join(columns)}"
            )
        outer = outer.order_by(exp.Ordered(this=exp.column(name), desc=desc, nulls_first=False))
    outer = outer.limit(resolve_limit(request.limit, config))

    return CompiledQuery(
        sql=outer.sql(dialect=dialect or None, pretty=True),
        dialect=dialect or "",
        columns=columns,
        tables=sorted(used_tables),
        join_paths=dict(sorted(join_paths.items())),
    )


def build_dimension_values_query(
    view: SemanticView,
    dimension: str,
    search: str | None,
    filters: list[Filter],
    limit: int,
    dialect: str,
    config: CompileConfig,
) -> CompiledQuery:
    """SELECT DISTINCT <dim> — the sanctioned, bounded escape hatch from SV-R10.7."""
    graph = build_graph(view)
    dim, grain = view.find_dimension(dimension)
    home = dim.table

    dim_filters: list[tuple[Dimension, str | None, Filter]] = []
    for flt in filters:
        d, g = view.find_dimension(flt.field)
        dim_filters.append((d, g, flt))

    tree, tables = _dim_expr(view, dim, grain)
    _guard_reach(view, graph, home, tables, f"dimension '{dim.name}'", [])
    conditions: list[exp.Expression] = []
    for d, g, flt in dim_filters:
        ftree, used = _dim_expr(view, d, g)
        _guard_reach(view, graph, home, used, f"filter on '{d.name}'", [])
        tables |= used
        conditions.append(_condition(ftree, flt))
    for t in sorted(tables):
        tf = view.tables[t].filter
        if tf:
            ftree, used = resolve_expr(view, tf, t)
            tables |= used
            conditions.append(ftree)
    if search:
        needle = search.lower().replace("%", "").replace("_", " ")
        conditions.append(
            exp.Like(
                this=exp.Lower(this=exp.Cast(this=tree.copy(), to=exp.DataType.build("text"))),
                expression=exp.convert(f"%{needle}%"),
            )
        )

    preds_by_table = {t: partition_predicates(view, view.tables[t], dim_filters, config) for t in sorted(tables)}
    sel = exp.select(exp.alias_(tree, "value")).distinct().from_(_source(view, home, preds_by_table[home]))
    seen = {home}
    for t in sorted(tables - {home}, key=lambda t: (len(graph.reach[home][t]), t)):
        for rel in graph.reach[home][t]:
            if rel.parent in seen:
                continue
            seen.add(rel.parent)
            on = exp.and_(
                *[
                    exp.EQ(this=exp.column(fk, table=rel.child), expression=exp.column(pk, table=rel.parent))
                    for fk, pk in zip(rel.child_columns, rel.parent_columns, strict=True)
                ]
            )
            sel = sel.join(_source(view, rel.parent, preds_by_table[rel.parent]), on=on, join_type="left")
    for c in conditions:
        sel = sel.where(c)
    sel = sel.order_by(exp.Ordered(this=exp.column("value"), desc=False, nulls_first=False))
    sel = sel.limit(max(1, min(limit, 50)))

    return CompiledQuery(
        sql=sel.sql(dialect=dialect or None, pretty=True),
        dialect=dialect or "",
        columns=["value"],
        tables=sorted(tables),
        join_paths={home: list(dict.fromkeys(n for t in sorted(tables) if t != home for n in graph.path_names(home, t)))},
    )
