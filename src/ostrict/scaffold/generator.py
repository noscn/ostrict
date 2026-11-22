"""Turn raw table metadata into a draft semantic view that passes SV-R1..R10.

The generator never invents semantics — it classifies what the metadata says
(numeric -> FACT + a sample SUM metric, date/timestamp -> time dimension,
text/bool -> dimension) and applies the linter's ground rules defensively:

- columns matching the security deny-list are dropped (SV-R6.2);
- key columns (primary keys, relationship columns, dangling ``*_id``) are
  never exposed as dimensions;
- names that collide across tables get a ``<table>_`` prefix (SV-R6.4);
- relationships that would break the join forest — self-references (SV-R3.1)
  or a second path between two tables (SV-R3.5) — are moved into a
  ``# TODO`` comment instead of the model;
- every table gets a grain sentence, a primary date and a Source (SV-R2.1,
  SV-R2.3, SV-R8.1), and partitioned tables get a conservative
  ``Partition: ... (lookback 1d, lookahead 1d).`` hint.

The output is a DRAFT for a human to review (the TODO header says what to
check), assembled through the same ``build_view`` -> ``lint`` ->
``to_native_yaml`` pipeline the rest of the project uses, so what comes out
is exactly what ``ostrict lint`` will see.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

from ..interchange import to_native_yaml
from ..linter import LintReport, lint
from ..linter.engine import DEFAULT_SECURITY_DENY
from ..models.ast import SemanticView
from ..parsers.common import build_view

_NUMERIC = ("INT", "BIGINT", "SMALLINT", "TINYINT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")
_STRINGY = ("VARCHAR", "TEXT", "STRING", "CHAR")
_SAMPLE_CAP = 15  # SV-R6.7: declare Values: only for genuinely low-cardinality columns


@dataclass
class ColumnDraft:
    name: str
    data_type: str | None = None  # warehouse/dbt type; None = unknown
    description: str = ""


@dataclass
class RelDraft:
    """A candidate child -> parent relationship (child side owns the draft)."""

    column: str
    parent: str
    parent_column: str


@dataclass
class TableDraft:
    name: str
    source: str
    columns: list[ColumnDraft]
    description: str = ""
    primary_key: list[str] = field(default_factory=list)
    partition_column: str | None = None
    relationships: list[RelDraft] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # reader-side caveats (heuristic PK, ...)


class ScaffoldResult(NamedTuple):
    text: str  # YAML draft, TODO header included
    notes: tuple[str, ...]  # everything a human should review (mirrored in the header)
    report: LintReport  # lint of the draft at scaffold time
    view: SemanticView


def _is_numeric(t: str | None) -> bool:
    return t is not None and t.upper().startswith(_NUMERIC)


def _is_stringy(t: str | None) -> bool:
    return t is not None and t.upper().startswith(_STRINGY)


def _time_kind(t: str | None) -> str | None:
    u = (t or "").upper()
    if u.startswith(("TIMESTAMP", "DATETIME")):
        return "timestamp"
    if u.startswith("DATE"):
        return "date"
    return None


def _singular(table: str) -> str:
    if len(table) > 3 and table.endswith("s") and not table.endswith("ss"):
        return table[:-1]
    return table


def _denied(name: str) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatch(n, pat) for pat in DEFAULT_SECURITY_DENY)


def guess_primary_key(table: str, columns: list[str]) -> tuple[list[str], str | None]:
    """Name-convention PK guess for sources without key metadata; a note means low confidence."""
    for cand in (f"{_singular(table)}_id", "id"):
        if cand in columns:
            return [cand], None
    if columns:
        return [columns[0]], f"no key metadata; guessed primary key '{columns[0]}' — fix it before trusting joins"
    return [], "no columns found to guess a primary key from"


def _sentence(text: str, fallback: str) -> str:
    """One trimmed sentence ending in a period; the fallback is used for empty metadata."""
    body = " ".join((text or "").split()).strip()
    if not body:
        body = fallback
    if len(body) > 200:
        body = body[:197].rstrip() + "..."
    return body if body.endswith(".") else body + "."


def directive_safe(values: list) -> list[str] | None:
    """The ``Values: a, b.`` grammar splits on commas and ends at a period."""
    if not values or len(values) > _SAMPLE_CAP:
        return None
    out = []
    for v in values:
        if not isinstance(v, str) or not v or "," in v or "." in v or v != v.strip():
            return None
        out.append(v)
    return sorted(out)


class _Forest:
    """Union-find over tables: the join graph must have one path between any two tables."""

    def __init__(self, tables):
        self._up = {t: t for t in tables}

    def find(self, t: str) -> str:
        while self._up[t] != t:
            self._up[t] = self._up[self._up[t]]
            t = self._up[t]
        return t

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self._up[ra] = rb
        return True


def generate(
    tables: list[TableDraft],
    *,
    name: str,
    dialect: str = "bigquery",
    sampler: Callable[[str, str], list[str] | None] | None = None,
    origin: str = "scaffold",
) -> ScaffoldResult:
    tables = sorted(tables, key=lambda t: t.name)
    notes: list[str] = []
    todo_rel_snippets: list[str] = []
    for t in tables:
        notes += [f"table {t.name}: {n}" for n in t.notes]

    # ---- pass 1: classify columns ------------------------------------------
    key_cols = {t.name: set(t.primary_key) for t in tables}
    for t in tables:
        for r in t.relationships:  # every relationship column is plumbing, kept or not
            key_cols[t.name].add(r.column)

    candidates: list[dict] = []  # {kind: dimension|fact, table, column, ...}
    for t in tables:
        for c in t.columns:
            if _denied(c.name):
                notes.append(
                    f"column {t.name}.{c.name} dropped: matches the security deny-list (SV-R6.2); "
                    "allow-list it in lint config if it is genuinely safe"
                )
                continue
            if c.name in key_cols[t.name]:
                continue  # keys are join plumbing, never dimensions (SV-R6.2/R6.4 hygiene)
            kind = _time_kind(c.data_type)
            if kind:
                candidates.append({"kind": "dimension", "table": t, "column": c, "time": kind})
            elif _is_numeric(c.data_type):
                if c.name == "id" or c.name.endswith("_id"):
                    notes.append(f"column {t.name}.{c.name} skipped: looks like a key but joins nothing")
                    continue
                candidates.append({"kind": "fact", "table": t, "column": c})
            else:
                if c.data_type is None:
                    notes.append(f"column {t.name}.{c.name}: unknown type; drafted as a dimension")
                candidates.append({"kind": "dimension", "table": t, "column": c, "time": None})

    # ---- pass 2: view-wide unique names (SV-R6.4) --------------------------
    def dedupe(items: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        counts = Counter(base for _, base in items)
        out = {}
        for table, base in items:
            final = f"{table}_{base}" if counts[base] > 1 else base
            out[(table, base)] = final
        again = Counter(out.values())
        for key, final in out.items():
            if again[final] > 1:  # pathological double collision: stay deterministic
                out[key] = f"{key[0]}_{final}"
        return out

    exposed = [(c["table"].name, c["column"].name) for c in candidates]
    exposed += [(c["table"].name, f"sum_{c['column'].name}") for c in candidates if c["kind"] == "fact"]
    names = dedupe(exposed)
    renamed = sorted(f"'{b}' -> '{n}'" for (_, b), n in names.items() if n != b and not b.startswith("sum_"))
    if renamed:
        notes.append(f"names duplicated across tables were prefixed (SV-R6.4): {', '.join(renamed)}")

    # ---- pass 3: relationships must form a forest (SV-R3.1 / SV-R3.5) ------
    by_name = {t.name: t for t in tables}
    forest = _Forest([t.name for t in tables])
    raw_rels = []
    for t in tables:
        for r in t.relationships:
            rel_name = f"{t.name}_to_{r.parent}"
            snippet = f"- name: {rel_name}\n  from: {t.name} ({r.column})\n  to: {r.parent} ({r.parent_column})"
            parent = by_name.get(r.parent)
            if parent is None:
                notes.append(f"relationship {rel_name} skipped: parent '{r.parent}' is not in the selection")
                continue
            if r.parent == t.name:
                notes.append(f"relationship {rel_name} skipped: self-reference (SV-R3.1); model a role-playing alias")
                continue
            if parent.primary_key != [r.parent_column]:
                notes.append(
                    f"relationship {rel_name} skipped: '{r.parent_column}' is not the full primary key "
                    f"of '{r.parent}' ({parent.primary_key}) — partial-key joins fan out (SV-R3.2)"
                )
                continue
            if not forest.union(t.name, r.parent):
                notes.append(
                    f"relationship {rel_name} skipped: second join path between '{t.name}' and "
                    f"'{r.parent}' (SV-R3.5); if both paths are real, model a role-playing alias table"
                )
                todo_rel_snippets.append(snippet)
                continue
            raw_rels.append(
                {"name": rel_name, "child": t.name, "columns": [r.column], "parent": r.parent,
                 "parent_columns": [r.parent_column]}
            )

    # ---- pass 4: assemble the raw view (build_view's shape) ----------------
    raw_tables: dict[str, dict] = {}
    partition_dims: list[str] = []
    for t in tables:
        time_cols = [c["column"].name for c in candidates if c["table"] is t and c.get("time")]
        part_type = next((c.data_type for c in t.columns if c.name == t.partition_column), None)
        primary_date = (
            t.partition_column
            if t.partition_column and _time_kind(part_type)
            else (time_cols[0] if time_cols else None)
        )
        comment = f"One row per {_singular(t.name)}. "
        if t.description:
            comment += _sentence(t.description, "") + " "
        if primary_date:
            comment += f"Primary date: {primary_date}. "
        else:
            notes.append(
                f"table {t.name}: no date column found — add one, or exempt the table via "
                "lint 'no_primary_date_tables' (SV-R2.3)"
            )
        if t.partition_column and t.partition_column == primary_date and (t.name, t.partition_column) in names:
            dim_name = names[(t.name, t.partition_column)]
            comment += f"Partition: {t.partition_column} BY {dim_name} (lookback 1d, lookahead 1d). "
            partition_dims.append(dim_name)
        elif t.partition_column:
            notes.append(
                f"table {t.name}: partitioned by '{t.partition_column}' but that column is not exposed "
                "as a time dimension; add the Partition directive once it is"
            )
        comment += f"Source: {t.source}."
        raw_tables[t.name] = {"source": t.source, "primary_key": list(t.primary_key), "comment": comment}
    notes.append("confirm each table's 'One row per ...' grain wording — it was guessed from the name")

    raw_dims, raw_facts, raw_metrics = [], [], []
    for c in candidates:
        t, col = c["table"], c["column"]
        final = names[(t.name, col.name)]
        if c["kind"] == "fact":
            raw_facts.append(
                {"table": t.name, "name": final, "expr": col.name,
                 "comment": _sentence(col.description, f"Row-level numeric value {col.name} on {t.name}")}
            )
            metric = names[(t.name, f"sum_{col.name}")]
            raw_metrics.append(
                {"table": t.name, "name": metric, "expr": f"SUM({final})",
                 "comment": _sentence(col.description, f"Sum of {col.name} on {t.name}")
                 + f" Population: all rows of {t.name}. Additivity: additive."}
            )
            continue
        if c["time"]:
            comment = _sentence(col.description, f"Date column {col.name} on {t.name}") + " Grains: day, week, month."
            if c["time"] == "timestamp":
                comment += " Type: timestamp."
        else:
            comment = _sentence(col.description, f"Categorical attribute {col.name} on {t.name}")
            values = directive_safe(sampler(t.source, col.name)) if sampler and _is_stringy(col.data_type) else None
            if values:
                comment += f" Values: {', '.join(values)}."
        raw_dims.append({"table": t.name, "name": final, "expr": col.name, "comment": comment})

    raw = {
        "name": name,
        "dialect": dialect,
        "comment": f"Scaffolded draft over {len(tables)} table(s) from {origin}; review every TODO before relying on it.",
        "ai_sql_generation": (
            f"Scaffolded draft. Always filter {' or '.join(sorted(set(partition_dims)))}."
            if partition_dims else "Scaffolded draft; review before use."
        ),
        "tables": raw_tables,
        "relationships": raw_rels,
        "facts": raw_facts,
        "dimensions": raw_dims,
        "metrics": raw_metrics,
    }
    view = build_view(raw, origin)
    report = lint(view)

    # ---- header: provenance + everything a human should look at ------------
    head = [
        "# Scaffolded by `ostrict scaffold` — a DRAFT to review, not a finished model.",
        f"# source: {origin}",
        f"# lint at scaffold time: {len(report.errors)} error(s), {len(report.warnings)} warning(s)",
    ]
    if notes:
        head.append("# TODO review:")
        head += [f"#   - {n}" for n in notes]
    if todo_rel_snippets:
        head.append("#   re-add a skipped relationship by pasting it under `relationships:`:")
        for s in todo_rel_snippets:
            head += [f"#     {line}" for line in s.splitlines()]
    text = "\n".join(head) + "\n" + to_native_yaml(view)
    return ScaffoldResult(text, tuple(notes), report, view)
