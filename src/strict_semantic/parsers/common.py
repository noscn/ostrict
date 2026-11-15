"""Shared builder: both parsers emit the same *raw* dict shape, and this module
turns it into the canonical AST — so DDL and YAML inputs are identical by
construction wherever they express the same thing.

Metadata that Snowflake DDL has no slot for travels in COMMENT directives
(SV-R8.1's structured templates). The native YAML may set explicit keys instead;
when both are present they must agree.

Comment directives (each ends with a period):
    One row per <grain>.                          -- SV-R2.1, start of table comment
    Primary date: <column>.                       -- SV-R2.3
    Point-in-time snapshot.                       -- marks a snapshot table (SV-R5.7/R7)
    Partition: <column> BY <dimension>.           -- FinOps partition pruning
    Partition: <column> BY <dimension> (lookback 35d, lookahead 1d).
    Grains: day, month.                           -- marks a time dimension
    Type: timestamp.                              -- time dimension storage type
    Values: a, b, c.                              -- declared enumeration (SV-R6.7)
"""

from __future__ import annotations

import re

import sqlglot

from ..models.ast import (
    GRAINS,
    Dimension,
    Fact,
    LogicalTable,
    Metric,
    Partition,
    Relationship,
    SemanticView,
    canonical,
)
from ..models.errors import ERR_PARSE, ERR_UNKNOWN_TABLE, CompilerError

_PRIMARY_DATE = re.compile(r"Primary date:\s*([A-Za-z_]\w*)\s*\.")
_SNAPSHOT = re.compile(r"Point-in-time snapshot\.")
_PARTITION = re.compile(
    r"Partition:\s*(\w+)\s+BY\s+(\w+)\s*"
    r"(?:\(\s*lookback\s+(\d+)d\s*(?:,\s*lookahead\s+(\d+)d\s*)?\))?\s*\."
)
_GRAINS = re.compile(r"Grains:\s*([a-z, ]+?)\s*\.")
_TYPE = re.compile(r"Type:\s*(date|timestamp)\s*\.")
_VALUES = re.compile(r"Values:\s*([^.]+?)\s*\.")


def _fail(origin: str, where: str, msg: str) -> CompilerError:
    return CompilerError(ERR_PARSE, f"{origin}: {where}: {msg}")


def _merge(explicit, directive, origin, where, what):
    """Combine an explicit YAML key with a comment directive; both present must agree."""
    if explicit is not None and directive is not None and explicit != directive:
        raise _fail(origin, where, f"{what} is '{explicit}' in the spec but '{directive}' in the comment")
    return explicit if explicit is not None else directive


def build_view(raw: dict, origin: str = "spec") -> SemanticView:
    if not isinstance(raw, dict) or not raw.get("name"):
        raise _fail(origin, "view", "a semantic view needs at least 'name' and 'tables'")
    dialect = raw.get("dialect") or "snowflake"

    def canon(text, where):
        try:
            return canonical(str(text), dialect)
        except sqlglot.errors.ParseError as e:
            raise _fail(origin, where, f"cannot parse SQL '{text}': {e}") from None

    tables: dict[str, LogicalTable] = {}
    for name, t in (raw.get("tables") or {}).items():
        comment = str(t.get("comment") or "")
        m = _PRIMARY_DATE.search(comment)
        primary_date = _merge(t.get("primary_date"), m.group(1) if m else None, origin, f"table {name}", "primary date")
        p = _PARTITION.search(comment)
        from_comment = (
            Partition(
                column=p.group(1),
                dimension=p.group(2),
                lookback_days=int(p.group(3) or 0),
                lookahead_days=int(p.group(4) or 0),
            )
            if p
            else None
        )
        explicit = Partition(**t["partition"]) if t.get("partition") else None
        partition = _merge(explicit, from_comment, origin, f"table {name}", "partition")
        key = t.get("primary_key") or ()
        tables[name] = LogicalTable(
            name=name,
            source=str(t.get("source") or "").strip(),
            primary_key=(key,) if isinstance(key, str) else tuple(key),
            comment=comment,
            primary_date=primary_date,
            snapshot=bool(t.get("snapshot")) or bool(_SNAPSHOT.search(comment)),
            partition=partition,
            filter=canon(t["filter"], f"table {name} filter") if t.get("filter") else None,
        )
        if not tables[name].source:
            raise _fail(origin, f"table {name}", "needs a 'source'")
    if not tables:
        raise _fail(origin, "view", "a semantic view needs at least one table")

    relationships = []
    for r in raw.get("relationships") or []:
        child, parent = r.get("child"), r.get("parent")
        for side in (child, parent):
            if side not in tables:
                raise CompilerError(
                    ERR_UNKNOWN_TABLE, f"{origin}: relationship references unknown table '{side}'"
                )
        parent_columns = tuple(r.get("parent_columns") or tables[parent].primary_key)
        relationships.append(
            Relationship(
                name=r.get("name") or f"{child}_to_{parent}",
                child=child,
                child_columns=tuple(r["columns"]),
                parent=parent,
                parent_columns=parent_columns,
            )
        )

    def field_common(item, section):
        table = item.get("table")
        if table is not None and table not in tables:
            raise CompilerError(
                ERR_UNKNOWN_TABLE, f"{origin}: {section} '{item['name']}' names unknown table '{table}'"
            )
        where = f"{section} {table + '.' if table else ''}{item['name']}"
        return table, item["name"], canon(item["expr"], where), str(item.get("comment") or "")

    facts, dimensions, metrics = [], [], []
    for item in raw.get("facts") or []:
        table, name, expr, comment = field_common(item, "fact")
        if table is None:
            raise _fail(origin, f"fact {name}", "facts must be written as <table>.<name>")
        facts.append(Fact(table=table, name=name, expr=expr, comment=comment))

    for item in raw.get("dimensions") or []:
        table, name, expr, comment = field_common(item, "dimension")
        if table is None:
            raise _fail(origin, f"dimension {name}", "dimensions must be written as <table>.<name>")
        g = _GRAINS.search(comment)
        grains = _merge(
            tuple(item["grains"]) if item.get("grains") else None,
            tuple(x.strip() for x in g.group(1).split(",") if x.strip()) if g else None,
            origin,
            f"dimension {name}",
            "grains",
        ) or ()
        bad = set(grains) - set(GRAINS)
        if bad:
            raise _fail(origin, f"dimension {name}", f"unknown grains {sorted(bad)}; use {GRAINS}")
        ty = _TYPE.search(comment)
        time_type = _merge(item.get("type"), ty.group(1) if ty else None, origin, f"dimension {name}", "type")
        v = _VALUES.search(comment)
        values = tuple(x.strip() for x in v.group(1).split(",")) if v else ()
        dimensions.append(
            Dimension(
                table=table,
                name=name,
                expr=expr,
                comment=comment,
                synonyms=tuple(item.get("synonyms") or ()),
                grains=tuple(grains),
                time_type=time_type or "date",
                values=values,
            )
        )

    for item in raw.get("metrics") or []:
        table, name, expr, comment = field_common(item, "metric")
        metrics.append(
            Metric(
                table=table,
                name=name,
                expr=expr,
                comment=comment,
                synonyms=tuple(item.get("synonyms") or ()),
                non_additive_by=item.get("non_additive_by"),
            )
        )

    return SemanticView(
        name=raw["name"],
        dialect=dialect,
        comment=str(raw.get("comment") or ""),
        ai_sql_generation=str(raw.get("ai_sql_generation") or ""),
        tables=tables,
        relationships=tuple(relationships),
        facts=tuple(facts),
        dimensions=tuple(dimensions),
        metrics=tuple(metrics),
    )
