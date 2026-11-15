"""Milestone 2: every ground rule catches its bad definition with the exact error code."""

from pathlib import Path

import pytest

from strict_semantic.linter import LintConfig, ensure_valid, lint
from strict_semantic.models.errors import CompilerError, Severity
from strict_semantic.parsers import load_view
from strict_semantic.parsers.common import build_view

FIXTURES = Path(__file__).parent / "fixtures"


def V(**overrides):
    """A minimal valid two-table raw spec; tests override pieces to break one rule."""
    raw = {
        "name": "t",
        "dialect": "duckdb",
        "comment": "A small two-table test view used by the linter unit tests.",
        "tables": {
            "orders": {
                "source": "s.orders",
                "primary_key": ["id"],
                "comment": "One row per order. Primary date: order_date. Source: s.orders.",
            },
            "customers": {
                "source": "s.customers",
                "primary_key": ["id"],
                "comment": "One row per customer. Primary date: signup_date. Source: s.customers.",
            },
        },
        "relationships": [
            {"name": "orders_customers", "child": "orders", "columns": ["customer_id"], "parent": "customers"}
        ],
        "facts": [],
        "dimensions": [
            {
                "table": "customers",
                "name": "region",
                "expr": "region",
                "comment": "Customer home region for grouping. Values: EU, US.",
            },
            {
                "table": "orders",
                "name": "order_date",
                "expr": "order_date",
                "comment": "Date the order was placed. Grains: day, month.",
            },
        ],
        "metrics": [
            {
                "table": "orders",
                "name": "order_count",
                "expr": "COUNT(id)",
                "comment": "Number of orders placed. Population: all orders. Additivity: additive.",
            }
        ],
    }
    raw.update(overrides)
    return raw


def errors_of(raw, cfg=None):
    report = lint(build_view(raw), cfg)
    return {f.code for f in report.errors}


def warns_of(raw, cfg=None):
    report = lint(build_view(raw), cfg)
    return {f.code for f in report.warnings}


def test_fixture_is_lint_clean():
    report = lint(load_view(FIXTURES / "ecommerce.sql"))
    assert report.findings == [], report.render()


# ---- structure (SV-R1..R3) ---------------------------------------------------


def test_r1_disconnected_tables():
    raw = V()
    raw["tables"]["a"] = {"source": "s.a", "primary_key": ["id"], "comment": "One row per a. Primary date: d. Source: s.a."}
    raw["tables"]["b"] = {"source": "s.b", "primary_key": ["id"], "comment": "One row per b. Primary date: d. Source: s.b."}
    raw["relationships"].append({"name": "ab", "child": "a", "columns": ["b_id"], "parent": "b"})
    assert "ERR_DISCONNECTED_TABLES" in errors_of(raw)


def test_r2_orphan_table():
    raw = V()
    raw["tables"]["lonely"] = {
        "source": "s.lonely",
        "primary_key": ["id"],
        "comment": "One row per lonely thing. Primary date: d. Source: s.lonely.",
    }
    assert "ERR_ORPHAN_TABLE" in errors_of(raw)


def test_r2_missing_pk_and_grain():
    raw = V()
    raw["tables"]["orders"] = {"source": "s.orders",
                               "comment": "Orders data table. Primary date: order_date. Source: s.orders."}
    codes = errors_of(raw)
    assert "ERR_MISSING_GRAIN_OR_PK" in codes
    # the event-only exemption only waives the PK, never the grain comment
    cfg = LintConfig(no_pk_tables=("orders",))
    assert "ERR_MISSING_GRAIN_OR_PK" in errors_of(raw, cfg)


def test_r2_missing_primary_date():
    raw = V()
    raw["tables"]["orders"]["comment"] = "One row per order. Source: s.orders."
    assert "ERR_MISSING_PRIMARY_DATE" in errors_of(raw)


def test_r3_cycle_and_self_reference():
    raw = V()
    raw["relationships"].append({"name": "back", "child": "customers", "columns": ["last_order_id"], "parent": "orders"})
    assert "ERR_CYCLIC_RELATIONSHIP" in errors_of(raw)

    raw2 = V()
    raw2["relationships"] = [{"name": "self", "child": "orders", "columns": ["parent_id"], "parent": "orders"}]
    assert "ERR_CYCLIC_RELATIONSHIP" in errors_of(raw2)


def test_r3_partial_pk_join():
    raw = V()
    raw["tables"]["customers"]["primary_key"] = ["id", "tenant_id"]
    raw["relationships"][0]["parent_columns"] = ["id"]
    assert "ERR_PARTIAL_PK_JOIN" in errors_of(raw)


def test_r3_ambiguous_multi_path():
    raw = V()
    raw["tables"]["warehouses"] = {
        "source": "s.warehouses",
        "primary_key": ["id"],
        "comment": "One row per warehouse. Primary date: opened_on. Source: s.warehouses.",
    }
    raw["relationships"] += [
        {"name": "orders_warehouses", "child": "orders", "columns": ["warehouse_id"], "parent": "warehouses"},
        {"name": "warehouses_customers", "child": "warehouses", "columns": ["owner_id"], "parent": "customers"},
    ]
    assert "ERR_AMBIGUOUS_MULTI_PATH" in errors_of(raw)


# ---- logic (SV-R4..R6) --------------------------------------------------------


def test_r4_numeric_in_dimensions():
    raw = V()
    raw["dimensions"].append(
        {"table": "orders", "name": "basket", "expr": "qty * price", "comment": "Order basket value in cents here."}
    )
    assert "ERR_NUMERIC_IN_DIMENSIONS" in errors_of(raw)


def test_r4_aggregate_in_dimension_rejected():
    raw = V()
    raw["dimensions"].append(
        {"table": "orders", "name": "total", "expr": "SUM(amount)", "comment": "Total order amount aggregated badly."}
    )
    assert "ERR_INVALID_METRIC" in errors_of(raw)


def test_r5_metric_must_aggregate():
    raw = V()
    raw["metrics"].append(
        {"table": "orders", "name": "raw_amount", "expr": "amount + 1",
         "comment": "Broken metric with no aggregate. Population: all. Additivity: additive."}
    )
    assert "ERR_INVALID_METRIC" in errors_of(raw)


def test_r5_no_current_date():
    raw = V()
    raw["metrics"].append(
        {"table": "orders", "name": "recent", "expr": "COUNT(CASE WHEN order_date > CURRENT_DATE THEN id END)",
         "comment": "Orders after today, which is non-deterministic. Population: recent. Additivity: additive."}
    )
    assert "ERR_NON_DETERMINISTIC_TIME" in errors_of(raw)


def test_r5_snapshot_requires_non_additive_by():
    raw = V()
    raw["tables"]["inventory"] = {
        "source": "s.inventory",
        "primary_key": ["snapshot_date", "sku"],
        "comment": "One row per sku per day. Point-in-time snapshot. Primary date: snapshot_date. Source: s.inventory.",
    }
    raw["relationships"].append({"name": "inv_orders", "child": "inventory", "columns": ["order_id"], "parent": "orders"})
    raw["dimensions"].append(
        {"table": "inventory", "name": "snapshot_date", "expr": "snapshot_date",
         "comment": "Inventory snapshot date. Grains: day."}
    )
    raw["metrics"].append(
        {"table": "inventory", "name": "stock", "expr": "SUM(units)",
         "comment": "Units on hand at snapshot date. Population: all skus. Additivity: non-additive."}
    )
    assert "ERR_MISSING_NON_ADDITIVE_BY" in errors_of(raw)
    raw["metrics"][-1]["non_additive_by"] = "snapshot_date"
    assert "ERR_MISSING_NON_ADDITIVE_BY" not in errors_of(raw)


def test_r5_ratio_composition():
    raw = V()
    raw["metrics"].append(
        {"table": None, "name": "bad_ratio", "expr": "SUM(a / b)",
         "comment": "Aggregating inside a ratio is forbidden. Additivity: ratio."}
    )
    assert "ERR_INVALID_RATIO_METRIC" in errors_of(raw)

    raw2 = V()
    raw2["metrics"].append(
        {"table": "orders", "name": "nested", "expr": "SUM(order_count)",
         "comment": "Nested aggregation of a metric. Population: all. Additivity: additive."}
    )
    assert "ERR_INVALID_RATIO_METRIC" in errors_of(raw2)

    raw3 = V()
    raw3["metrics"].append(
        {"table": None, "name": "mystery", "expr": "order_count / unknown_thing",
         "comment": "References something undefined. Additivity: ratio."}
    )
    assert "ERR_INVALID_RATIO_METRIC" in errors_of(raw3)


def test_r6_security_plumbing_denied_and_allowlisted():
    raw = V()
    raw["dimensions"].append(
        {"table": "customers", "name": "user_email", "expr": "user_email",
         "comment": "Customer email address, which is security plumbing."}
    )
    assert "ERR_SECURITY_PLUMBING_EXPOSED" in errors_of(raw)
    cfg = LintConfig(security_allowlist=("user_email",))
    assert "ERR_SECURITY_PLUMBING_EXPOSED" not in errors_of(raw, cfg)


def test_r6_duplicate_field_names():
    raw = V()
    raw["dimensions"].append(
        {"table": "orders", "name": "region", "expr": "ship_region", "comment": "Shipping region of the order here."}
    )
    assert "ERR_DUPLICATE_FIELD_NAME" in errors_of(raw)


# ---- governance (SV-R8, SV-R9) -------------------------------------------------


def test_r8_duplicate_synonym():
    raw = V()
    raw["dimensions"][0]["synonyms"] = ["order_count"]  # collides with the metric name
    assert "ERR_DUPLICATE_SYNONYM" in errors_of(raw)


def test_r8_warns_on_lazy_comments():
    raw = V()
    raw["metrics"][0]["comment"] = "Order count."
    assert "WARN_COMMENT_TOO_SHORT" in warns_of(raw)


def test_r9_security_in_prose_is_policy():
    raw = V(ai_sql_generation="Only show rows the user's role permits.")
    report = lint(build_view(raw))
    assert any(f.code == "POLICY_SECURITY_IN_PROSE" and f.severity == Severity.POLICY for f in report.findings)


def test_ensure_valid_raises_with_code():
    raw = V()
    raw["tables"]["orders"]["comment"] = "One row per order. Source: s.orders."
    with pytest.raises(CompilerError) as e:
        ensure_valid(build_view(raw))
    assert e.value.code in ("ERR_MISSING_PRIMARY_DATE",)
