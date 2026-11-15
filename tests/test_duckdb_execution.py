"""Milestone 3: compiled SQL executes correctly on DuckDB.

The seed is built to catch the classic failures: an order with several lines
(fan-out bait), a cancelled order, an orphan order whose customer is missing,
metrics of different grains, and a true point-in-time snapshot table.
"""

import pytest
from conftest import JAN, normalize

from strict_semantic.compiler import (
    CompileConfig,
    compile_dimension_values,
    compile_semantic_query,
    execute_semantic_query,
)
from strict_semantic.models.errors import CompilerError
from strict_semantic.models.query import Filter, SemanticQueryRequest
from strict_semantic.plugins.base import CompilerPlugin


def run(view, connector, **req):
    compiled = compile_semantic_query(view, req, dialect="duckdb")
    return normalize(connector.execute(compiled.sql))


def by(rows, key):
    return {r[key]: r for r in rows}


def test_january_totals(view, connector):
    [r] = run(view, connector, metrics=["revenue", "units_sold", "order_count", "buyer_count"], filters=JAN)
    assert r == {"revenue": 94.0, "units_sold": 12, "order_count": 5, "buyer_count": 4}


def test_drill_across_by_region_handles_nulls(view, connector):
    rows = by(run(view, connector, metrics=["revenue", "order_count", "buyer_count"],
                  dimensions=["region"], filters=JAN), "region")
    assert rows["EU"] == {"region": "EU", "revenue": 70.0, "order_count": 2, "buyer_count": 2}
    assert rows["US"] == {"region": "US", "revenue": 17.0, "order_count": 2, "buyer_count": 1}
    assert rows[None] == {"region": None, "revenue": 7.0, "order_count": 1, "buyer_count": 1}


def test_derived_ratio_metric(view, connector):
    rows = by(run(view, connector, metrics=["aov"], dimensions=["region"], filters=JAN), "region")
    assert rows["EU"]["aov"] == 35.0 and rows["US"]["aov"] == 8.5 and rows[None]["aov"] == 7.0


def test_time_grain_and_dimension_filter(view, connector):
    rows = run(view, connector, metrics=["revenue"], dimensions=["order_date__month"],
               filters=[{"field": "order_date", "op": "between", "value": ["2026-01-01", "2026-02-28"]}],
               order_by=["order_date__month"])
    assert [(str(r["order_date__month"])[:7], r["revenue"]) for r in rows] == [("2026-01", 94.0), ("2026-02", 100.0)]

    [r] = run(view, connector, metrics=["revenue"],
              filters=[*JAN, {"field": "order_status", "op": "=", "value": "completed"},
                       {"field": "region", "op": "=", "value": "US"}])
    assert r["revenue"] == 8.0


def test_synonym_and_metric_filter_and_order(view, connector):
    rows = run(view, connector, metrics=["revenue", "order_count"], dimensions=["region"],
               filters=[*JAN, {"field": "market", "op": "is not null"},
                        {"field": "revenue", "op": ">", "value": 10}],
               order_by=["-revenue"], limit=5)
    assert [(r["region"], r["revenue"]) for r in rows] == [("EU", 70.0), ("US", 17.0)]


def test_customer_metric_needs_no_date_filter(view, connector):
    rows = by(run(view, connector, metrics=["customer_count"], dimensions=["region"]), "region")
    assert {k: v["customer_count"] for k, v in rows.items()} == {"EU": 2, "US": 1, "APAC": 1}


def test_golden_fanout_rejected(view):
    """Golden test 4 (SV-R3.4): a customers-grain metric sliced by a child dimension."""
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"metrics": ["customer_count"], "dimensions": ["channel"]}, dialect="duckdb")
    assert e.value.code == "ERR_FANOUT_JOIN_FORBIDDEN"


def test_unreachable_filter_rejected(view):
    """Strict reading of SV-R3.4: filters must be reachable from EVERY metric's base."""
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(
            view, {"metrics": ["customer_count", "revenue"], "dimensions": ["region"], "filters": JAN},
            dialect="duckdb",
        )
    assert e.value.code == "ERR_FANOUT_JOIN_FORBIDDEN"


def test_snapshot_rules(view, connector):
    pinned = [{"field": "snapshot_date", "op": "=", "value": "2026-01-02"}]
    [r] = run(view, connector, metrics=["stock_units"], filters=pinned)
    assert r["stock_units"] == 70

    rows = by(run(view, connector, metrics=["stock_units"], dimensions=["category"], filters=pinned), "category")
    assert {k: v["stock_units"] for k, v in rows.items()} == {"electronics": 30, "books": 40}

    rows = run(view, connector, metrics=["stock_units"], dimensions=["snapshot_date"],
               filters=[{"field": "snapshot_date", "op": "between", "value": ["2026-01-01", "2026-01-02"]}],
               order_by=["snapshot_date"])
    assert [r["stock_units"] for r in rows] == [30, 70]

    with pytest.raises(CompilerError) as e:
        compile_semantic_query(
            view,
            {"metrics": ["stock_units"],
             "filters": [{"field": "snapshot_date", "op": "between", "value": ["2026-01-01", "2026-01-02"]}]},
            dialect="duckdb",
        )
    assert e.value.code == "ERR_SNAPSHOT_AGGREGATION_VIOLATION"


def test_partition_guardrails(view):
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"metrics": ["revenue"]}, dialect="duckdb")
    assert e.value.code == "ERR_MISSING_PARTITION_FILTER"

    # an upper bound alone does not bound the scan
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(
            view, {"metrics": ["revenue"], "filters": [{"field": "order_date", "op": "<", "value": "2026-02-01"}]},
            dialect="duckdb",
        )
    assert e.value.code == "ERR_MISSING_PARTITION_FILTER"

    cfg = CompileConfig(allow_full_scan=True)
    sql = compile_semantic_query(view, {"metrics": ["revenue"]}, dialect="duckdb", config=cfg).sql
    assert "WHERE" not in sql.split("GROUP BY")[0].split("FROM")[0]  # no filters invented


def test_partition_bounds_are_pushed_into_the_table_subquery(view):
    sql = compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="bigquery").sql
    assert "FROM shop.order_items AS order_items" in sql
    assert "order_items.order_date >= '2026-01-01'" in sql
    assert "order_items.order_date <= '2026-02-01'" in sql


def test_limit_injection_and_cap(view, connector):
    sql = compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb").sql
    assert sql.rstrip().endswith("LIMIT 1000")
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN, "limit": 999999}, dialect="duckdb")
    assert e.value.code == "ERR_EXCESSIVE_LIMIT"


def test_metric_required(view):
    """Golden test 10 (first half, SV-R10.7): no raw-row dumps."""
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"dimensions": ["region"]}, dialect="duckdb")
    assert e.value.code == "ERR_METRIC_REQUIRED"


def test_get_dimension_values(view, connector):
    c = compile_dimension_values(view, "channel", filters=[Filter(**JAN[0]), Filter(**JAN[1])], dialect="duckdb")
    assert [r["value"] for r in connector.execute(c.sql)] == ["store", "web"]

    c = compile_dimension_values(view, "region", search="e", dialect="duckdb")
    assert [r["value"] for r in connector.execute(c.sql)] == ["EU"]


def test_plugin_hooks(view, connector):
    calls = []

    class Tenant(CompilerPlugin):
        def before_compile(self, v, request):
            request.filters.append(Filter(field="region", op="=", value="EU"))
            return request

        def after_compile(self, v, request, compiled):
            calls.append("after")
            return compiled

        def on_query_executed(self, v, request, compiled, result):
            calls.append(("executed", len(result.rows)))

    req = SemanticQueryRequest(metrics=["revenue"], filters=[Filter(**f) for f in JAN])
    result = execute_semantic_query(view, req, connector, plugins=[Tenant()])
    assert normalize(result.rows) == [{"revenue": 70.0}]
    assert calls == ["after", ("executed", 1)]
