"""v0.5.1: regression tests for the eight findings of the external code audit.

F1/F4 filter-chain expansion + reachability in dimension-value lookups,
F2 Decimal precision over MCP, F3 cross-schema scaffold identity, F5 exact
``truncated``, F6 mixed-timezone IN refusal, F7 ``__slots__`` plugins,
F8 ``max_rows=0``.
"""

from decimal import Decimal

import pytest
from conftest import JAN

from ostrict.compiler import CompileConfig, compile_semantic_query, execute_semantic_query
from ostrict.compiler.sql_builder import build_dimension_values_query
from ostrict.connectors.duckdb import DuckDBConnector
from ostrict.models.errors import CompilerError
from ostrict.parsers import parse_yaml_view
from ostrict.plugins.base import CompilerPlugin
from ostrict.scaffold.db_reader import read_schema

# --------------------------------------------------------------- F1/F4 fixtures

FILTER_CHAIN = """
name: audit_chain
dialect: duckdb
comment: Three-table always-on filter chain.
tables:
  orders:
    source: shop.orders
    primary_key: [order_id]
    comment: 'One row per order. Source: shop.orders.'
    filter: "customers.is_active = 1"
  customers:
    source: shop.customers
    primary_key: [customer_id]
    comment: 'One row per customer. Source: shop.customers.'
    filter: "segments.deleted = 0"
  segments:
    source: shop.segments
    primary_key: [segment_id]
    comment: 'One row per segment. Source: shop.segments.'
    filter: "visible = 1"
relationships:
  - name: orders_customers
    from: orders (customer_id)
    to: customers (customer_id)
  - name: customers_segments
    from: customers (segment_id)
    to: segments (segment_id)
dimensions:
  orders.channel:
    expr: channel
    comment: 'Sales channel. Values: web, store.'
metrics:
  orders.order_count:
    expr: COUNT(order_id)
    comment: 'Number of orders. Population: all orders. Additivity: additive.'
"""

UNREACHABLE_FILTER = """
name: audit_unreachable
dialect: duckdb
comment: A table filter that reads a non-ancestor.
tables:
  orders:
    source: shop.orders
    primary_key: [order_id]
    comment: 'One row per order. Source: shop.orders.'
    filter: "events.ok = 1"
  events:
    source: shop.events
    primary_key: [event_id]
    comment: 'One row per event. Source: shop.events.'
relationships:
  - name: events_orders
    from: events (order_id)
    to: orders (order_id)
dimensions:
  orders.channel:
    expr: channel
    comment: 'Sales channel. Values: web, store.'
metrics:
  orders.order_count:
    expr: COUNT(order_id)
    comment: 'Number of orders. Population: all orders. Additivity: additive.'
"""


def test_dimension_values_follow_the_filter_chain():
    """F1: a filter pulling in a table applies that table's own filter too."""
    view = parse_yaml_view(FILTER_CHAIN)
    c = build_dimension_values_query(view, "channel", None, [], 50, "duckdb", CompileConfig())
    assert "customers.is_active = 1" in c.sql
    assert "segments.deleted = 0" in c.sql  # previously dropped: snapshot loop
    assert "segments.visible = 1" in c.sql  # previously dropped: chain end
    assert c.tables == ["customers", "orders", "segments"]


def test_dimension_values_guard_unreachable_filter_dependency():
    """F4: the lookup path enforces the same reachability rule as aggregates."""
    view = parse_yaml_view(UNREACHABLE_FILTER)
    with pytest.raises(CompilerError) as e:  # was an unhandled KeyError before
        build_dimension_values_query(view, "channel", None, [], 50, "duckdb", CompileConfig())
    assert e.value.code == "ERR_FANOUT_JOIN_FORBIDDEN"


def test_aggregate_and_lookup_share_filter_expansion(view, connector):
    """The extraction must not change aggregate SQL for the stock fixture."""
    sql = compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb").sql
    assert connector.execute(sql)[0]["revenue"] == Decimal("94.00")


# ------------------------------------------------------------------------- F2


def test_decimal_precision_preserved_over_mcp():
    server = pytest.importorskip("ostrict.mcp_server.server")
    big = Decimal("12345678901234567890.123456789")
    out = server._jsonable(big)
    assert out == "12345678901234567890.123456789"
    assert Decimal(out) == big  # lossless round-trip; float() would round
    assert server._jsonable(Decimal("0.1")) == "0.1"


# ------------------------------------------------------------------------- F3


def test_cross_schema_same_table_names_stay_separate():
    con = DuckDBConnector()
    con.con.execute(
        "CREATE SCHEMA s1; CREATE SCHEMA s2;"
        "CREATE TABLE s1.orders(order_id INTEGER, amount DECIMAL(10,2));"
        "CREATE TABLE s2.orders(order_id INTEGER, status VARCHAR);"
    )
    drafts = {d.name: d for d in read_schema(con, ["s1.orders", "s2.orders"])}
    assert set(drafts) == {"s1_orders", "s2_orders"}
    assert drafts["s1_orders"].source == "s1.orders"
    assert drafts["s2_orders"].source == "s2.orders"
    assert [c.name for c in drafts["s1_orders"].columns] == ["order_id", "amount"]
    assert [c.name for c in drafts["s2_orders"].columns] == ["order_id", "status"]
    assert any("renamed from 'orders'" in n for n in drafts["s1_orders"].notes)


def test_unique_table_names_keep_their_bare_name():
    con = DuckDBConnector()
    con.con.execute("CREATE SCHEMA s1; CREATE TABLE s1.orders(order_id INTEGER)")
    [d] = read_schema(con, ["s1.orders"])
    assert d.name == "orders" and d.source == "s1.orders"


def test_missing_tables_checked_by_full_identity():
    con = DuckDBConnector()
    con.con.execute("CREATE SCHEMA s1; CREATE TABLE s1.orders(order_id INTEGER)")
    with pytest.raises(CompilerError) as e:
        read_schema(con, ["s1.orders", "s2.orders"])  # bare-name check would miss this
    assert "s2.orders" in str(e.value)


# ------------------------------------------------------------------------- F5


def test_truncated_is_a_fact_not_a_guess(view, connector):
    req = {"metrics": ["revenue"], "dimensions": ["region"], "filters": JAN, "order_by": ["-revenue"]}
    full = execute_semantic_query(view, {**req, "limit": 3}, connector)  # exactly 3 region rows exist
    assert len(full.rows) == 3 and full.truncated is False
    assert full.sql.rstrip().endswith("LIMIT 3")  # reported SQL keeps the requested limit
    cut = execute_semantic_query(view, {**req, "limit": 2}, connector)
    assert len(cut.rows) == 2 and cut.truncated is True


def test_truncation_probe_respects_the_limit_cap(view, connector):
    req = {"metrics": ["revenue"], "dimensions": ["region"], "filters": JAN, "limit": 3}
    r = execute_semantic_query(view, req, connector, config=CompileConfig(max_limit=3))
    assert len(r.rows) == 3 and r.truncated is False  # the +1 probe must not trip the cap


# ------------------------------------------------------------------------- F6


def test_mixed_timezone_in_filter_is_refused(view):
    mixed = [{"field": "order_date", "op": "in", "value": ["2026-01-01", "2026-01-02T00:00:00+00:00"]}]
    with pytest.raises(CompilerError) as e:  # was an unhandled TypeError before
        compile_semantic_query(view, {"metrics": ["revenue"], "filters": mixed}, dialect="duckdb")
    assert e.value.code == "ERR_INVALID_FILTER"

    consistent = [
        {"field": "order_date", "op": "in",
         "value": ["2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"]}
    ]
    compile_semantic_query(view, {"metrics": ["revenue"], "filters": consistent}, dialect="duckdb")


# ------------------------------------------------------------------------- F7


def test_slots_plugin_memoizes_outside_the_instance(view):
    fired = []

    class Slotted:  # duck-typed plugin without __dict__ or __weakref__
        __slots__ = ()

        def on_model_loaded(self, v):
            fired.append(id(v))

        def before_compile(self, v, request):
            return request

        def after_compile(self, v, request, compiled):
            return compiled

        def on_query_executed(self, v, request, compiled, result):
            return None

    p = Slotted()
    for _ in range(2):
        compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb", plugins=[p])
    assert fired == [id(view)]  # fired once, and no AttributeError on __dict__


def test_regular_plugin_still_fires_once_per_view_object(view):
    fired = []

    class P(CompilerPlugin):
        def on_model_loaded(self, v):
            fired.append(1)

    p = P()
    for _ in range(2):
        compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb", plugins=[p])
    assert fired == [1]


# ------------------------------------------------------------------------- F8


def test_duckdb_max_rows_edges(connector):
    assert connector.execute("SELECT 1 AS x", max_rows=0) == []  # 0 means zero, not unlimited
    assert connector.execute("SELECT 1 AS x", max_rows=1) == [{"x": 1}]
    assert connector.execute("SELECT 1 AS x") == [{"x": 1}]
    with pytest.raises(ValueError):
        connector.execute("SELECT 1 AS x", max_rows=-1)
