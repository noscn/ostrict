"""Read semantic-view raw material from a dbt project — metadata only, no dbt run.

Two inputs for ``--from-dbt``:

- a compiled ``target/manifest.json`` (preferred when a ``.json`` file is
  passed): model names, schemas/aliases, descriptions, column types/meta/tags,
  plus ``unique``, ``relationships`` and ``accepted_values`` tests
  (``test_metadata`` + ``attached_node``, dbt >= 1.4 shapes);
- a dbt project directory: ``models/**/*.yml`` for descriptions, tests
  (``tests:`` and dbt 1.8's ``data_tests:``), column ``meta`` and model tags,
  and each model's ``.sql`` for ``{{ config(...) }}`` keys — ``schema``,
  ``alias`` and ``partition_by`` (dict ``{'field': ...}`` or plain string).

Column types come from (in order): an explicit ``data_type`` in the yml, then
``catalog.json`` — auto-discovered next to the manifest or under the project's
``target/`` (or passed explicitly) — dbt's standard artifact from
``dbt docs generate``, which carries real warehouse types with zero scanning.

The primary key is the earliest column (in declared order) carrying a
``unique`` test; models without one fall back to the name-convention guess,
with a note. ``relationships`` tests become child -> parent edges, and
``accepted_values`` lists become ``Values:`` enumerations.

``--models`` accepts exact names, ``fnmatch`` globs (``int_dw_*``) and dbt
tags (``tag:semantic``); tokens that match nothing are refused.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import yaml

from ..models.errors import ERR_PARSE, CompilerError
from .generator import ColumnDraft, RelDraft, TableDraft, guess_primary_key

_REF = re.compile(r"ref\(\s*['\"]([\w.]+)['\"]")
_SCHEMA = re.compile(r"\bschema\s*=\s*['\"](\w+)['\"]")
_ALIAS = re.compile(r"\balias\s*=\s*['\"](\w+)['\"]")
_PARTITION = re.compile(r"partition_by\s*=\s*(?:\{[^}]*?['\"]field['\"]\s*:\s*['\"](\w+)['\"]|['\"](\w+)['\"])")

MAX_UNFILTERED_MODELS = 10  # SV-R2.5's table cap: beyond it, --models is required


def read_dbt(
    path: str | Path, models: list[str] | None = None, catalog: str | Path | None = None
) -> tuple[list[TableDraft], str, list[str]]:
    """Return (table drafts, project name, notes) from a manifest.json or a project directory."""
    p = Path(path)
    if p.is_file() and p.suffix.lower() == ".json":
        metas, project = _from_manifest(p)
        catalog_path = Path(catalog) if catalog else p.parent / "catalog.json"
    elif p.is_dir():
        metas, project = _from_project(p)
        catalog_path = Path(catalog) if catalog else p / "target" / "catalog.json"
    else:
        raise CompilerError(ERR_PARSE, f"{p}: --from-dbt needs a manifest.json or a dbt project directory")
    notes = _apply_catalog(metas, catalog_path, explicit=catalog is not None)
    return _assemble(metas, models, str(p)), project, notes


# ---- project directory --------------------------------------------------------


def _from_project(root: Path) -> tuple[list[dict], str]:
    proj_file = root / "dbt_project.yml"
    proj = (yaml.safe_load(proj_file.read_text()) or {}) if proj_file.is_file() else {}
    project = str(proj.get("name") or root.name)
    model_dirs = [root / d for d in (proj.get("model-paths") or ["models"])]

    metas: dict[str, dict] = {}
    for d in model_dirs:
        if not d.is_dir():
            continue
        for doc_path in sorted([*d.rglob("*.yml"), *d.rglob("*.yaml")]):
            doc = yaml.safe_load(doc_path.read_text()) or {}
            for m in doc.get("models") or []:
                meta = _blank(str(m["name"]), str(m.get("description") or ""))
                meta["tags"] = list((m.get("config") or {}).get("tags") or m.get("tags") or [])
                for c in m.get("columns") or []:
                    cname = str(c["name"])
                    meta["columns"].append(
                        ColumnDraft(
                            name=cname,
                            data_type=c.get("data_type"),
                            description=str(c.get("description") or ""),
                            meta=c.get("meta") if isinstance(c.get("meta"), dict) else None,
                        )
                    )
                    for item in c.get("tests") or c.get("data_tests") or []:
                        if item == "unique":
                            meta["uniques"].append(cname)
                        elif isinstance(item, dict) and "relationships" in item:
                            cfg = item["relationships"] or {}
                            to = str(cfg.get("to") or "")
                            ref = _REF.search(to)
                            meta["rels"].append(
                                RelDraft(column=cname, parent=ref.group(1) if ref else to,
                                         parent_column=str(cfg.get("field") or cname))
                            )
                        elif isinstance(item, dict) and "accepted_values" in item:
                            vals = (item["accepted_values"] or {}).get("values")
                            if isinstance(vals, list):
                                meta["colvalues"][cname] = [str(v) for v in vals]
                metas[meta["name"]] = meta

    for name, meta in metas.items():
        sql = next((f for d in model_dirs if d.is_dir() for f in sorted(d.rglob(f"{name}.sql"))), None)
        if sql is None:
            continue
        text = sql.read_text()
        schema = (m.group(1) if (m := _SCHEMA.search(text)) else None)
        alias = (m.group(1) if (m := _ALIAS.search(text)) else None)
        part = _PARTITION.search(text)
        meta["partition"] = (part.group(1) or part.group(2)) if part else None
        meta["source"] = f"{schema}.{alias or name}" if schema else (alias or name)
    return list(metas.values()), project


# ---- manifest.json ------------------------------------------------------------


def _from_manifest(path: Path) -> tuple[list[dict], str]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise CompilerError(ERR_PARSE, f"{path.name}: invalid manifest JSON: {e}") from None
    project = str((data.get("metadata") or {}).get("project_name") or path.stem)
    nodes = data.get("nodes") or {}

    metas: dict[str, dict] = {}
    for node in nodes.values():
        if node.get("resource_type") != "model":
            continue
        meta = _blank(str(node["name"]), str(node.get("description") or ""))
        meta["tags"] = list(node.get("tags") or [])
        schema, alias = node.get("schema"), node.get("alias") or node["name"]
        meta["source"] = f"{schema}.{alias}" if schema else alias
        part = (node.get("config") or {}).get("partition_by")
        meta["partition"] = part.get("field") if isinstance(part, dict) else part
        for c in (node.get("columns") or {}).values():
            meta["columns"].append(
                ColumnDraft(
                    name=str(c["name"]),
                    data_type=c.get("data_type"),
                    description=str(c.get("description") or ""),
                    meta=c.get("meta") if isinstance(c.get("meta"), dict) else None,
                )
            )
        metas[meta["name"]] = meta

    for node in nodes.values():
        tm = node.get("test_metadata")
        if node.get("resource_type") != "test" or not tm:
            continue
        kwargs = tm.get("kwargs") or {}
        attached = str(node.get("attached_node") or "")
        ref = _REF.search(str(kwargs.get("model") or ""))
        model = attached.rpartition(".")[2] if attached else (ref.group(1) if ref else "")
        meta = metas.get(model)
        col = kwargs.get("column_name")
        if meta is None or not col:
            continue
        if tm.get("name") == "unique":
            meta["uniques"].append(str(col))
        elif tm.get("name") == "relationships":
            to = _REF.search(str(kwargs.get("to") or ""))
            if to:
                meta["rels"].append(
                    RelDraft(column=str(col), parent=to.group(1), parent_column=str(kwargs.get("field") or col))
                )
        elif tm.get("name") == "accepted_values" and isinstance(kwargs.get("values"), list):
            meta["colvalues"][str(col)] = [str(v) for v in kwargs["values"]]
    return list(metas.values()), project


# ---- catalog.json (dbt docs generate): real column types, zero scanning -------


def _apply_catalog(metas: list[dict], path: Path, *, explicit: bool) -> list[str]:
    if not path.is_file():
        if explicit:
            raise CompilerError(ERR_PARSE, f"{path}: --catalog file not found")
        return []
    try:
        nodes = (json.loads(path.read_text()) or {}).get("nodes") or {}
    except json.JSONDecodeError as e:
        raise CompilerError(ERR_PARSE, f"{path.name}: invalid catalog JSON: {e}") from None
    by_model: dict[str, dict[str, str]] = {}
    for unique_id, node in nodes.items():
        cols = {str(c.get("name")): str(c.get("type") or "") for c in (node.get("columns") or {}).values()}
        by_model[unique_id.rpartition(".")[2]] = cols
    filled = 0
    for meta in metas:
        cols = by_model.get(meta["name"])
        if not cols:
            continue
        folded = {k.lower(): v for k, v in cols.items()}
        for c in meta["columns"]:
            if c.data_type:
                continue  # an explicit yml data_type wins
            t = cols.get(c.name) or folded.get(c.name.lower())
            if t:
                c.data_type = t
                filled += 1
    return [f"{filled} column type(s) read from {path.name} (dbt docs generate)"] if filled else []


# ---- shared assembly ----------------------------------------------------------


def _blank(name: str, description: str) -> dict:
    return {"name": name, "description": description, "source": name, "tags": [],
            "columns": [], "uniques": [], "rels": [], "colvalues": {}, "partition": None}


def _select(metas: list[dict], selected: list[str] | None, origin: str) -> list[dict]:
    if not selected:
        if len(metas) > MAX_UNFILTERED_MODELS:
            raise CompilerError(
                ERR_PARSE,
                f"{origin}: {len(metas)} models in the project; pass --models to pick the view's "
                f"tables (SV-R2.5 caps a view at {MAX_UNFILTERED_MODELS})",
            )
        return metas
    names = {m["name"]: m for m in metas}
    picked: set[str] = set()
    unmatched = []
    for tok in selected:
        if tok.startswith("tag:"):
            hits = [m["name"] for m in metas if tok[4:] in m["tags"]]
        elif any(ch in tok for ch in "*?["):
            hits = [m["name"] for m in metas if fnmatch.fnmatch(m["name"], tok)]
        else:
            hits = [tok] if tok in names else []
        if hits:
            picked.update(hits)
        else:
            unmatched.append(tok)
    if unmatched:
        raise CompilerError(
            ERR_PARSE,
            f"{origin}: --models matched no models for: {', '.join(unmatched)}; "
            f"available: {', '.join(sorted(names))}",
        )
    return [m for m in metas if m["name"] in picked]


def _assemble(metas: list[dict], selected: list[str] | None, origin: str) -> list[TableDraft]:
    if not metas:
        raise CompilerError(ERR_PARSE, f"{origin}: no dbt models found")
    metas = _select(metas, selected, origin)
    chosen = {m["name"] for m in metas}
    tables = []
    for m in metas:
        for c in m["columns"]:
            if c.name in m["colvalues"]:
                c.values = m["colvalues"][c.name]
        order = {c.name: i for i, c in enumerate(m["columns"])}
        uniques = sorted((u for u in set(m["uniques"]) if u in order), key=order.__getitem__)
        notes = []
        if uniques:
            pk = [uniques[0]]
        else:
            pk, note = guess_primary_key(m["name"], [c.name for c in m["columns"]])
            if note:
                notes.append(note)
        rels = [r for r in m["rels"] if r.parent in chosen]
        for r in m["rels"]:
            if r.parent not in chosen:
                notes.append(f"relationship on '{r.column}' skipped: parent model '{r.parent}' not selected")
        tables.append(
            TableDraft(name=m["name"], source=m["source"], columns=list(m["columns"]),
                       description=m["description"], primary_key=pk, partition_column=m["partition"],
                       relationships=rels, notes=notes)
        )
    return tables
