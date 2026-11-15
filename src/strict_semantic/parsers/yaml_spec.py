"""Native YAML spec: the same information as the Snowflake DDL, analyst-readable
and friendly to automated export (e.g. generated from dbt model metadata).

Revision note vs. blueprint v5.0: the blueprint named this input "Apache Ossie
YAML". That project could not be verified, so the secondary input is this
project's own documented YAML contract; an adapter for an external interchange
format (e.g. Open Semantic Interchange) can map onto `build_view`'s raw shape
once its spec is pinned down.

Shape:
    name: ecommerce
    dialect: snowflake
    comment: ...
    ai_sql_generation: ...
    tables:
      orders:
        source: shop.orders
        primary_key: [order_id]        # or a single string
        comment: 'One row per order. Primary date: order_date.'
        filter: "status = 'completed'"   # optional always-on predicate
        # snapshot / primary_date / partition may be explicit keys instead of
        # comment directives; when both appear they must agree.
    relationships:
      - orders.customer_id -> customers              # shorthand
      - name: items_orders                           # long form
        from: order_items (order_id)
        to: orders (order_id)
    facts:
      order_items.line_revenue:
        expr: quantity * unit_price
        comment: ...
    dimensions:
      customers.region: { expr: region, synonyms: [market], comment: '... Values: EU, US.' }
    metrics:
      order_items.revenue: { expr: SUM(line_revenue), comment: ... }
      aov: { expr: revenue / order_count, comment: ... }   # no table -> derived
"""

from __future__ import annotations

import re

import yaml

from ..models.ast import SemanticView
from ..models.errors import ERR_PARSE, CompilerError
from .common import build_view

_SIDE = re.compile(r"^\s*(\w+)\s*(?:\.\s*(\w+)|\(\s*([\w\s,]+)\))?\s*$")


def _side(text: str, origin: str) -> tuple[str, list[str]]:
    m = _SIDE.match(str(text))
    if not m:
        raise CompilerError(
            ERR_PARSE, f"{origin}: expected 'table.column' or 'table (col1, col2)', got '{text}'"
        )
    cols = [m.group(2)] if m.group(2) else [c.strip() for c in (m.group(3) or "").split(",") if c.strip()]
    return m.group(1), cols


def _relationship(r, origin: str) -> dict:
    if isinstance(r, str):
        if "->" not in r:
            raise CompilerError(ERR_PARSE, f"{origin}: relationship '{r}' must be 'child.fk -> parent'")
        left, right = (s.strip() for s in r.split("->", 1))
        name = None
    else:
        left, right, name = r.get("from", ""), r.get("to", ""), r.get("name")
    child, columns = _side(left, origin)
    parent, parent_columns = _side(right, origin)
    if not columns:
        raise CompilerError(ERR_PARSE, f"{origin}: relationship '{left} -> {right}' names no child columns")
    out = {"name": name, "child": child, "columns": columns, "parent": parent}
    if parent_columns:
        out["parent_columns"] = parent_columns
    return out


def _fields(section: dict | None, origin: str, section_name: str) -> list[dict]:
    out = []
    for key, body in (section or {}).items():
        body = dict(body) if isinstance(body, dict) else {"expr": body}
        if "expr" not in body:
            raise CompilerError(ERR_PARSE, f"{origin}: {section_name} '{key}' needs an 'expr'")
        table, _, name = str(key).rpartition(".")
        body.update({"table": table or None, "name": name})
        out.append(body)
    return out


def parse_yaml(text: str, origin: str = "yaml") -> dict:
    """YAML text -> the raw dict shape shared with the DDL parser."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise CompilerError(ERR_PARSE, f"{origin}: invalid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise CompilerError(ERR_PARSE, f"{origin}: expected a mapping at the top level")
    raw = dict(raw)
    raw["relationships"] = [_relationship(r, origin) for r in raw.get("relationships") or []]
    for section in ("facts", "dimensions", "metrics"):
        raw[section] = _fields(raw.get(section), origin, section[:-1])
    return raw


def parse_yaml_view(text: str, origin: str = "yaml") -> SemanticView:
    return build_view(parse_yaml(text, origin), origin)
