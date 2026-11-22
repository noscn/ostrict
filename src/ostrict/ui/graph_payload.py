"""Transform a SemanticView plus its lint report into the studio's JSON payload.

This never raises on a lint-broken model — debugging broken specs is the whole
point — so ``lint()`` runs (it only *reports*) and every finding is attached to
the node, edge or view level it concerns, parsed from ``finding.where``:

- ``table <name>``          -> that node (SV-R3.5 multi-path findings land here
                               too, on the child table, where the resolver
                               reports them);
- ``relationship <name>``   -> that edge, which turns ``status: "error"`` for
                               ERROR findings (SV-R3.1 self-reference,
                               SV-R3.2 partial-key joins);
- ``metric|dimension|fact <name>`` -> the owning table's node (derived metrics
                               and unknown names fall through to view level);
- anything else             -> the view-level findings tray.

Node roles: ``snapshot`` wins (point-in-time semantics dominate), then
``fact`` (owns a base metric, or is a pure child in the join graph), else
``dimension``. Tables sharing one physical source are additionally badged
``role_playing`` with their siblings listed.
"""

from __future__ import annotations

from ..linter import LintConfig, lint
from ..models.ast import SemanticView
from ..models.errors import Severity


def _finding(f) -> dict:
    return {"rule": f.rule, "code": f.code, "severity": f.severity.value, "message": f.message, "where": f.where}


def build_ui_payload(view: SemanticView, lint_config: LintConfig | None = None) -> dict:
    report = lint(view, lint_config)

    shared: dict[str, list[str]] = {}
    for t in view.tables.values():
        shared.setdefault(t.source.strip().lower(), []).append(t.name)
    incoming = dict.fromkeys(view.tables, 0)
    outgoing = dict.fromkeys(view.tables, 0)
    for r in view.relationships:
        if r.child in outgoing:
            outgoing[r.child] += 1
        if r.parent in incoming:
            incoming[r.parent] += 1

    nodes: dict[str, dict] = {}
    for t in view.tables.values():
        owns_metric = any(m.table == t.name for m in view.metrics)
        if t.snapshot:
            role = "snapshot"
        elif owns_metric or (outgoing[t.name] and not incoming[t.name]):
            role = "fact"
        else:
            role = "dimension"
        siblings = [x for x in shared[t.source.strip().lower()] if x != t.name]
        nodes[t.name] = {
            "name": t.name,
            "role": role,
            "role_playing": bool(siblings),
            "shared_source_with": siblings,
            "source": t.source,
            "primary_key": list(t.primary_key),
            "primary_date": t.primary_date,
            "snapshot": t.snapshot,
            "partition": (
                {
                    "column": t.partition.column,
                    "dimension": t.partition.dimension,
                    "lookback_days": t.partition.lookback_days,
                    "lookahead_days": t.partition.lookahead_days,
                }
                if t.partition
                else None
            ),
            "filter": t.filter,
            "comment": t.comment,
            "facts": [{"name": f.name, "expr": f.expr, "comment": f.comment} for f in view.facts if f.table == t.name],
            "dimensions": [
                {
                    "name": d.name,
                    "expr": d.expr,
                    "comment": d.comment,
                    "grains": list(d.grains),
                    "time_type": d.time_type,
                    "is_time": d.is_time,
                    "values": list(d.values),
                    "synonyms": list(d.synonyms),
                }
                for d in view.dimensions
                if d.table == t.name
            ],
            "metrics": [
                {
                    "name": m.name,
                    "expr": m.expr,
                    "comment": m.comment,
                    "non_additive_by": m.non_additive_by,
                    "derived": False,
                }
                for m in view.metrics
                if m.table == t.name
            ],
            "findings": [],
        }

    edges = [
        {
            "name": r.name,
            "child": r.child,
            "parent": r.parent,
            "child_columns": list(r.child_columns),
            "parent_columns": list(r.parent_columns),
            "status": "ok",
            "findings": [],
        }
        for r in view.relationships
    ]
    edge_index: dict[str, list[dict]] = {}
    for e in edges:
        edge_index.setdefault(e["name"], []).append(e)

    field_owner: dict[str, str] = {}
    for f in (*view.facts, *view.dimensions):
        field_owner.setdefault(f.name, f.table)
    for m in view.metrics:
        if m.table:
            field_owner.setdefault(m.name, m.table)

    view_findings: list[dict] = []
    for f in report.findings:
        kind, _, name = (f.where or "").partition(" ")
        fd = _finding(f)
        if kind == "table" and name in nodes:
            nodes[name]["findings"].append(fd)
        elif kind == "relationship" and name in edge_index:
            for e in edge_index[name]:
                e["findings"].append(fd)
                if f.severity == Severity.ERROR:
                    e["status"] = "error"
        elif kind in ("metric", "dimension", "fact") and field_owner.get(name) in nodes:
            nodes[field_owner[name]]["findings"].append(fd)
        else:
            view_findings.append(fd)

    sev = {s: sum(1 for f in report.findings if f.severity == s) for s in Severity}
    return {
        "name": view.name,
        "dialect": view.dialect,
        "comment": view.comment,
        "ai_sql_generation": view.ai_sql_generation,
        "nodes": list(nodes.values()),
        "edges": edges,
        "derived_metrics": [
            {"name": m.name, "expr": m.expr, "comment": m.comment, "derived": True}
            for m in view.metrics
            if m.is_derived
        ],
        "findings": view_findings,
        "summary": {
            "errors": sev[Severity.ERROR],
            "warnings": sev[Severity.WARN],
            "policies": sev[Severity.POLICY],
        },
        "ok": report.ok,
    }
