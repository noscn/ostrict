"""v0.4.0 `ostrict scaffold`: dbt directory, manifest.json, live DuckDB schema, CLI."""

import subprocess
import sys
from pathlib import Path

import yaml
from conftest import FIXTURES

from ostrict.connectors import from_config
from ostrict.linter import lint
from ostrict.parsers import parse_yaml_view
from ostrict.scaffold import scaffold_view

DBT_DIR = FIXTURES / "dbt_shop"
EXAMPLES = Path(__file__).parent.parent / "examples"


def _duckdb():
    cfg = yaml.safe_load((EXAMPLES / "config.duckdb.yml").read_text())
    return from_config(cfg["connector"], EXAMPLES)


def test_scaffold_from_dbt_directory_is_lint_clean():
    res = scaffold_view(from_dbt=str(DBT_DIR))
    assert res.report.ok, res.report.render()
    view = parse_yaml_view(res.text)  # the emitted draft parses as a native spec...
    assert lint(view).ok  # ...and passes the gate on its own
    body = "\n".join(line for line in res.text.splitlines() if not line.startswith("#"))
    assert "user_email" not in body  # SV-R6.2: dropped from the model, mentioned only in the TODO header
    assert any("user_email" in n and "deny-list" in n for n in res.notes)
    dims = {d.name for d in view.dimensions}
    assert {"orders_status", "shipments_status"} <= dims and "status" not in dims  # SV-R6.4 prefixing
    # the diamond edge (shipments -> customers) became a TODO comment, not a relationship
    assert [r.name for r in view.relationships] == ["orders_to_customers", "shipments_to_orders"]
    assert "SV-R3.5" in res.text and "from: shipments (customer_id)" in res.text
    orders = view.tables["orders"]
    assert orders.partition and (orders.partition.lookback_days, orders.partition.lookahead_days) == (1, 1)
    assert {m.name for m in view.metrics} == {"sum_total_amount", "sum_weight_kg"}
    shipped = next(d for d in view.dimensions if d.name == "shipped_at")
    assert shipped.time_type == "timestamp" and shipped.grains == ("day", "week", "month")


def test_scaffold_manifest_matches_directory():
    a = scaffold_view(from_dbt=str(DBT_DIR))
    b = scaffold_view(from_dbt=str(FIXTURES / "dbt_shop_manifest.json"))
    assert a.text == b.text  # both readers normalize to the same draft, byte for byte


def test_scaffold_from_duckdb_schema_with_sample_values():
    res = scaffold_view(
        connector=_duckdb(),
        models="shop.customers,shop.orders,shop.order_items,shop.products",
        sample_values=True,
    )
    assert res.report.ok, res.report.render()
    view = parse_yaml_view(res.text)
    assert view.name == "shop" and view.dialect == "duckdb"
    region = next(d for d in view.dimensions if d.name == "region")
    assert region.values == ("APAC", "EU", "US")  # sampled, sorted, grammar-safe (SV-R6.7)
    assert len(view.relationships) == 3  # inferred from *_id naming conventions
    assert {d.name for d in view.dimensions} >= {"orders_order_date", "order_items_order_date"}
    assert any("guessed primary key" in n for n in res.notes)  # order_items has no conventional key


def test_scaffold_cli_end_to_end(tmp_path):
    cli = [sys.executable, "-m", "ostrict.cli"]
    out = tmp_path / "shop_draft.yaml"
    run = subprocess.run(
        [*cli, "scaffold", "--from-dbt", str(DBT_DIR), "-o", str(out)], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr
    assert "note:" in run.stderr
    run = subprocess.run([*cli, "lint", str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
