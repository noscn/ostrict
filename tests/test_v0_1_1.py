"""v0.1.1: `in` partition bounds, plugin hooks on value lookups and model load,
MCP spec cache keyed on file mtime, ascending ORDER BY on the BigQuery dialect."""

import os
import shutil

import pytest
import sqlglot
from conftest import FIXTURES, JAN

from ostrict.compiler import compile_dimension_values, compile_semantic_query
from ostrict.compiler.guardrails import _bounds_from
from ostrict.models.errors import CompilerError
from ostrict.models.query import Filter
from ostrict.parsers import load_view
from ostrict.plugins.base import CompilerPlugin


def test_in_filter_yields_min_max_bounds(view):
    d = next(x for x in view.dimensions if x.name == "order_date")
    f = Filter(field="order_date", op="in", value=["2026-01-05", "2026-01-01", "2026-01-03"])
    lowers, uppers = _bounds_from([(d, None, f)], "order_date")
    assert lowers == ["2026-01-01"]
    assert uppers == ["2026-01-05"]


def test_in_only_date_filter_satisfies_partition(view):
    req = {
        "metrics": ["revenue"],
        "filters": [{"field": "order_date", "op": "in", "value": ["2026-01-01", "2026-01-15"]}],
    }
    sql = compile_semantic_query(view, req, dialect="bigquery").sql
    assert "SELECT" in sql
    sqlglot.parse_one(sql, read="bigquery")


def test_non_date_in_filter_still_requires_partition(view):
    req = {"metrics": ["revenue"], "filters": [{"field": "channel", "op": "in", "value": ["web"]}]}
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, req, dialect="bigquery")
    assert e.value.code == "ERR_MISSING_PARTITION_FILTER"


class _Rbac(CompilerPlugin):
    def before_compile(self, view, request):
        request.filters.append(Filter(field="order_date", op="<", value="2026-02-01"))
        return request


def test_dimension_values_respect_before_compile(view):
    base = compile_dimension_values(view, "channel", filters=[JAN[0]], dialect="bigquery").sql
    gated = compile_dimension_values(
        view, "channel", filters=[JAN[0]], dialect="bigquery", plugins=[_Rbac()]
    ).sql
    assert "2026-02-01" not in base
    assert "2026-02-01" in gated


class _Counter(CompilerPlugin):
    def __init__(self):
        self.loaded = []

    def on_model_loaded(self, view):
        self.loaded.append(view.name)


def test_on_model_loaded_fires_once_per_model_object(view):
    p = _Counter()
    req = {"metrics": ["revenue"], "filters": JAN}
    compile_semantic_query(view, req, plugins=[p])
    compile_semantic_query(view, req, plugins=[p])
    assert p.loaded == ["ecommerce"]
    compile_dimension_values(view, "channel", filters=[JAN[0]], plugins=[p])  # same object
    assert p.loaded == ["ecommerce"]
    fresh = load_view(FIXTURES / "ecommerce.yaml")  # a reloaded spec is a new object
    compile_semantic_query(fresh, req, plugins=[p])
    assert p.loaded == ["ecommerce", "ecommerce"]


def test_order_by_ascending_parses_in_bigquery(view):
    req = {
        "metrics": ["revenue"],
        "dimensions": ["region"],
        "filters": JAN,
        "order_by": ["region", "-revenue"],
    }
    sql = compile_semantic_query(view, req, dialect="bigquery").sql
    assert "ORDER BY" in sql
    sqlglot.parse_one(sql, read="bigquery")


def test_mcp_state_reparses_specs_only_on_change(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    spec = views_dir / "ecommerce.yaml"
    shutil.copy(FIXTURES / "ecommerce.yaml", spec)
    (tmp_path / "ostrict.yml").write_text("views: ./views\nconnector:\n  type: duckdb\n")
    monkeypatch.delenv("OSTRICT_CONFIG", raising=False)
    monkeypatch.setenv("STRICT_SEMANTIC_CONFIG", str(tmp_path / "ostrict.yml"))
    from ostrict.mcp_server.server import _State

    st = _State()
    a = st.views()
    assert st.views() is a  # unchanged files: cached object returned
    os.utime(spec, (spec.stat().st_atime, spec.stat().st_mtime + 2))
    b = st.views()
    assert b is not a and "ecommerce" in b  # touched file: re-parsed
