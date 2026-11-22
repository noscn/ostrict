"""SV-R4 (facts), SV-R5 (metrics), SV-R6 (dimensions)."""

from __future__ import annotations

import fnmatch
from collections import Counter

from sqlglot import exp

from ..models.ast import SemanticView
from ..models.errors import (
    ERR_DUPLICATE_FIELD_NAME,
    ERR_INVALID_METRIC,
    ERR_INVALID_RATIO_METRIC,
    ERR_MISSING_NON_ADDITIVE_BY,
    ERR_NON_DETERMINISTIC_TIME,
    ERR_NUMERIC_IN_DIMENSIONS,
    ERR_SECURITY_PLUMBING_EXPOSED,
    LintFinding,
    Severity,
)

E, W = Severity.ERROR, Severity.WARN
_NUMERIC_TYPES = ("INT", "BIGINT", "SMALLINT", "TINYINT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")
_NUMERIC_NAMES = {"quantity", "qty", "amount", "headcount", "units"}
_NUMERIC_SUFFIXES = ("_amount", "_qty", "_quantity", "_count", "_total", "_sum", "_units")
_TIME_FUNCS = {"NOW", "GETDATE", "SYSDATE", "CURRENT_DATE", "CURRENT_TIMESTAMP", "TODAY", "CURDATE"}
_CALENDAR_PARTS = {"day_of_month", "month_number", "quarter_number", "calendar_year"}


def _f(rule, code, severity, message, where=""):
    return LintFinding(rule=rule, code=code, severity=severity, message=message, where=where)


def _nondeterministic_time(tree: exp.Expression) -> bool:
    if tree.find(exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime):
        return True
    return any(
        isinstance(f, exp.Anonymous) and f.name.upper() in _TIME_FUNCS for f in tree.find_all(exp.Anonymous)
    )


def _is_numeric_expr(e: exp.Expression) -> bool:
    while isinstance(e, exp.Paren):
        e = e.this
    if isinstance(e, (exp.Mul, exp.Div, exp.Add, exp.Sub)):
        return True
    if isinstance(e, exp.Literal) and not e.is_string:
        return True
    if isinstance(e, exp.Cast):
        t = e.to.sql().upper()
        return any(t.startswith(x) for x in _NUMERIC_TYPES)
    return False


def r4_numeric_in_dimensions(view: SemanticView, graph, cfg):
    """SV-R4.1: numeric row values belong in FACTS, never DIMENSIONS."""
    out = []
    for d in view.dimensions:
        tree = view.parse_expr(d.expr)
        if _is_numeric_expr(tree):
            out.append(
                _f("SV-R4.1", ERR_NUMERIC_IN_DIMENSIONS, E,
                   f"'{d.expr}' is a numeric row value; move it to FACTS and aggregate it in a metric",
                   f"dimension {d.name}")
            )
        elif d.name.lower() in _NUMERIC_NAMES or d.name.lower().endswith(_NUMERIC_SUFFIXES):
            out.append(
                _f("SV-R4.1", "WARN_NUMERIC_NAME_IN_DIMENSIONS", W,
                   "the name looks like a numeric measure; if it is, move it to FACTS",
                   f"dimension {d.name}")
            )
    return out


def r4_row_level_only(view, graph, cfg):
    """SV-R4: facts and dimensions are row-level — no aggregation, no windows."""
    out = []
    for kind, items in (("fact", view.facts), ("dimension", view.dimensions)):
        for f in items:
            if view.parse_expr(f.expr).find(exp.AggFunc, exp.Window):
                out.append(
                    _f("SV-R4.1", ERR_INVALID_METRIC, E,
                       f"{kind}s are row-level; aggregation belongs in METRICS", f"{kind} {f.name}")
                )
    return out


def r4_dry_shared_expressions(view, graph, cfg):
    """SV-R4.2: the same aggregate argument in 2+ metrics should be a shared FACT."""
    args = Counter()
    for m in view.metrics:
        if m.is_derived:
            continue
        for agg in view.parse_expr(m.expr).find_all(exp.AggFunc):
            arg = agg.this
            if arg is not None and not isinstance(arg, (exp.Column, exp.Star)):
                args[arg.sql()] += 1
    return [
        _f("SV-R4.2", "WARN_REPEATED_EXPRESSION", W,
           f"expression '{sql}' is aggregated in {n} metrics; extract it into a FACT")
        for sql, n in sorted(args.items())
        if n >= 2
    ]


def r5_metric_shape(view, graph, cfg):
    """SV-R5: a base metric is one aggregation over row-level inputs; windows are forbidden."""
    out = []
    metric_names = {m.name for m in view.metrics}
    for m in view.metrics:
        tree = view.parse_expr(m.expr)
        where = f"metric {m.name}"
        if tree.find(exp.Window):
            out.append(_f("SV-R5.1", ERR_INVALID_METRIC, E, "window functions are not supported in metrics", where))
        if m.is_derived:
            continue
        if not tree.find(exp.AggFunc):
            out.append(_f("SV-R5.1", ERR_INVALID_METRIC, E, "a metric must aggregate, e.g. SUM(...) or COUNT(...)", where))
            continue
        loose = [c.sql() for c in tree.find_all(exp.Column) if not c.find_ancestor(exp.AggFunc)]
        if loose:
            out.append(
                _f("SV-R5.1", ERR_INVALID_METRIC, E,
                   f"{', '.join(loose)} sits outside any aggregate; wrap it or make a derived metric", where)
            )
        nested = [c.name for c in tree.find_all(exp.Column) if c.name in metric_names]
        if nested:
            out.append(
                _f("SV-R5.8", ERR_INVALID_RATIO_METRIC, E,
                   f"aggregating other metrics ({', '.join(sorted(set(nested)))}) is forbidden; "
                   "combine named metrics in a derived metric instead", where)
            )
        case = tree.find(exp.Case)
        if case and len(list(case.find_all(exp.If))) > cfg.max_case_conditions:
            out.append(
                _f("SV-R5.1", "WARN_COMPLEX_CASE_IN_METRIC", W,
                   "complex CASE population logic belongs upstream (e.g. a dbt boolean flag)", where)
            )
    return out


def r5_repeated_inline_filters(view, graph, cfg):
    """SV-R5.2: a literal predicate repeated across many metrics should be an upstream flag."""
    preds = Counter()
    for m in view.metrics:
        seen = set()
        for p in view.parse_expr(m.expr).find_all(exp.EQ, exp.NEQ, exp.In, exp.Like, exp.GT, exp.LT, exp.GTE, exp.LTE):
            seen.add(p.sql())
        preds.update(seen)
    return [
        _f("SV-R5.2", "WARN_REPEATED_FILTER", W,
           f"predicate {sql} appears in {n} metrics; materialize an upstream boolean flag")
        for sql, n in sorted(preds.items())
        if n >= cfg.max_repeated_predicates
    ]


def r5_no_current_time(view, graph, cfg):
    """SV-R5.3: CURRENT_DATE / NOW / GETDATE make results non-deterministic."""
    out = []
    for kind, items in (("fact", view.facts), ("dimension", view.dimensions), ("metric", view.metrics)):
        for f in items:
            if _nondeterministic_time(view.parse_expr(f.expr)):
                out.append(
                    _f("SV-R5.3", ERR_NON_DETERMINISTIC_TIME, E,
                       "evaluate time windows against explicit query filters or precomputed as-of columns",
                       f"{kind} {f.name}")
                )
    return out


def r5_snapshot_non_additive(view, graph, cfg):
    """SV-R5.7: metrics on snapshot tables must declare NON ADDITIVE BY (<primary date>)."""
    out = []
    for m in view.metrics:
        if m.is_derived:
            continue
        t = view.tables.get(m.table)
        if t is None or not t.snapshot:
            continue
        if m.non_additive_by != t.primary_date:
            out.append(
                _f("SV-R5.7", ERR_MISSING_NON_ADDITIVE_BY, E,
                   f"snapshot metrics must declare NON ADDITIVE BY ({t.primary_date}) so they are "
                   "never summed across snapshot dates", f"metric {m.name}")
            )
    return out


def r5_ratio_composition(view, graph, cfg):
    """SV-R5.8: derived metrics combine *named* metrics arithmetically — no aggregation inside."""
    out = []
    names = {m.name for m in view.metrics} | {s for m in view.metrics for s in m.synonyms}
    for m in view.metrics:
        if not m.is_derived:
            continue
        where = f"metric {m.name}"
        tree = view.parse_expr(m.expr)
        if tree.find(exp.AggFunc):
            out.append(
                _f("SV-R5.8", ERR_INVALID_RATIO_METRIC, E,
                   "derived metrics must combine named base metrics (metric_a / metric_b), "
                   "never aggregate again (no SUM(a / b))", where)
            )
            continue
        bad = sorted({c.name for c in tree.find_all(exp.Column) if c.name not in names})
        if bad:
            out.append(
                _f("SV-R5.8", ERR_INVALID_RATIO_METRIC, E,
                   f"derived metrics may only reference other metrics; unknown: {', '.join(bad)}", where)
            )
    return out


def r6_security_plumbing(view, graph, cfg):
    """SV-R6.2: access-control plumbing never ships as a dimension."""
    out = []
    for d in (*view.dimensions, *view.facts):
        names = {d.name.lower()} | {c.name.lower() for c in view.parse_expr(d.expr).find_all(exp.Column)}
        for n in sorted(names):
            if n in (x.lower() for x in cfg.security_allowlist):
                continue
            if any(fnmatch.fnmatch(n, pat) for pat in cfg.security_deny):
                out.append(
                    _f("SV-R6.2", ERR_SECURITY_PLUMBING_EXPOSED, E,
                       f"'{n}' matches the security deny-list; enforce access in the warehouse or a "
                       "before_compile plugin, or allow-list it explicitly", f"dimension {d.name}")
                )
                break
    return out


def r6_unique_names(view, graph, cfg):
    """SV-R6.4: every exposed dimension, fact and metric name is unique view-wide."""
    counts = Counter(f.name for f in (*view.dimensions, *view.facts, *view.metrics))
    return [
        _f("SV-R6.4", ERR_DUPLICATE_FIELD_NAME, E,
           f"'{name}' is defined {n} times; outputs return bare names, so they must be unique view-wide")
        for name, n in sorted(counts.items())
        if n > 1
    ]


def r6_calendar_parts(view, graph, cfg):
    """SV-R6.6: don't expose calendar parts next to a real date dimension — grains cover it."""
    if not any(d.is_time for d in view.dimensions):
        return []
    return [
        _f("SV-R6.6", "WARN_REDUNDANT_CALENDAR_PART", W,
           "engines derive calendar parts from the date dimension's grains", f"dimension {d.name}")
        for d in view.dimensions
        if d.name.lower() in _CALENDAR_PARTS
    ]


def r6_values_enumerated(view, graph, cfg):
    """SV-R6.7: low-cardinality categoricals should declare 'Values: ...' for the agent."""
    return [
        _f("SV-R6.7", "WARN_MISSING_VALUES", W,
           "declare 'Values: ...' in the comment so agents can resolve exact filter values",
           f"dimension {d.name}")
        for d in view.dimensions
        if not d.is_time and not d.values
    ]


RULES = [
    r4_numeric_in_dimensions,
    r4_row_level_only,
    r4_dry_shared_expressions,
    r5_metric_shape,
    r5_repeated_inline_filters,
    r5_no_current_time,
    r5_snapshot_non_additive,
    r5_ratio_composition,
    r6_security_plumbing,
    r6_unique_names,
    r6_calendar_parts,
    r6_values_enumerated,
]
