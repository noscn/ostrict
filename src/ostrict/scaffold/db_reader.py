"""Read a semantic-view draft straight from a warehouse schema.

``read_schema`` pulls column names and types from ``INFORMATION_SCHEMA.COLUMNS``
through a :class:`WarehouseConnector` (DuckDB or BigQuery) — one metadata query
per schema, zero data scanned. Primary keys are guessed by name convention
(``<singular>_id`` or ``id``, else the first column, with a note) and
child -> parent relationships are inferred where a ``*_id`` column matches
exactly one other selected table's confident primary key.

``sample_values`` is the only thing here that touches data, and only when the
user asks (``--sample-values``): a bounded
``SELECT DISTINCT <col> FROM <table> WHERE <col> IS NOT NULL LIMIT 21``.
"""

from __future__ import annotations

import re
from collections import Counter

import sqlglot
from sqlglot import exp

from ..connectors import WarehouseConnector
from ..models.errors import ERR_PARSE, CompilerError
from .generator import ColumnDraft, RelDraft, TableDraft, guess_primary_key

SAMPLE_LIMIT = 21  # > the 15-value Values: cap, so "too many" is detectable


def read_schema(connector: WarehouseConnector, models: list[str]) -> list[TableDraft]:
    if not models:
        raise CompilerError(ERR_PARSE, "schema mode needs --models with (schema-qualified) table names")
    by_schema: dict[str, list[str]] = {}
    for m in models:
        schema, _, table = m.rpartition(".")
        by_schema.setdefault(schema, []).append(table)

    # drafts are keyed by full (schema, table) identity: the same table name in two
    # schemas is two physical tables, never one draft with merged columns
    drafts: dict[tuple[str, str], TableDraft] = {}
    for schema, tabs in sorted(by_schema.items()):
        for row in _columns(connector, schema, tabs):
            tname = str(row["table_name"])
            key = (schema, tname)
            if key not in drafts:
                drafts[key] = TableDraft(name=tname, source=f"{schema}.{tname}" if schema else tname, columns=[])
            drafts[key].columns.append(ColumnDraft(name=str(row["column_name"]), data_type=str(row["data_type"])))
    missing = sorted(m for m in models if (m.rpartition(".")[0], m.rpartition(".")[2]) not in drafts)
    if missing:
        raise CompilerError(ERR_PARSE, f"not found in information_schema: {', '.join(missing)}")

    # logical names must stay unique view-wide: qualify duplicates with their schema
    counts = Counter(t for _, t in drafts)
    for (schema, tname), d in drafts.items():
        if counts[tname] > 1 and schema:
            d.name = re.sub(r"[^A-Za-z0-9_]", "_", f"{schema}_{tname}")
            d.notes.append(f"renamed from '{tname}': the same table name exists in another selected schema")

    confident: dict[str, TableDraft] = {}
    for d in drafts.values():
        d.primary_key, note = guess_primary_key(d.name, [c.name for c in d.columns])
        if note:
            d.notes.append(note)
        else:
            confident[d.name] = d
    for d in drafts.values():
        for c in d.columns:
            if not c.name.endswith("_id"):
                continue
            parents = [p for p in confident.values() if p is not d and p.primary_key == [c.name]]
            if len(parents) == 1:
                d.relationships.append(RelDraft(column=c.name, parent=parents[0].name, parent_column=c.name))
                d.notes.append(f"relationship to '{parents[0].name}' inferred from column name '{c.name}'")
    return list(drafts.values())


def _columns(connector: WarehouseConnector, schema: str, tables: list[str]) -> list[dict]:
    names = [exp.Literal.string(t) for t in sorted(set(tables))]
    cond: exp.Expression = exp.column("table_name").isin(*names)
    if connector.dialect == "bigquery":
        source = sqlglot.to_table(f"{schema}.INFORMATION_SCHEMA.COLUMNS", dialect="bigquery")
    else:
        source = sqlglot.to_table("information_schema.columns")
        cond = exp.and_(cond, exp.column("table_schema").eq(exp.Literal.string(schema or "main")))
    q = (
        exp.select("table_name", "column_name", "data_type", "ordinal_position")
        .from_(source)
        .where(cond)
        .order_by("table_name", "ordinal_position")
    )
    return connector.execute(q.sql(dialect=connector.dialect))


def sample_values(connector: WarehouseConnector, source: str, column: str, limit: int = SAMPLE_LIMIT) -> list:
    q = (
        exp.select(exp.column(column))
        .distinct()
        .from_(sqlglot.to_table(source, dialect=connector.dialect))
        .where(exp.column(column).is_(exp.Null()).not_())
        .limit(limit)
    )
    rows = connector.execute(q.sql(dialect=connector.dialect))
    return sorted((r[column] for r in rows), key=str)  # deterministic regardless of scan order
