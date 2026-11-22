"""The blueprint's ten mandatory golden tests (section 9), under their exact names.

Tests 1, 2 and 4 are also covered in the milestone suites; here each blueprint
name asserts its case explicitly so the acceptance list is auditable in one file.
"""

import random
from pathlib import Path

import duckdb
import pytest
from conftest import FIXTURES, JAN, normalize

from ostrict.compiler import compile_semantic_query
from ostrict.connectors.duckdb import DuckDBConnector
from ostrict.eval.runner import run_eval
from ostrict.linter import lint
from ostrict.models.errors import CompilerError
from ostrict.parsers import load_view
from ostrict.parsers.common import build_view


def codes(raw):
    return {f.code for f in lint(build_view(raw)).errors}


def T(name, source, comment, key=("id",)):
    return {"source": source, "primary_key": list(key), "comment": comment}


def test_dual_input_equivalence():
    """1. Snowflake DDL and the YAML spec produce identical ASTs and identical SQL."""
    a, b = load_view(FIXTURES / "ecommerce.sql"), load_view(FIXTURES / "ecommerce.yaml")
    assert a == b
    req = {"metrics": ["revenue"], "dimensions": ["region"], "filters": JAN}
    assert (
        compile_semantic_query(a, req, dialect="bigquery").sql
        == compile_semantic_query(b, req, dialect="bigquery").sql
    )


def test_100x_determinism(view):
    """2. 100 consecutive compilations are byte-for-byte identical."""
    req = {"metrics": ["revenue", "aov"], "dimensions": ["region", "order_date__month"], "filters": JAN}
    results = {compile_semantic_query(view, req, dialect="bigquery").sql for _ in range(100)}
    assert len(results) == 1


def test_zero_fanout_accuracy(view):
    """3. 100 random orders, 20 customers: semantic totals equal raw-SQL ground truth,
    while a naive flat join provably inflates the order count."""
    rng = random.Random(42)
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE SCHEMA shop;
        CREATE TABLE shop.customers (customer_id INT, region TEXT, is_vip BOOLEAN, signup_date DATE);
        CREATE TABLE shop.orders (order_id INT, customer_id INT, order_date DATE, channel TEXT, status TEXT);
        CREATE TABLE shop.order_items
          (order_id INT, line_no INT, product_id INT, quantity INT, unit_price DECIMAL(10,2), order_date DATE);
        CREATE TABLE shop.products (product_id INT, category TEXT, created_at DATE);
        CREATE TABLE shop.daily_inventory_snapshot (snapshot_date DATE, product_id INT, units_on_hand INT);"""
    )
    regions = ["EU", "US", "APAC"]
    con.executemany(
        "INSERT INTO shop.customers VALUES (?, ?, ?, ?)",
        [(i, regions[i % 3], False, "2025-12-01") for i in range(1, 21)],
    )
    con.executemany(
        "INSERT INTO shop.products VALUES (?, ?, ?)",
        [(p, "electronics" if p % 2 else "books", "2025-10-01") for p in range(1, 6)],
    )
    orders, items = [], []
    for oid in range(1, 101):
        cid = 999 if oid % 25 == 0 else rng.randint(1, 20)  # a few orphans
        day = f"2026-01-{rng.randint(1, 28):02d}"
        orders.append((oid, cid, day, rng.choice(["web", "app", "store"]), "completed"))
        for line in range(1, rng.randint(1, 4) + 1):
            items.append((oid, line, rng.randint(1, 5), rng.randint(1, 5), rng.choice([5, 10, 25, 99.5]), day))
    con.executemany("INSERT INTO shop.orders VALUES (?, ?, ?, ?, ?)", orders)
    con.executemany("INSERT INTO shop.order_items VALUES (?, ?, ?, ?, ?, ?)", items)

    connector = DuckDBConnector(connection=con)
    compiled = compile_semantic_query(
        view, {"metrics": ["revenue", "order_count"], "dimensions": ["region"], "filters": JAN},
        dialect="duckdb",
    )
    got = {r["region"]: r for r in normalize(connector.execute(compiled.sql))}

    truth = {
        r[0]: (float(r[1]), None)
        for r in con.execute(
            """SELECT c.region, SUM(i.quantity * i.unit_price)
               FROM shop.order_items i
               JOIN shop.orders o ON i.order_id = o.order_id
               LEFT JOIN shop.customers c ON o.customer_id = c.customer_id
               WHERE o.order_date >= '2026-01-01' AND o.order_date < '2026-02-01'
               GROUP BY 1"""
        ).fetchall()
    }
    counts = {
        r[0]: r[1]
        for r in con.execute(
            """SELECT c.region, COUNT(o.order_id)
               FROM shop.orders o LEFT JOIN shop.customers c ON o.customer_id = c.customer_id
               WHERE o.order_date >= '2026-01-01' AND o.order_date < '2026-02-01' GROUP BY 1"""
        ).fetchall()
    }
    assert set(got) == set(truth)
    for region, (revenue, _) in truth.items():
        assert got[region]["revenue"] == pytest.approx(revenue)
        assert got[region]["order_count"] == counts[region]

    naive = con.execute(
        """SELECT COUNT(o.order_id) FROM shop.orders o JOIN shop.order_items i ON o.order_id = i.order_id
           WHERE o.order_date >= '2026-01-01' AND o.order_date < '2026-02-01'"""
    ).fetchone()[0]
    assert naive > sum(counts.values())  # the fan-out the compiler is built to prevent


def test_reject_downward_fanout_join(view):
    """4. SV-R3.4: a customers-grain metric sliced by an order_items-side dimension."""
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"metrics": ["customer_count"], "dimensions": ["category"]}, dialect="duckdb")
    assert e.value.code == "ERR_FANOUT_JOIN_FORBIDDEN"


def test_reject_ambiguous_multipath():
    """5. SV-R3.5: shipments reach regions directly AND via warehouses -> refused;
    modeling the warehouse's region as a role-playing alias table passes."""
    raw = {
        "name": "logistics",
        "comment": "Shipment routing used by the multipath golden test.",
        "tables": {
            "shipments": T("s", "l.shipments", "One row per shipment. Primary date: shipped_at. Source: l.shipments."),
            "warehouses": T("w", "l.warehouses", "One row per warehouse. Primary date: opened_on. Source: l.warehouses."),
            "regions": T("r", "l.regions", "One row per region. Primary date: created_on. Source: l.regions."),
        },
        "relationships": [
            {"name": "ship_region", "child": "shipments", "columns": ["region_id"], "parent": "regions"},
            {"name": "ship_wh", "child": "shipments", "columns": ["warehouse_id"], "parent": "warehouses"},
            {"name": "wh_region", "child": "warehouses", "columns": ["region_id"], "parent": "regions"},
        ],
        "metrics": [
            {"table": "shipments", "name": "shipment_count", "expr": "COUNT(id)",
             "comment": "Number of shipments made. Population: all shipments. Additivity: additive."}
        ],
    }
    assert "ERR_AMBIGUOUS_MULTI_PATH" in codes(raw)

    fixed = dict(raw)
    fixed["tables"] = dict(raw["tables"])
    fixed["tables"]["warehouse_regions"] = T(
        "wr", "l.regions", "One row per warehouse region. Primary date: created_on. Source: l.regions."
    )
    fixed["relationships"] = [
        raw["relationships"][0],
        raw["relationships"][1],
        {"name": "wh_region", "child": "warehouses", "columns": ["region_id"], "parent": "warehouse_regions"},
    ]
    assert "ERR_AMBIGUOUS_MULTI_PATH" not in codes(fixed)


def test_reject_partial_pk_join():
    """6. SV-R3.2: referencing one column of a two-column composite key."""
    raw = {
        "name": "p",
        "comment": "Partial key join fixture for the golden test.",
        "tables": {
            "lines": T("l", "s.lines", "One row per line. Primary date: d. Source: s.lines."),
            "orders": T("o", "s.orders", "One row per order per tenant. Primary date: d. Source: s.orders.",
                        key=("order_id", "tenant_id")),
        },
        "relationships": [{"name": "lo", "child": "lines", "columns": ["order_id"], "parent": "orders",
                           "parent_columns": ["order_id"]}],
        "metrics": [{"table": "lines", "name": "n", "expr": "COUNT(id)",
                     "comment": "Number of line rows counted. Population: all lines. Additivity: additive."}],
    }
    assert "ERR_PARTIAL_PK_JOIN" in codes(raw)


def test_reject_numeric_dimension_and_duplicate_names():
    """7. SV-R4.1 + SV-R6.4."""
    raw = {
        "name": "d",
        "comment": "Numeric dimension and duplicate name fixture.",
        "tables": {
            "a": T("a", "s.a", "One row per a. Primary date: d. Source: s.a."),
            "b": T("b", "s.b", "One row per b. Primary date: d. Source: s.b."),
        },
        "relationships": [{"name": "ab", "child": "a", "columns": ["b_id"], "parent": "b"}],
        "dimensions": [
            {"table": "a", "name": "quantity_value", "expr": "qty * 1",
             "comment": "Quantity exposed wrongly as a dimension."},
            {"table": "a", "name": "status", "expr": "status", "comment": "Lifecycle status of the a row. Values: x, y."},
            {"table": "b", "name": "status", "expr": "status", "comment": "Lifecycle status of the b row. Values: x, y."},
        ],
        "metrics": [{"table": "a", "name": "n", "expr": "COUNT(id)",
                     "comment": "Number of a rows counted. Population: all. Additivity: additive."}],
    }
    got = codes(raw)
    assert "ERR_NUMERIC_IN_DIMENSIONS" in got and "ERR_DUPLICATE_FIELD_NAME" in got


def test_reject_current_date_and_missing_non_additive():
    """8. SV-R5.3 + SV-R5.7."""
    raw = {
        "name": "s",
        "comment": "Snapshot and current-date fixture for the golden test.",
        "tables": {
            "inv": T("i", "s.inv",
                     "One row per sku per day. Point-in-time snapshot. Primary date: snapshot_date. Source: s.inv.",
                     key=("snapshot_date", "sku")),
        },
        "dimensions": [{"table": "inv", "name": "snapshot_date", "expr": "snapshot_date",
                        "comment": "Snapshot date of the inventory row. Grains: day."}],
        "metrics": [
            {"table": "inv", "name": "stock", "expr": "SUM(units)",
             "comment": "Units on hand at the snapshot date. Population: all skus. Additivity: non-additive."},
            {"table": "inv", "name": "fresh", "expr": "COUNT(CASE WHEN snapshot_date = CURRENT_DATE THEN sku END)",
             "comment": "Rows stamped today, which is non-deterministic. Population: all. Additivity: additive."},
        ],
    }
    got = codes(raw)
    assert "ERR_NON_DETERMINISTIC_TIME" in got and "ERR_MISSING_NON_ADDITIVE_BY" in got


def test_finops_partition_guardrail(view):
    """9. A partitioned fact table without a bounding date filter is refused."""
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"metrics": ["revenue"], "dimensions": ["region"]}, dialect="bigquery")
    assert e.value.code == "ERR_MISSING_PARTITION_FILTER"


def test_reject_raw_table_dump_and_sql_injection(view, connector):
    """10. SV-R10.7 + injection safety: values are escaped literals, names are validated."""
    with pytest.raises(CompilerError) as e:
        compile_semantic_query(view, {"metrics": [], "dimensions": ["region"]}, dialect="duckdb")
    assert e.value.code == "ERR_METRIC_REQUIRED"

    evil = "1=1'; DROP TABLE shop.orders--"
    compiled = compile_semantic_query(
        view, {"metrics": ["revenue"], "filters": [*JAN, {"field": "region", "op": "=", "value": evil}]},
        dialect="duckdb",
    )
    assert "DROP TABLE" in compiled.sql and "''" in compiled.sql  # inert, escaped literal
    [row] = connector.execute(compiled.sql)
    assert row["revenue"] is None
    assert connector.execute("SELECT COUNT(*) AS n FROM shop.orders")[0]["n"] == 6  # unharmed

    with pytest.raises(CompilerError) as e:
        compile_semantic_query(
            view, {"metrics": ["revenue"], "filters": [{"field": "region; DROP TABLE x", "op": "=", "value": 1}]},
            dialect="duckdb",
        )
    assert e.value.code == "ERR_UNKNOWN_FIELD"


def test_eval_runner_passes_the_golden_file():
    report = run_eval(Path(FIXTURES) / "golden_questions.yaml")
    assert report.ok, report.render()
