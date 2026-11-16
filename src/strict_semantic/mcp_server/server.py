"""MCP server: the five blueprint tools over stdio (mcp 2.x MCPServer).

The agent only ever picks names from the catalog — it never writes SQL — and
every refusal carries its error code and a repair hint, raised as ToolError so
the model actually sees it (mcp 2.x shows generic text for other exceptions).

Config file (path in STRICT_SEMANTIC_CONFIG, default ./strict_semantic.yml;
relative paths resolve against the config file's folder):

    views: ./views                   # a folder of specs, or one .sql/.yaml file
    connector:
      type: duckdb                   # duckdb | bigquery
      seed: ./seed.sql               # duckdb: optional fixture SQL
      # type: bigquery
      # project: my-project
      # credentials: /path/sa.json   # omit to use ADC
      # max_bytes: 100000000000      # refuse queries scanning more (dry-run)
    limits:
      default_limit: 1000
      max_limit: 10000
    lint:
      security_allowlist: []
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..compiler import (
    CompileConfig,
    compile_dimension_values,
    compile_semantic_query,
    execute_semantic_query,
)
from ..connectors import from_config
from ..linter import LintConfig
from ..models.ast import SemanticView
from ..models.errors import ERR_UNKNOWN_VIEW, CompilerError
from ..models.query import FILTER_OPS
from ..parsers import load_view, load_views

INSTRUCTIONS = f"""\
Query governed business metrics without writing SQL.
1. list_semantic_views, then inspect_semantic_view for its metrics and dimensions.
2. query with execute_semantic_query: metric names, dimension names (time dimensions
   take a grain suffix, e.g. order_date__month) and structured filters
   [{{"field": ..., "op": ..., "value": ...}}]. Ops: {", ".join(FILTER_OPS)}.
3. compile_semantic_query shows the SQL and scan estimate without running it.
4. get_dimension_values resolves exact categorical values (never dump raw rows).
In answers that mix metrics from different fact tables, a NULL metric cell means
that fact had no rows for that group (no activity), not an error.
Every refusal names an error code and how to fix the request — repair and retry.
"""


class _State:
    def __init__(self) -> None:
        path = Path(os.environ.get("STRICT_SEMANTIC_CONFIG", "strict_semantic.yml")).resolve()
        cfg = yaml.safe_load(path.read_text()) or {}
        self.base = path.parent
        self.views_path = self.base / cfg.get("views", "views")
        self.connector = from_config(cfg.get("connector") or {"type": "duckdb"}, self.base)
        limits = cfg.get("limits") or {}
        self.compile_config = CompileConfig(
            default_limit=int(limits.get("default_limit", 1000)),
            max_limit=int(limits.get("max_limit", 10000)),
            allow_full_scan=bool(limits.get("allow_full_scan", False)),
        )
        lint_cfg = cfg.get("lint") or {}
        self.lint_config = LintConfig(
            security_allowlist=tuple(lint_cfg.get("security_allowlist") or ()),
            no_pk_tables=tuple(lint_cfg.get("no_pk_tables") or ()),
            no_primary_date_tables=tuple(lint_cfg.get("no_primary_date_tables") or ()),
        )
        self._views_cache: dict[str, SemanticView] | None = None
        self._views_key: tuple | None = None

    def _views_fingerprint(self) -> tuple:
        # mirrors load_views' glob so any add/remove/edit of a spec invalidates
        p = self.views_path
        files = (
            [f for f in sorted(p.iterdir()) if f.suffix.lower() in (".sql", ".yml", ".yaml")]
            if p.is_dir() else [p]
        )
        return tuple((str(f), (st := f.stat()).st_mtime_ns, st.st_size) for f in files)

    def views(self) -> dict[str, SemanticView]:
        # spec edits still apply without a restart: files are stat-ed per call,
        # but only re-parsed when something actually changed
        key = self._views_fingerprint()
        if self._views_cache is None or key != self._views_key:
            if self.views_path.is_dir():
                self._views_cache = load_views(self.views_path)
            else:
                v = load_view(self.views_path)
                self._views_cache = {v.name: v}
            self._views_key = key
        return self._views_cache

    def view(self, name: str) -> SemanticView:
        views = self.views()
        if name not in views:
            raise CompilerError(
                ERR_UNKNOWN_VIEW, f"unknown view '{name}'; available: {', '.join(sorted(views)) or 'none'}"
            )
        return views[name]


_STATE: _State | None = None


def _state() -> _State:
    global _STATE
    if _STATE is None:
        _STATE = _State()
    return _STATE


@contextmanager
def _caller_errors():
    """Anticipated failures must be ToolError, or the model sees only a generic line."""
    try:
        yield
    except CompilerError as e:
        detail = f"[{e.code}] {e}" + (f" | hint: {e.hint}" if e.hint else "")
        raise ToolError(detail) from None
    except (OSError, ValueError, KeyError) as e:
        raise ToolError(f"configuration error: {e}") from None
    except Exception as e:  # warehouse/driver errors are the agent's to read and adapt to
        raise ToolError(f"warehouse error: {e}") from None


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


server = MCPServer(name="strict-semantic", instructions=INSTRUCTIONS)


@server.tool()
def list_semantic_views() -> list[dict]:
    """List the semantic views that can be queried, with their routing guidance."""
    with _caller_errors():
        return [
            {"name": v.name, "comment": v.comment, "ai_sql_generation": v.ai_sql_generation}
            for v in _state().views().values()
        ]


@server.tool()
def inspect_semantic_view(view: str) -> dict:
    """Metrics, dimensions (grains, declared values, synonyms) and required filters of one view."""
    with _caller_errors():
        st = _state()
        v = st.view(view)
        required = sorted({t.partition.dimension for t in v.tables.values() if t.partition})
        return {
            "name": v.name,
            "comment": v.comment,
            "ai_sql_generation": v.ai_sql_generation,
            "tables": [
                {
                    "name": t.name,
                    "grain": t.comment.split(".")[0] + "." if t.comment else "",
                    "primary_date": t.primary_date,
                    "snapshot": t.snapshot,
                    "partition_dimension": t.partition.dimension if t.partition else None,
                }
                for t in v.tables.values()
            ],
            "metrics": [
                {
                    "name": m.name,
                    "comment": m.comment,
                    "derived": m.is_derived,
                    "synonyms": list(m.synonyms),
                    "non_additive_by": m.non_additive_by,
                }
                for m in v.metrics
            ],
            "dimensions": [
                {
                    "name": d.name,
                    "comment": d.comment,
                    "time": d.is_time,
                    "grains": list(d.grains),
                    "values": list(d.values),
                    "synonyms": list(d.synonyms),
                }
                for d in v.dimensions
            ],
            "required_filters": required,
            "filter_ops": list(FILTER_OPS),
            "limits": {"default_limit": st.compile_config.default_limit, "max_limit": st.compile_config.max_limit},
        }


@server.tool()
def get_dimension_values(
    view: str,
    dimension: str,
    search: str | None = None,
    filters: list[dict] | None = None,
    limit: int = 50,
) -> dict:
    """Resolve exact values of one dimension (bounded SELECT DISTINCT ... LIMIT <=50).

    filters: [{"field": ..., "op": ..., "value": ...}] — include the required date
    filter when the dimension's table is partitioned.
    """
    with _caller_errors():
        st = _state()
        c = compile_dimension_values(
            st.view(view), dimension, search=search, filters=filters or [],
            limit=limit, dialect=st.connector.dialect, config=st.compile_config,
            lint_config=st.lint_config,
        )
        rows = st.connector.execute(c.sql)
        return {"values": [_jsonable(r["value"]) for r in rows], "sql": c.sql}


@server.tool()
def compile_semantic_query(  # noqa: F811 - tool name mirrors the library call deliberately
    view: str,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Compile a request to deterministic SQL and estimate its scan, without running it.

    order_by: output column names, "-name" for descending.
    """
    from .. import compiler

    with _caller_errors():
        st = _state()
        c = compiler.compile_semantic_query(
            st.view(view),
            {"metrics": metrics or [], "dimensions": dimensions or [], "filters": filters or [],
             "order_by": order_by or [], "limit": limit},
            dialect=st.connector.dialect, config=st.compile_config, lint_config=st.lint_config,
        )
        return {
            "sql": c.sql,
            "columns": c.columns,
            "tables": c.tables,
            "join_paths": c.join_paths,
            "estimated_bytes": st.connector.estimate_bytes(c.sql),
        }


@server.tool()
def execute_semantic_query(  # noqa: F811
    view: str,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Compile and run a metrics query; returns rows plus the executed SQL."""
    from .. import compiler

    with _caller_errors():
        st = _state()
        result = compiler.execute_semantic_query(
            st.view(view),
            {"metrics": metrics or [], "dimensions": dimensions or [], "filters": filters or [],
             "order_by": order_by or [], "limit": limit},
            st.connector, config=st.compile_config, lint_config=st.lint_config,
        )
        rows = [{k: _jsonable(x) for k, x in r.items()} for r in result.rows]
        return {"columns": result.columns, "rows": rows, "truncated": result.truncated, "sql": result.sql}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
