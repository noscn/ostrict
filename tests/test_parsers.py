"""Milestone 1: dual parsers produce identical canonical ASTs."""

from pathlib import Path

import pytest

from strict_semantic.models.ast import Partition
from strict_semantic.models.errors import ERR_PARSE, ERR_UNKNOWN_TABLE, CompilerError
from strict_semantic.parsers import load_view, parse_ddl_view, parse_yaml_view

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def ddl_view():
    return load_view(FIXTURES / "ecommerce.sql")


@pytest.fixture(scope="module")
def yaml_view():
    return load_view(FIXTURES / "ecommerce.yaml")


def test_dual_input_equivalence(ddl_view, yaml_view):
    """Golden test 1 (first half): .sql and .yaml fixtures build identical ASTs."""
    assert ddl_view.model_dump() == yaml_view.model_dump()
    assert ddl_view == yaml_view


def test_ddl_structure(ddl_view):
    v = ddl_view
    assert v.name == "ecommerce"
    assert set(v.tables) == {"customers", "orders", "order_items", "products", "daily_inventory_snapshot"}
    assert v.tables["order_items"].primary_key == ("order_id", "line_no")
    assert [r.name for r in v.relationships] == [
        "orders_customers",
        "items_orders",
        "items_products",
        "inventory_products",
    ]
    r = v.relationships[0]
    assert (r.child, r.child_columns, r.parent, r.parent_columns) == (
        "orders",
        ("customer_id",),
        "customers",
        ("customer_id",),
    )
    assert len(v.facts) == 1 and v.facts[0].expr == "quantity * unit_price"
    assert len(v.dimensions) == 8 and len(v.metrics) == 7
    assert v.comment.startswith("Retail e-commerce")
    assert v.ai_sql_generation.startswith("Route revenue")


def test_comment_directives(ddl_view):
    v = ddl_view
    assert v.tables["orders"].primary_date == "order_date"
    assert v.tables["orders"].partition == Partition(
        column="order_date", dimension="order_date", lookback_days=0, lookahead_days=0
    )
    assert v.tables["daily_inventory_snapshot"].snapshot is True
    assert v.tables["customers"].snapshot is False

    region, _ = v.find_dimension("region")
    assert region.values == ("EU", "US", "APAC")
    assert region.synonyms == ("market", "country_group")
    order_date, grain = v.find_dimension("order_date__month")
    assert order_date.grains == ("day", "week", "month") and grain == "month"


def test_metric_kinds(ddl_view):
    v = ddl_view
    stock = v.find_metric("stock_units")
    assert stock.non_additive_by == "snapshot_date" and stock.table == "daily_inventory_snapshot"
    aov = v.find_metric("aov")
    assert aov.is_derived
    assert [m.name for m in v.base_metrics(aov)] == ["revenue", "order_count"]


def test_synonym_lookup(ddl_view):
    d, _ = ddl_view.find_dimension("market")
    assert d.name == "region"


def test_unknown_field_suggests(ddl_view):
    with pytest.raises(CompilerError) as e:
        ddl_view.find_metric("revenu")
    assert e.value.code == "ERR_UNKNOWN_FIELD" and "revenue" in str(e.value)


def test_parse_errors_have_codes():
    with pytest.raises(CompilerError) as e:
        parse_ddl_view("CREATE SEMANTIC VIEW v TABLES (t AS s.t PRIMARY KEY (id) NONSENSE x)")
    assert e.value.code == ERR_PARSE

    with pytest.raises(CompilerError) as e:
        parse_yaml_view("name: v\ntables:\n  t: {source: s.t}\nmetrics:\n  t.m: {expr: 'SUM(']}")
    assert e.value.code == ERR_PARSE

    with pytest.raises(CompilerError) as e:
        parse_yaml_view("name: v\ntables:\n  t: {source: s.t}\nmetrics:\n  nope.m: 'SUM(x)'")
    assert e.value.code == ERR_UNKNOWN_TABLE


def test_conflicting_directive_and_key_rejected():
    spec = (
        "name: v\n"
        "tables:\n"
        "  t:\n"
        "    source: s.t\n"
        "    primary_date: a\n"
        "    comment: 'One row per thing. Primary date: b.'\n"
    )
    with pytest.raises(CompilerError) as e:
        parse_yaml_view(spec)
    assert e.value.code == ERR_PARSE and "primary date" in str(e.value)
