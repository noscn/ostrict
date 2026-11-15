"""Dual input layer: Snowflake DDL (.sql) and native YAML (.yml/.yaml) -> one AST."""

from __future__ import annotations

from pathlib import Path

from ..models.ast import SemanticView
from ..models.errors import ERR_PARSE, CompilerError
from .snowflake_ddl import parse_ddl_view
from .yaml_spec import parse_yaml_view

__all__ = ["load_view", "load_views", "parse_ddl_view", "parse_yaml_view"]


def load_view(path: str | Path) -> SemanticView:
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() == ".sql":
        return parse_ddl_view(text, origin=p.name)
    if p.suffix.lower() in (".yml", ".yaml"):
        return parse_yaml_view(text, origin=p.name)
    raise CompilerError(ERR_PARSE, f"{p.name}: unknown spec format '{p.suffix}' (use .sql, .yml or .yaml)")


def load_views(directory: str | Path) -> dict[str, SemanticView]:
    views: dict[str, SemanticView] = {}
    for p in sorted(Path(directory).iterdir()):
        if p.suffix.lower() not in (".sql", ".yml", ".yaml"):
            continue
        v = load_view(p)
        if v.name in views:
            raise CompilerError(ERR_PARSE, f"{p.name}: duplicate view name '{v.name}'")
        views[v.name] = v
    return views
