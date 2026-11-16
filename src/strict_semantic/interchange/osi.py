"""Bidirectional OSI (Open Semantic Interchange) adapter.

Mapping contract: ``docs/osi-mapping.md``. Standard OSI
slots (datasets, fields, relationships, metrics) map 1:1 onto the canonical
AST; strict-semantic guardrails that vanilla OSI has no slot for (dialect,
partition pruning bounds, snapshot flags, time types) travel in a namespaced
``custom_extensions.strict_semantic`` block at the document, dataset and field
level, so an export → import round-trip rebuilds an equal ``SemanticView``.

Import feeds :func:`strict_semantic.parsers.common.build_view`'s raw shape —
the adapter the native-YAML module reserved for a pinned interchange format —
so OSI, Snowflake DDL and native YAML construct views identically (same
canonicalization, same directive reconciliation), and every import passes the
full SV-R1..R10 gate before it can be compiled or written out.

Two deliberate asymmetries, documented in ``docs/REVISIONS.md``:

- OSI nests facts/dimensions under datasets, so a view whose fields interleave
  across tables round-trips with them regrouped in table order (order within a
  table is preserved; compilation resolves by name and is unaffected).
- The raw shape has no explicit enumeration key (the native format expresses
  it only as a ``Values:`` comment directive), so OSI ``fields[].values`` are
  reconciled after construction with the same agree-or-fail semantics as
  ``_merge``.

Unknown OSI keys are ignored on import; missing required ones are refused with
``ERR_PARSE``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, NamedTuple

import pydantic
import yaml

from ..linter import LintConfig, ensure_valid
from ..models.ast import SemanticView
from ..models.errors import ERR_PARSE, CompilerError
from ..parsers.common import (
    _GRAINS,
    _PARTITION,
    _PRIMARY_DATE,
    _SNAPSHOT,
    _TYPE,
    _VALUES,
    build_view,
)

_EXT = "strict_semantic"
_YAML_OPTS = {"sort_keys": False, "allow_unicode": True, "width": 4096}


class OsiExport(NamedTuple):
    """A rendered OSI document plus warnings about guardrails vanilla OSI cannot carry."""

    text: str
    warnings: tuple[str, ...]


def _clean(d: dict) -> dict:
    """Drop empty values so the document stays minimal (and the drop is deterministic)."""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


# ---- export ------------------------------------------------------------------


def view_to_osi(view: SemanticView, *, strip_extensions: bool = False) -> tuple[dict, list[str]]:
    """Map the canonical AST to an OSI document (plain dict of lists/strings)."""
    warnings: list[str] = []
    if strip_extensions and view.dialect != "snowflake":
        warnings.append(f"dialect '{view.dialect}' is stripped; vanilla OSI importers will assume 'snowflake'")

    datasets = []
    for t in view.tables.values():
        fields = []
        for f in view.facts:
            if f.table == t.name:
                fields.append(_clean({"name": f.name, "kind": "fact", "expression": f.expr, "description": f.comment}))
        for d in view.dimensions:
            if d.table != t.name:
                continue
            fd = _clean(
                {
                    "name": d.name,
                    "kind": "dimension",
                    "expression": d.expr,
                    "description": d.comment,
                    "synonyms": list(d.synonyms),
                    "time_grains": list(d.grains),
                    "values": list(d.values),
                }
            )
            if d.time_type != "date":
                if strip_extensions:
                    warnings.append(
                        f"dimension {d.table}.{d.name}: time type '{d.time_type}' is stripped from custom_extensions"
                    )
                else:
                    fd["custom_extensions"] = {_EXT: {"time_type": d.time_type}}
            fields.append(fd)

        ds = _clean(
            {
                "name": t.name,
                "source": t.source,
                "primary_key": list(t.primary_key),
                "description": t.comment,
                "filter": t.filter,
                "fields": fields,
            }
        )
        dext: dict = {}
        if t.primary_date:
            dext["primary_date"] = t.primary_date
        if t.snapshot:
            dext["snapshot"] = True
        if t.partition:
            p = t.partition
            dext["partition"] = {
                "column": p.column,
                "dimension": p.dimension,
                "lookback_days": p.lookback_days,
                "lookahead_days": p.lookahead_days,
            }
            if strip_extensions:
                warnings.append(
                    f"table {t.name}: partition pruning bounds ({p.column} BY {p.dimension}, "
                    f"lookback {p.lookback_days}d, lookahead {p.lookahead_days}d) are stripped from custom_extensions"
                )
        if strip_extensions:
            if t.primary_date:
                warnings.append(f"table {t.name}: primary date '{t.primary_date}' is stripped from custom_extensions")
            if t.snapshot:
                warnings.append(f"table {t.name}: the point-in-time snapshot flag is stripped from custom_extensions")
        elif dext:
            ds["custom_extensions"] = {_EXT: dext}
        datasets.append(ds)

    doc: dict = {
        "semantic_model": _clean({"name": view.name, "description": view.comment}),
        "datasets": datasets,
    }
    if view.ai_sql_generation:
        doc["semantic_model"]["ai_context"] = {"instructions": view.ai_sql_generation}
    if view.relationships:
        doc["relationships"] = [
            {
                "name": r.name,
                "from_dataset": r.child,
                "from_columns": list(r.child_columns),
                "to_dataset": r.parent,
                "to_columns": list(r.parent_columns),
            }
            for r in view.relationships
        ]
    if view.metrics:
        doc["metrics"] = [
            _clean(
                {
                    "name": m.name,
                    "dataset": m.table,
                    "expression": m.expr,
                    "description": m.comment,
                    "synonyms": list(m.synonyms),
                    "non_additive_by": m.non_additive_by,
                }
            )
            for m in view.metrics
        ]
    if strip_extensions:
        if warnings:
            warnings.append(
                "stripped guardrails survive only as description directives, "
                "which vanilla OSI consumers do not interpret"
            )
    else:
        ext: dict = {"dialect": view.dialect}
        if view.ai_sql_generation:
            ext["ai_sql_generation"] = view.ai_sql_generation
        doc["custom_extensions"] = {_EXT: ext}
    return doc, warnings


def export_osi(
    view: SemanticView, *, fmt: Literal["yaml", "json"] = "yaml", strip_extensions: bool = False
) -> OsiExport:
    """Render a view as an OSI YAML or JSON document (deterministic text)."""
    doc, warnings = view_to_osi(view, strip_extensions=strip_extensions)
    if fmt == "yaml":
        text = yaml.safe_dump(doc, **_YAML_OPTS)
    elif fmt == "json":
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    else:
        raise CompilerError(ERR_PARSE, f"unknown OSI format '{fmt}' (use yaml or json)")
    return OsiExport(text, tuple(warnings))


# ---- import ------------------------------------------------------------------


def _req(mapping, key: str, where: str, origin: str):
    if not isinstance(mapping, dict) or mapping.get(key) in (None, "", [], {}):
        raise CompilerError(ERR_PARSE, f"{origin}: {where}: missing required OSI key '{key}'")
    return mapping[key]


def _agree(a, b, origin: str, where: str, what: str):
    if a is not None and b is not None and a != b:
        raise CompilerError(
            ERR_PARSE, f"{origin}: {where}: {what} is '{a}' in custom_extensions but '{b}' elsewhere in the document"
        )
    return a if a is not None else b


def _ext_of(node: dict, where: str, origin: str) -> dict:
    ce = node.get("custom_extensions") or {}
    ext = ce.get(_EXT, {}) if isinstance(ce, dict) else None
    if ext is None or not isinstance(ext, dict):
        raise CompilerError(ERR_PARSE, f"{origin}: {where}: custom_extensions.{_EXT} must be a mapping")
    return ext


def _strs(v, where: str, what: str, origin: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    raise CompilerError(ERR_PARSE, f"{origin}: {where}: {what} must be a list")


def _listsection(node: dict, key: str, origin: str) -> list:
    v = node.get(key) or []
    if not isinstance(v, list):
        raise CompilerError(ERR_PARSE, f"{origin}: '{key}' must be a list")
    return v


def osi_to_view(
    doc, *, origin: str = "osi", validate: bool = True, lint_config: LintConfig | None = None
) -> SemanticView:
    """Map an OSI document (dict) onto ``build_view``'s raw shape and build the AST."""
    if not isinstance(doc, dict):
        raise CompilerError(ERR_PARSE, f"{origin}: expected a mapping at the top level")
    sm = _req(doc, "semantic_model", "document", origin)
    name = _req(sm, "name", "semantic_model", origin)
    ai_ctx = sm.get("ai_context") or {}
    if not isinstance(ai_ctx, dict):
        raise CompilerError(ERR_PARSE, f"{origin}: semantic_model.ai_context must be a mapping")
    ext = _ext_of(doc, "document", origin)
    raw: dict = {
        "name": name,
        "dialect": ext.get("dialect") or "snowflake",
        "comment": sm.get("description"),
        "ai_sql_generation": _agree(
            ext.get("ai_sql_generation"), ai_ctx.get("instructions"), origin, "semantic_model", "ai instructions"
        ),
        "tables": {},
        "relationships": [],
        "facts": [],
        "dimensions": [],
        "metrics": [],
    }

    _req(doc, "datasets", "document", origin)
    declared_values: dict[tuple[str, str], tuple[str, ...]] = {}
    for ds in _listsection(doc, "datasets", origin):
        tname = _req(ds, "name", "dataset", origin)
        if tname in raw["tables"]:
            raise CompilerError(ERR_PARSE, f"{origin}: duplicate dataset '{tname}'")
        dext = _ext_of(ds, f"dataset {tname}", origin)
        raw["tables"][tname] = {
            "source": _req(ds, "source", f"dataset {tname}", origin),
            "primary_key": ds.get("primary_key"),
            "comment": ds.get("description"),
            "filter": ds.get("filter"),
            "primary_date": dext.get("primary_date"),
            "snapshot": dext.get("snapshot"),
            "partition": dext.get("partition"),
        }
        for f in _listsection(ds, "fields", origin):
            fname = _req(f, "name", f"dataset {tname} field", origin)
            where = f"field {tname}.{fname}"
            kind = f.get("kind") or "dimension"
            entry = {
                "table": tname,
                "name": fname,
                "expr": _req(f, "expression", where, origin),
                "comment": f.get("description"),
            }
            if kind == "fact":
                raw["facts"].append(entry)
            elif kind == "dimension":
                fext = _ext_of(f, where, origin)
                entry.update(
                    {
                        "synonyms": _strs(f.get("synonyms"), where, "synonyms", origin),
                        "grains": _strs(f.get("time_grains"), where, "time_grains", origin),
                        "type": fext.get("time_type"),
                    }
                )
                raw["dimensions"].append(entry)
                values = _strs(f.get("values"), where, "values", origin)
                if values:
                    declared_values[(tname, fname)] = tuple(values)
            else:
                raise CompilerError(ERR_PARSE, f"{origin}: {where}: unknown kind '{kind}' (use fact or dimension)")

    for r in _listsection(doc, "relationships", origin):
        raw["relationships"].append(
            {
                "name": r.get("name"),
                "child": _req(r, "from_dataset", "relationship", origin),
                "columns": _strs(_req(r, "from_columns", "relationship", origin), "relationship", "from_columns", origin),
                "parent": _req(r, "to_dataset", "relationship", origin),
                "parent_columns": _strs(r.get("to_columns"), "relationship", "to_columns", origin) or None,
            }
        )

    for m in _listsection(doc, "metrics", origin):
        mname = _req(m, "name", "metric", origin)
        raw["metrics"].append(
            {
                "table": m.get("dataset"),
                "name": mname,
                "expr": _req(m, "expression", f"metric {mname}", origin),
                "comment": m.get("description"),
                "synonyms": _strs(m.get("synonyms"), f"metric {mname}", "synonyms", origin),
                "non_additive_by": m.get("non_additive_by"),
            }
        )

    try:
        view = build_view(raw, origin)
    except (pydantic.ValidationError, TypeError) as e:
        raise CompilerError(ERR_PARSE, f"{origin}: invalid OSI document: {e}") from None
    view = _apply_declared_values(view, declared_values, origin)
    if validate:
        ensure_valid(view, lint_config)
    return view


def _apply_declared_values(
    view: SemanticView, declared: dict[tuple[str, str], tuple[str, ...]], origin: str
) -> SemanticView:
    """OSI ``fields[].values`` has no slot in the raw shape; reconcile it like ``_merge``."""
    if not declared:
        return view
    dims, changed = [], False
    for d in view.dimensions:
        vals = declared.get((d.table, d.name))
        if vals is None or d.values == vals:
            dims.append(d)
            continue
        if d.values:
            raise CompilerError(
                ERR_PARSE,
                f"{origin}: dimension {d.name}: values are {list(vals)} in the OSI field "
                f"but {list(d.values)} in the comment",
            )
        dims.append(d.model_copy(update={"values": vals}))
        changed = True
    return view.model_copy(update={"dimensions": tuple(dims)}) if changed else view


def import_osi(
    source: str | Path, *, validate: bool = True, lint_config: LintConfig | None = None
) -> SemanticView:
    """Parse an OSI YAML/JSON document into a linted ``SemanticView``.

    A ``Path`` reads that file; a ``str`` is the document text itself. With
    ``validate=True`` (the default) the imported model must pass SV-R1..R10
    (``ensure_valid``) — external models cannot bypass the ground rules.
    """
    if isinstance(source, Path):
        origin, text = source.name, source.read_text()
    else:
        origin, text = "osi", source
    try:
        doc = yaml.safe_load(text)  # YAML is a JSON superset, so this reads both
    except yaml.YAMLError as e:
        raise CompilerError(ERR_PARSE, f"{origin}: invalid OSI YAML/JSON: {e}") from None
    return osi_to_view(doc, origin=origin, validate=validate, lint_config=lint_config)


# ---- native emission (the back half of `strict-semantic import`) -------------


def to_native_yaml(view: SemanticView) -> str:
    """Render a view in the native YAML contract (``parsers/yaml_spec.py``).

    ``load_view`` of the result rebuilds an equal view, with one exception: an
    enumeration that exists only as OSI ``values`` (no ``Values:`` directive in
    the comment) is appended to the comment as a directive — the only slot the
    native format has for it. Explicit keys are emitted only where the comment
    carries no directive, so hand-written-style specs stay minimal.
    """
    doc: dict = {"name": view.name, "dialect": view.dialect}
    if view.comment:
        doc["comment"] = view.comment
    if view.ai_sql_generation:
        doc["ai_sql_generation"] = view.ai_sql_generation

    tables: dict = {}
    for t in view.tables.values():
        e: dict = {"source": t.source}
        if t.primary_key:
            e["primary_key"] = list(t.primary_key)
        if t.comment:
            e["comment"] = t.comment
        if t.filter:
            e["filter"] = t.filter
        if t.primary_date and not _PRIMARY_DATE.search(t.comment):
            e["primary_date"] = t.primary_date
        if t.snapshot and not _SNAPSHOT.search(t.comment):
            e["snapshot"] = True
        if t.partition and not _PARTITION.search(t.comment):
            e["partition"] = {
                "column": t.partition.column,
                "dimension": t.partition.dimension,
                "lookback_days": t.partition.lookback_days,
                "lookahead_days": t.partition.lookahead_days,
            }
        tables[t.name] = e
    doc["tables"] = tables

    if view.relationships:
        doc["relationships"] = [
            {
                "name": r.name,
                "from": f"{r.child} ({', '.join(r.child_columns)})",
                "to": f"{r.parent} ({', '.join(r.parent_columns)})",
            }
            for r in view.relationships
        ]

    if view.facts:
        doc["facts"] = {
            f"{f.table}.{f.name}": _clean({"expr": f.expr, "comment": f.comment}) for f in view.facts
        }

    if view.dimensions:
        dims: dict = {}
        for d in view.dimensions:
            e = {"expr": d.expr}
            comment = d.comment
            if d.values and not _VALUES.search(comment):
                comment = (comment.rstrip() + " " if comment.strip() else "") + "Values: " + ", ".join(d.values) + "."
            if d.synonyms:
                e["synonyms"] = list(d.synonyms)
            if comment:
                e["comment"] = comment
            if d.grains and not _GRAINS.search(comment):
                e["grains"] = list(d.grains)
            if d.time_type != "date" and not _TYPE.search(comment):
                e["type"] = d.time_type
            dims[f"{d.table}.{d.name}"] = e
        doc["dimensions"] = dims

    if view.metrics:
        doc["metrics"] = {
            (f"{m.table}.{m.name}" if m.table else m.name): _clean(
                {
                    "expr": m.expr,
                    "comment": m.comment,
                    "synonyms": list(m.synonyms),
                    "non_additive_by": m.non_additive_by,
                }
            )
            for m in view.metrics
        }

    return yaml.safe_dump(doc, **_YAML_OPTS)
