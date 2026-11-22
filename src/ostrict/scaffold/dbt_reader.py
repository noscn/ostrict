"""Read semantic-view raw material from a dbt project — metadata only, no dbt run.

Two inputs for ``--from-dbt``:

- a compiled ``target/manifest.json`` (preferred when a ``.json`` file is
  passed): model names, schemas/aliases, descriptions, column types, plus
  ``unique`` and ``relationships`` tests (``test_metadata`` + ``attached_node``,
  dbt >= 1.4 shapes);
- a dbt project directory: ``models/**/*.yml`` for descriptions and tests
  (``tests:`` and dbt 1.8's ``data_tests:``), and each model's ``.sql`` for
  ``{{ config(...) }}`` keys — ``schema``, ``alias`` and ``partition_by``
  (dict ``{'field': ...}`` or plain string).

The primary key is the earliest column (in declared order) carrying a
``unique`` test; models without one fall back to the name-convention guess,
with a note. ``relationships`` tests become child -> parent edges.
"""

from __future__ import annotations

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


def read_dbt(path: str | Path, models: list[str] | None = None) -> tuple[list[TableDraft], str]:
    """Return (table drafts, project name) from a manifest.json or a project directory."""
    p = Path(path)
    if p.is_file() and p.suffix.lower() == ".json":
        metas, project = _from_manifest(p)
    elif p.is_dir():
        metas, project = _from_project(p)
    else:
        raise CompilerError(ERR_PARSE, f"{p}: --from-dbt needs a manifest.json or a dbt project directory")
    return _assemble(metas, models, str(p)), project


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
                for c in m.get("columns") or []:
                    cname = str(c["name"])
                    meta["columns"].append(
                        ColumnDraft(name=cname, data_type=c.get("data_type"), description=str(c.get("description") or ""))
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
        schema, alias = node.get("schema"), node.get("alias") or node["name"]
        meta["source"] = f"{schema}.{alias}" if schema else alias
        part = (node.get("config") or {}).get("partition_by")
        meta["partition"] = part.get("field") if isinstance(part, dict) else part
        for c in (node.get("columns") or {}).values():
            meta["columns"].append(
                ColumnDraft(name=str(c["name"]), data_type=c.get("data_type"), description=str(c.get("description") or ""))
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
    return list(metas.values()), project


# ---- shared assembly ----------------------------------------------------------


def _blank(name: str, description: str) -> dict:
    return {"name": name, "description": description, "source": name,
            "columns": [], "uniques": [], "rels": [], "partition": None}


def _assemble(metas: list[dict], selected: list[str] | None, origin: str) -> list[TableDraft]:
    if not metas:
        raise CompilerError(ERR_PARSE, f"{origin}: no dbt models found")
    names = [m["name"] for m in metas]
    if selected:
        missing = sorted(set(selected) - set(names))
        if missing:
            raise CompilerError(
                ERR_PARSE,
                f"{origin}: unknown model(s) {', '.join(missing)}; available: {', '.join(sorted(names))}",
            )
        metas = [m for m in metas if m["name"] in selected]
    elif len(metas) > MAX_UNFILTERED_MODELS:
        raise CompilerError(
            ERR_PARSE,
            f"{origin}: {len(metas)} models in the project; pass --models to pick the view's "
            f"tables (SV-R2.5 caps a view at {MAX_UNFILTERED_MODELS})",
        )

    chosen = {m["name"] for m in metas}
    tables = []
    for m in metas:
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
