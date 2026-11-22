"""Turn raw table metadata into a draft semantic view that passes SV-R1..R10.

The generator never invents semantics — it classifies what the metadata says
(numeric -> FACT + a sample SUM metric, date/timestamp -> time dimension,
text/bool -> dimension, plus one COUNT metric per keyed table) and applies the
linter's ground rules defensively:

- columns matching the security deny-list are dropped (SV-R6.2), honoring the
  same ``LintConfig`` (deny + allowlist) the linter uses;
- columns that merely *look* like personal data (email, phone, names, bank
  identifiers, ...) are kept but flagged ``TODO(PII-check)`` — flagging is the
  scaffold's job, blocking is the deny-list's;
- key columns (primary keys, relationship columns, dangling ``*_id``) are
  never exposed as dimensions, and sequence-like numerics get a FACT but no
  sample SUM metric;
- names that collide across tables get a ``<table>_`` prefix (SV-R6.4);
- relationships that would break the join forest — self-references (SV-R3.1)
  or a second path between two tables (SV-R3.5) — move into a ``# TODO``
  comment, and name-matched foreign keys (``customer_id`` / ``memberId``) that
  were never declared become paste-ready *candidate join* snippets instead of
  silent orphans;
- every table gets a grain sentence (reusing the description's own
  "One row per ..." when it already starts with one), a primary date and a
  Source (SV-R2.1, SV-R2.3, SV-R8.1), and partitioned tables get a
  conservative ``Partition: ... (lookback 1d, lookahead 1d).`` hint;
- declared enumerations (dbt ``accepted_values`` tests, ``meta.semantic``
  overrides) become ``Values:`` directives in the author's order; sampled
  values stay opt-in;
- ``meta: {semantic: {kind: fact|dimension|skip, values: [...]}}`` (or
  ``meta.ostrict``) overrides classification, so regenerating a scaffold keeps
  an engineer's tuning;
- with ``infer_types=True``, untyped columns ending ``At``/``_at``/``Date``/
  ``_date``/``Timestamp``/``_timestamp`` are drafted as time dimensions, each
  inference counted in the header.

The output is a DRAFT for a human to review (the TODO header says exactly what
to check), assembled through the same ``build_view`` -> ``lint`` ->
``to_native_yaml`` pipeline the rest of the project uses, so what comes out is
exactly what ``ostrict lint`` will see.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from ..interchange import to_native_yaml
from ..linter import LintConfig, LintReport, lint
from ..models.ast import SemanticView
from ..parsers.common import build_view

_NUMERIC = ("INT", "BIGINT", "SMALLINT", "TINYINT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")
_STRINGY = ("VARCHAR", "TEXT", "STRING", "CHAR")
_SAMPLE_CAP = 15  # SV-R6.7: declare Values: only for genuinely low-cardinality columns

# Kept but flagged: names that usually mean personal data (the deny-list blocks, this warns).
PII_SUSPECT = ("email", "*email", "phone", "*phone", "mobile", "firstname", "lastname",
               "fullname", "*_name", "bank*", "iban", "passport", "ssn", "dob")
_SKIP_SUM_SUFFIXES = ("_no", "_num", "_code")
_SKIP_SUM_NAMES = {"sequence", "version", "rank", "status_code"}
_DEV_SCHEMA_PREFIXES = ("dev_", "sandbox_", "staging_")
_MAX_CANDIDATE_JOINS = 20


@dataclass
class ColumnDraft:
    name: str
    data_type: str | None = None  # warehouse/dbt/catalog type; None = unknown
    description: str = ""
    values: list[str] | None = None  # declared enumeration (e.g. dbt accepted_values)
    meta: dict | None = None  # dbt column meta (meta.semantic / meta.ostrict overrides)


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


def _time_name_kind(name: str) -> str | None:
    """Opt-in (--infer-types): the naming conventions that almost always mean a time column."""
    if name.endswith(("_at", "At", "_timestamp", "Timestamp")):
        return "timestamp"
    if name.endswith(("_date", "Date")):
        return "date"
    return None


def _singular(table: str) -> str:
    if len(table) > 3 and table.endswith("s") and not table.endswith("ss"):
        return table[:-1]
    return table


def _denied(name: str, cfg: LintConfig) -> bool:
    n = name.lower()
    if n in (a.lower() for a in cfg.security_allowlist):
        return False
    return any(fnmatch.fnmatch(n, pat) for pat in cfg.security_deny)


def _pii_suspect(name: str) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatch(n, pat) for pat in PII_SUSPECT)


def _skip_sum(name: str) -> bool:
    n = name.lower()
    return n in _SKIP_SUM_NAMES or n.endswith(_SKIP_SUM_SUFFIXES)


def _fk_base(column: str) -> str | None:
    """customer_id -> customer, memberId -> member; None when the name is not key-shaped."""
    if column.lower().endswith("_id") and len(column) > 3:
        return column[:-3]
    if column.endswith("Id") and len(column) > 2:
        return column[:-2]
    return None


def _meta_semantic(col: ColumnDraft) -> dict:
    meta = col.meta or {}
    override = meta.get("ostrict") or meta.get("semantic") or {}
    return override if isinstance(override, dict) else {}


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


def directive_safe(values: list | None) -> list[str] | None:
    """The ``Values: a, b.`` grammar splits on commas and ends at a period; order is kept."""
    if not values or len(values) > _SAMPLE_CAP:
        return None
    out = []
    for v in values:
        if not isinstance(v, str) or not v or "," in v or "." in v or v != v.strip():
            return None
        out.append(v)
    return out


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
    lint_config: LintConfig | None = None,
    infer_types: bool = False,
    extra_notes: Iterable[str] = (),
) -> ScaffoldResult:
    cfg = lint_config or LintConfig()
    tables = sorted(tables, key=lambda t: t.name)
    notes: list[str] = list(extra_notes)
    for t in tables:
        notes += [f"table {t.name}: {n}" for n in t.notes]

    # ---- pass 1: classify columns ------------------------------------------
    key_cols = {t.name: set(t.primary_key) for t in tables}
    for t in tables:
        for r in t.relationships:  # every relationship column is plumbing, kept or not
            key_cols[t.name].add(r.column)

    candidates: list[dict] = []  # {kind: dimension|fact, table, column, time, values, skip_sum}
    pii: dict[str, list[str]] = {}
    untyped: Counter = Counter()
    inferred: Counter = Counter()
    keylike: dict[str, list[str]] = {}
    meta_skipped: dict[str, list[str]] = {}
    for t in tables:
        for c in t.columns:
            sem = _meta_semantic(c)
            override = str(sem.get("kind") or "").lower()
            if override == "skip":
                meta_skipped.setdefault(t.name, []).append(c.name)
                continue
            if _denied(c.name, cfg):
                notes.append(
                    f"column {t.name}.{c.name} dropped: matches the security deny-list (SV-R6.2); "
                    "allow-list it in lint config if it is genuinely safe"
                )
                continue
            if c.name in key_cols[t.name]:
                continue  # keys are join plumbing, never dimensions
            if _pii_suspect(c.name):
                pii.setdefault(t.name, []).append(c.name)
            values = sem.get("values") if isinstance(sem.get("values"), list) else c.values
            entry = {"table": t, "column": c, "values": values, "time": None}
            if override == "fact":
                candidates.append({**entry, "kind": "fact", "skip_sum": _skip_sum(c.name)})
                continue
            if override == "dimension":
                candidates.append({**entry, "kind": "dimension", "time": _time_kind(c.data_type)})
                continue
            time = _time_kind(c.data_type)
            if time:
                candidates.append({**entry, "kind": "dimension", "time": time})
            elif _is_numeric(c.data_type):
                if _fk_base(c.name) or c.name == "id":
                    keylike.setdefault(t.name, []).append(c.name)
                    continue
                candidates.append({**entry, "kind": "fact", "skip_sum": _skip_sum(c.name)})
            else:
                if c.data_type is None:
                    named = _time_name_kind(c.name) if infer_types else None
                    if named:
                        inferred[t.name] += 1
                        candidates.append({**entry, "kind": "dimension", "time": named})
                        continue
                    untyped[t.name] += 1
                candidates.append({**entry, "kind": "dimension"})

    for tname, cols in sorted(meta_skipped.items()):
        notes.append(f"table {tname}: skipped by meta.semantic: {', '.join(cols)}")
    for tname, cols in sorted(keylike.items()):
        notes.append(f"table {tname}: key-shaped column(s) left out (join nothing): {', '.join(cols)}")
    skipped_sums: dict[str, list[str]] = {}
    for c in candidates:
        if c["kind"] == "fact" and c["skip_sum"]:
            skipped_sums.setdefault(c["table"].name, []).append(c["column"].name)
    for tname, cols in sorted(skipped_sums.items()):
        notes.append(f"table {tname}: no sample SUM metric for sequence-like column(s): {', '.join(cols)}")
    for tname, n in sorted(untyped.items()):
        notes.append(f"table {tname}: {n} column(s) have no declared type; drafted as plain dimensions")
    if untyped:
        notes.append(
            "untyped columns: add data_type in the dbt yml, run `dbt docs generate` for a "
            "catalog.json, or pass --infer-types for name-based time inference"
        )
    for tname, n in sorted(inferred.items()):
        notes.append(f"table {tname}: {n} time dimension(s) inferred from column names (--infer-types); verify them")
    for tname, cols in sorted(pii.items()):
        notes.append(
            f"TODO(PII-check) table {tname}: {', '.join(cols)} look like personal data; "
            "confirm exposure is allowed or add them to security_deny"
        )

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
    exposed += [(c["table"].name, f"sum_{c['column'].name}")
                for c in candidates if c["kind"] == "fact" and not c["skip_sum"]]
    exposed += [(t.name, f"{t.name}_count") for t in tables if t.primary_key]
    names = dedupe(exposed)
    renamed = sorted(f"'{b}' -> '{n}'" for (_, b), n in names.items()
                     if n != b and not b.startswith("sum_") and not b.endswith("_count"))
    if renamed:
        notes.append(f"names duplicated across tables were prefixed (SV-R6.4): {', '.join(renamed)}")

    # ---- pass 3: relationships must form a forest (SV-R3.1 / SV-R3.5) ------
    by_name = {t.name: t for t in tables}
    forest = _Forest([t.name for t in tables])
    raw_rels, todo_rel_snippets = [], []
    declared_pairs = {(t.name, r.parent) for t in tables for r in t.relationships}
    declared_pairs |= {(b, a) for a, b in declared_pairs}
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

    # name-matched foreign keys nobody declared: paste-ready suggestions, never auto-joins
    candidate_joins = []
    for t in tables:
        for c in t.columns:
            base = _fk_base(c.name)
            if not base:
                continue
            for p in tables:
                if p is t or (t.name, p.name) in declared_pairs or len(p.primary_key) != 1:
                    continue
                if p.name.lower() == base.lower() or _singular(p.name).lower() == base.lower():
                    candidate_joins.append(
                        f"- name: {t.name}_to_{p.name}\n  from: {t.name} ({c.name})\n"
                        f"  to: {p.name} ({p.primary_key[0]})"
                    )
    candidate_joins = candidate_joins[:_MAX_CANDIDATE_JOINS]
    if candidate_joins:
        notes.append(
            f"{len(candidate_joins)} candidate join(s) matched by column name; verify and paste "
            "them under `relationships:` (snippets in the header)"
        )

    # ---- pass 4: assemble the raw view (build_view's shape) ----------------
    raw_tables: dict[str, dict] = {}
    partition_dims: list[str] = []
    dev_schemas: dict[str, list[str]] = {}
    for t in tables:
        schema = t.source.rpartition(".")[0]
        if schema.lower().startswith(_DEV_SCHEMA_PREFIXES) or schema.lower() in ("dev", "sandbox", "staging"):
            dev_schemas.setdefault(schema, []).append(t.name)
        time_cols = [c["column"].name for c in candidates if c["table"] is t and c.get("time")]
        part_type_ok = t.partition_column in time_cols if t.partition_column else False
        primary_date = (
            t.partition_column if part_type_ok else (time_cols[0] if time_cols else None)
        )
        desc = _sentence(t.description, "") if t.description else ""
        if desc.lower().startswith("one row per"):
            comment = desc + " "  # the description already states the grain — don't repeat it
        else:
            comment = f"One row per {_singular(t.name)}. " + (desc + " " if desc else "")
        if primary_date:
            comment += f"Primary date: {primary_date}. "
        elif t.name not in cfg.no_primary_date_tables:
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
    for schema, tabs in sorted(dev_schemas.items()):
        notes.append(
            f"sources point at dev-looking schema '{schema}' ({', '.join(tabs)}); "
            "regenerate from a production-target artifact before relying on this draft"
        )

    raw_dims, raw_facts, raw_metrics = [], [], []
    for c in candidates:
        t, col = c["table"], c["column"]
        final = names[(t.name, col.name)]
        if c["kind"] == "fact":
            raw_facts.append(
                {"table": t.name, "name": final, "expr": col.name,
                 "comment": _sentence(col.description, f"Row-level numeric value {col.name} on {t.name}")}
            )
            if c["skip_sum"]:
                continue
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
            values = directive_safe(c["values"])
            if c["values"] and not values:
                notes.append(
                    f"table {t.name}: declared values for '{col.name}' contain ',' or '.' and "
                    "cannot be written as a Values: directive"
                )
            if not values and sampler and _is_stringy(col.data_type):
                values = directive_safe(sampler(t.source, col.name))
            if values:
                comment += f" Values: {', '.join(values)}."
        raw_dims.append({"table": t.name, "name": final, "expr": col.name, "comment": comment})

    for t in tables:
        if not t.primary_key:
            continue
        pk = t.primary_key[0]
        raw_metrics.append(
            {"table": t.name, "name": names[(t.name, f"{t.name}_count")], "expr": f"COUNT({pk})",
             "comment": f"Number of {t.name} rows, counted by {pk}. "
             f"Population: all rows of {t.name}. Additivity: additive."}
        )

    sums = [m for m in raw_metrics if m["expr"].startswith("SUM(")]
    counts = [m for m in raw_metrics if not m["expr"].startswith("SUM(")]
    raw_metrics = sums + counts

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
    report = lint(view, cfg)

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
    if candidate_joins:
        head.append("#   candidate joins matched by column name — verify, then paste under `relationships:`:")
        for s in candidate_joins:
            head += [f"#     {line}" for line in s.splitlines()]
    text = "\n".join(head) + "\n" + to_native_yaml(view)
    return ScaffoldResult(text, tuple(notes), report, view)
