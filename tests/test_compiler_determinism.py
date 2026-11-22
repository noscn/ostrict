"""Milestone 3: byte-for-byte deterministic compilation."""


import pytest
import sqlglot
from conftest import FIXTURES, JAN

from ostrict.compiler import compile_semantic_query
from ostrict.models.errors import CompilerError
from ostrict.parsers import load_view
from ostrict.parsers.common import build_view

REQUEST = {
    "metrics": ["revenue", "order_count", "aov"],
    "dimensions": ["region", "order_date__month"],
    "filters": [*JAN, {"field": "channel", "op": "in", "value": ["web", "app", "store"]}],
    "order_by": ["-revenue"],
    "limit": 100,
}


def test_100x_determinism(view):
    """Golden test 2: 100 consecutive compilations are byte-for-byte identical,
    even across fresh parses of the spec."""
    results = set()
    v = view
    for i in range(100):
        if i % 10 == 9:
            v = load_view(FIXTURES / "ecommerce.sql")
        results.add(compile_semantic_query(v, REQUEST, dialect="bigquery").sql)
    assert len(results) == 1


def test_dual_inputs_compile_identically(view):
    """Golden test 1 (second half): DDL and YAML specs emit identical SQL."""
    a = compile_semantic_query(view, REQUEST, dialect="bigquery").sql
    b = compile_semantic_query(load_view(FIXTURES / "ecommerce.yaml"), REQUEST, dialect="bigquery").sql
    assert a == b


def test_join_order_is_stable(view):
    c = compile_semantic_query(view, REQUEST, dialect="duckdb")
    assert c.join_paths == {"order_items": ["items_orders", "orders_customers"], "orders": ["orders_customers"]}
    assert c.tables == ["customers", "order_items", "orders"]
    assert c.columns == ["region", "order_date__month", "revenue", "order_count", "aov"]


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb", "snowflake", "postgres"])
def test_emitted_sql_parses_in_target_dialect(view, dialect):
    sql = compile_semantic_query(view, REQUEST, dialect=dialect).sql
    sqlglot.parse_one(sql, read=dialect)


def test_invalid_view_is_refused_at_compile_time():
    raw = {
        "name": "broken",
        "tables": {"t": {"source": "s.t", "primary_key": ["id"], "comment": "Orders rows."}},
        "metrics": [{"table": "t", "name": "n", "expr": "COUNT(id)",
                     "comment": "Row count metric for t. Population: all. Additivity: additive."}],
    }
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(build_view(raw), {"metrics": ["n"]}, dialect="duckdb")
    assert e.value.code in ("ERR_MISSING_GRAIN_OR_PK", "ERR_MISSING_PRIMARY_DATE")
