"""Live BigQuery stress tests against bigquery-public-data.thelook_ecommerce.

Opt-in: set GCP_PROJECT to a project you can bill (Google's free sandbox tier
is enough — the estimates are dry-runs, and the few real queries scan at most
a few hundred MB of public data). Without GCP_PROJECT this whole module skips,
so the standard suite stays offline and fast:

    GCP_PROJECT=<your-project> pytest -m integration -v -s

Two deviations from naive expectations are design features, not gaps:

- Combining metrics whose partitioned tables prune on *different* date
  dimensions (orders vs events) in one request is refused — every filter must
  be reachable from every requested metric's base table (REVISIONS item 5).
  The working drill-across below pairs orders with order_items, both pruned
  by the one `order_date` dimension; the events family runs on `event_date`.
- Grouping `order_count` by `products.category` is refused the same way:
  products is not an ancestor of orders, and guessing a join would fan out.
"""

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("GCP_PROJECT"),
        reason="set GCP_PROJECT to run live BigQuery integration tests",
    ),
]
pytest.importorskip("google.cloud.bigquery")

from pathlib import Path  # noqa: E402

from ostrict.compiler import CompileConfig, compile_semantic_query, execute_semantic_query  # noqa: E402
from ostrict.connectors.bigquery import BigQueryConnector  # noqa: E402
from ostrict.linter import LintConfig  # noqa: E402
from ostrict.models.errors import ERR_COST_LIMIT, ERR_FANOUT_JOIN_FORBIDDEN, CompilerError  # noqa: E402
from ostrict.parsers import load_view  # noqa: E402

VIEW = load_view(Path(__file__).parent.parent.parent / "examples" / "thelook_ecommerce.yaml")
LINT = LintConfig(no_primary_date_tables=("products",))
ITEMS = "bigquery-public-data.thelook_ecommerce.order_items"
JUNE_DAY = [
    {"field": "order_date", "op": ">=", "value": "2023-06-01"},
    {"field": "order_date", "op": "<", "value": "2023-06-02"},
]
JUNE_WEEK = [
    {"field": "order_date", "op": ">=", "value": "2023-06-01"},
    {"field": "order_date", "op": "<", "value": "2023-06-08"},
]


@pytest.fixture(scope="module")
def conn():
    return BigQueryConnector(project=os.environ["GCP_PROJECT"])


def test_partition_pruning_dry_run(conn):
    bounded = compile_semantic_query(
        VIEW,
        {"metrics": ["total_revenue"], "dimensions": ["country"], "filters": JUNE_DAY},
        dialect="bigquery",
        lint_config=LINT,
    )
    assert ITEMS in bounded.sql and "created_at" in bounded.sql  # pruning sits inside the subquery
    est_bounded = conn.estimate_bytes(bounded.sql)
    full = compile_semantic_query(
        VIEW,
        {"metrics": ["total_revenue"], "dimensions": ["country"]},
        dialect="bigquery",
        config=CompileConfig(allow_full_scan=True),
        lint_config=LINT,
    )
    est_full = conn.estimate_bytes(full.sql)
    part = conn.client.get_table(ITEMS).time_partitioning
    print(
        f"\n1-day bounded: {est_bounded:,} bytes | unbounded full scan: {est_full:,} bytes"
        f" | order_items partitioned on: {part.field if part else None}"
    )
    assert est_bounded and est_full and 0 < est_bounded <= est_full
    if part:  # bytes only shrink when BigQuery has physical partitions to prune
        assert est_bounded < est_full
    else:
        pytest.xfail("thelook order_items is not date-partitioned: dry-run bytes cannot shrink")


def test_byte_ceiling_refuses_before_billing():
    capped = BigQueryConnector(project=os.environ["GCP_PROJECT"], max_bytes=1024)  # 1 KB
    with pytest.raises(CompilerError) as e:
        execute_semantic_query(
            VIEW,
            {"metrics": ["total_revenue"], "dimensions": ["country"], "filters": JUNE_DAY},
            capped,
            lint_config=LINT,
        )
    assert e.value.code == ERR_COST_LIMIT  # aborted at dry-run: $0 billed
    print(f"\nrefused as designed: {e.value}")


def test_sibling_fanout_refused():
    # events.traffic_source is a sibling branch under users, never an ancestor of orders
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(
            VIEW,
            {"metrics": ["order_count"], "dimensions": ["traffic_source"], "filters": JUNE_DAY},
            dialect="bigquery",
            lint_config=LINT,
        )
    assert e.value.code == ERR_FANOUT_JOIN_FORBIDDEN
    # products.category is no ancestor of orders either — same refusal, zero queries run
    with pytest.raises(CompilerError) as e2:
        compile_semantic_query(
            VIEW,
            {"metrics": ["order_count"], "dimensions": ["category"], "filters": JUNE_DAY},
            dialect="bigquery",
            lint_config=LINT,
        )
    assert e2.value.code == ERR_FANOUT_JOIN_FORBIDDEN


def test_drill_across_on_bigquery(conn):
    # (a) cross-family date filters are refused by design (REVISIONS item 5)
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(
            VIEW,
            {"metrics": ["order_count", "event_count"], "dimensions": ["country"], "filters": JUNE_WEEK},
            dialect="bigquery",
            lint_config=LINT,
        )
    assert e.value.code == ERR_FANOUT_JOIN_FORBIDDEN

    # (b) two fact grains, one date dimension: UNION ALL + GROUP BY + MAX runs clean on BigQuery
    result = execute_semantic_query(
        VIEW,
        {"metrics": ["order_count", "total_revenue"], "dimensions": ["country"],
         "filters": JUNE_WEEK, "order_by": ["-order_count"], "limit": 10},
        conn,
        lint_config=LINT,
    )
    assert "UNION ALL" in result.sql
    assert result.columns == ["country", "order_count", "total_revenue"]
    assert result.rows, "expected at least one country row for June 2023"
    print(f"\ndrill-across rows ({len(result.rows)}):")
    for r in result.rows[:5]:
        print("  ", r)

    # (c) the events family executes on its own date dimension
    ev = execute_semantic_query(
        VIEW,
        {"metrics": ["event_count"], "dimensions": ["traffic_source"],
         "filters": [
             {"field": "event_date", "op": ">=", "value": "2023-06-01"},
             {"field": "event_date", "op": "<", "value": "2023-06-02"},
         ],
         "order_by": ["-event_count"]},
        conn,
        lint_config=LINT,
    )
    assert ev.columns == ["traffic_source", "event_count"] and ev.rows
    print("events by traffic_source:")
    for r in ev.rows[:5]:
        print("  ", r)


def test_derived_metric_and_ordering(conn):
    result = execute_semantic_query(
        VIEW,
        {"metrics": ["total_revenue", "order_count", "revenue_per_order"],
         "dimensions": ["country"], "filters": JUNE_WEEK,
         "order_by": ["-total_revenue"], "limit": 5},
        conn,
        lint_config=LINT,
    )
    assert len(result.rows) == 5
    revs = [float(r["total_revenue"]) for r in result.rows]
    assert revs == sorted(revs, reverse=True)
    print("\ntop countries by revenue (June 2023 week):")
    for r in result.rows:
        print("  ", r)
        if r["order_count"]:
            assert abs(float(r["revenue_per_order"]) - float(r["total_revenue"]) / r["order_count"]) < 1e-6
