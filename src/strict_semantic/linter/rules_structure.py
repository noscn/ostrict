"""SV-R1 (view boundary), SV-R2 (logical tables), SV-R3 (relationships)."""

from __future__ import annotations

from ..compiler.resolver import Graph
from ..models.ast import SemanticView
from ..models.errors import (
    ERR_DISCONNECTED_TABLES,
    ERR_MISSING_GRAIN_OR_PK,
    ERR_MISSING_PRIMARY_DATE,
    ERR_ORPHAN_TABLE,
    ERR_PARTIAL_PK_JOIN,
    LintFinding,
    Severity,
)

E, W = Severity.ERROR, Severity.WARN


def _f(rule: str, code: str, severity: Severity, message: str, where: str = "") -> LintFinding:
    return LintFinding(rule=rule, code=code, severity=severity, message=message, where=where)


def r1_connected(view: SemanticView, graph: Graph, cfg) -> list[LintFinding]:
    """SV-R1.1: all tables form one connected component (single-table views exempt)."""
    if len(view.tables) <= 1:
        return []
    adj: dict[str, set[str]] = {t: set() for t in view.tables}
    for r in view.relationships:
        if r.child in adj and r.parent in adj:
            adj[r.child].add(r.parent)
            adj[r.parent].add(r.child)
    seen: set[str] = set()
    todo = [next(iter(view.tables))]
    while todo:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        todo += sorted(adj[t] - seen)
    missing = [t for t in view.tables if t not in seen]
    if missing:
        return [
            _f(
                "SV-R1.1",
                ERR_DISCONNECTED_TABLES,
                E,
                f"tables {missing} are not connected to the rest of the view; "
                "split them into their own semantic view",
            )
        ]
    return []


def r1_view_comment(view, graph, cfg):
    """SV-R1.4: the view itself needs a domain comment."""
    if not view.comment.strip():
        msg = "add a one-paragraph COMMENT describing the view's domain boundary"
        return [_f("SV-R1.4", "WARN_MISSING_VIEW_COMMENT", W, msg)]
    return []


def r2_grain_and_pk(view, graph, cfg):
    """SV-R2.1: every table declares a PRIMARY KEY and a grain comment ('One row per ...')."""
    out = []
    for t in view.tables.values():
        if not t.primary_key and t.name not in cfg.no_pk_tables:
            out.append(_f("SV-R2.1", ERR_MISSING_GRAIN_OR_PK, E, "declare PRIMARY KEY (...)", f"table {t.name}"))
        if not t.comment.lower().startswith("one row per"):
            out.append(
                _f("SV-R2.1", ERR_MISSING_GRAIN_OR_PK, E,
                   "the comment must state the grain, starting with 'One row per ...'", f"table {t.name}")
            )
    return out


def r2_primary_date(view, graph, cfg):
    """SV-R2.3: exactly one primary date per table; snapshot tables must expose it as a time dimension."""
    out = []
    for t in view.tables.values():
        if not t.primary_date:
            out.append(
                _f("SV-R2.3", ERR_MISSING_PRIMARY_DATE, E,
                   "declare the primary date with 'Primary date: <column>.' in the comment", f"table {t.name}")
            )
            continue
        if t.snapshot:
            exposed = any(d.table == t.name and d.expr == t.primary_date and d.is_time for d in view.dimensions)
            if not exposed:
                out.append(
                    _f("SV-R2.3", ERR_MISSING_PRIMARY_DATE, E,
                       f"snapshot table must expose its primary date '{t.primary_date}' as a time dimension "
                       "so point-in-time queries can bind to it", f"table {t.name}")
                )
    return out


def r2_caps(view, graph, cfg):
    """SV-R2.4 / SV-R2.5: context-bloat caps."""
    out = []
    for t in view.tables.values():
        n = sum(1 for d in view.dimensions if d.table == t.name) + sum(
            1 for f in view.facts if f.table == t.name
        )
        if n > cfg.max_fields_per_table:
            out.append(
                _f("SV-R2.4", "WARN_TOO_MANY_FIELDS", W,
                   f"{n} exposed fields (cap {cfg.max_fields_per_table}); trim to keep agents accurate",
                   f"table {t.name}")
            )
    if len(view.tables) > cfg.max_tables:
        out.append(
            _f("SV-R2.5", "WARN_TOO_MANY_TABLES", W,
               f"{len(view.tables)} logical tables (cap {cfg.max_tables}); consider splitting the view")
        )
    return out


def r2_orphans(view, graph, cfg):
    """SV-R2.10: in a multi-table view every table participates in a relationship."""
    if len(view.tables) <= 1:
        return []
    linked = {r.child for r in view.relationships} | {r.parent for r in view.relationships}
    return [
        _f("SV-R2.10", ERR_ORPHAN_TABLE, E, "table participates in no relationship", f"table {t}")
        for t in view.tables
        if t not in linked
    ]


def r3_graph(view, graph, cfg):
    """SV-R3.1 / SV-R3.5: cycles, self-references and ambiguous multi-paths (from the resolver)."""
    return list(graph.findings)


def r3_full_pk(view, graph, cfg):
    """SV-R3.2: the referenced parent columns must equal the parent's entire PRIMARY KEY."""
    out = []
    for r in view.relationships:
        parent = view.tables.get(r.parent)
        if parent is None:
            continue
        if not parent.primary_key or tuple(r.parent_columns) != tuple(parent.primary_key):
            out.append(
                _f("SV-R3.2", ERR_PARTIAL_PK_JOIN, E,
                   f"'{r.name}' references {list(r.parent_columns)} but the full primary key of "
                   f"'{r.parent}' is {list(parent.primary_key)}; partial-key joins fan out",
                   f"relationship {r.name}")
            )
        elif len(r.child_columns) != len(r.parent_columns):
            out.append(
                _f("SV-R3.2", ERR_PARTIAL_PK_JOIN, E,
                   f"'{r.name}' maps {len(r.child_columns)} child column(s) onto "
                   f"{len(r.parent_columns)} parent key column(s)",
                   f"relationship {r.name}")
            )
    return out


RULES = [r1_connected, r1_view_comment, r2_grain_and_pk, r2_primary_date, r2_caps, r2_orphans, r3_graph, r3_full_pk]
