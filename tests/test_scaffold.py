"""`ostrict scaffold` (v0.4.0 + v0.4.1 hardening): dbt directory, manifest + catalog,
meta overrides, PII flagging, selection by tag/glob, live DuckDB schema, CLI."""

import subprocess
import sys
from pathlib import Path

import yaml
from conftest import FIXTURES

from ostrict.connectors import from_config
from ostrict.linter import LintConfig, lint
from ostrict.linter.engine import DEFAULT_SECURITY_DENY
from ostrict.parsers import parse_yaml_view
from ostrict.scaffold import ColumnDraft, TableDraft, generate, scaffold_view

DBT_DIR = FIXTURES / "dbt_shop"
MANIFEST = DBT_DIR / "target" / "manifest.json"
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
    assert "internal_hash" not in body  # meta.semantic kind: skip
    dims = {d.name for d in view.dimensions}
    assert {"orders_status", "shipments_status"} <= dims and "status" not in dims  # SV-R6.4 prefixing
    assert {"phone", "priority_score"} <= dims  # PII kept-but-flagged; meta forces numeric -> dimension
    assert "TODO(PII-check)" in res.text and any("phone" in n and "PII" in n for n in res.notes)
    # the diamond edge (shipments -> customers) became a TODO comment, not a relationship
    assert [r.name for r in view.relationships] == ["orders_to_customers", "shipments_to_orders"]
    assert "SV-R3.5" in res.text and "from: shipments (customer_id)" in res.text
    orders = view.tables["orders"]
    assert orders.partition and (orders.partition.lookback_days, orders.partition.lookahead_days) == (1, 1)
    # one COUNT per keyed table; no sample SUM for sequence-like line_no (it stays a fact)
    assert {m.name for m in view.metrics} == {
        "sum_total_amount", "sum_weight_kg", "customers_count", "orders_count", "shipments_count"
    }
    assert "line_no" in {f.name for f in view.facts}
    # catalog.json supplied the types the yml left out (all of shipments, customers.phone)
    assert any("catalog.json" in n for n in res.notes)
    shipped = next(d for d in view.dimensions if d.name == "shipped_at")
    assert shipped.time_type == "timestamp" and shipped.grains == ("day", "week", "month")
    # declared enumerations keep the author's order (accepted_values test; meta.semantic values)
    assert next(d for d in view.dimensions if d.name == "orders_status").values == ("completed", "cancelled", "refunded")
    assert next(d for d in view.dimensions if d.name == "region").values == ("EU", "US", "APAC")
    # a description that already states the grain is not repeated
    comment = view.tables["shipments"].comment
    assert comment.startswith("One row per parcel") and comment.count("One row per") == 1


def test_scaffold_manifest_matches_directory():
    a = scaffold_view(from_dbt=str(DBT_DIR))
    b = scaffold_view(from_dbt=str(MANIFEST))
    assert a.text == b.text  # both readers normalize to the same draft, byte for byte


def test_scaffold_model_selection_by_tag_and_glob():
    core = scaffold_view(from_dbt=str(DBT_DIR), models="tag:core")
    assert set(core.view.tables) == {"customers", "orders"} and core.report.ok
    ship = scaffold_view(from_dbt=str(DBT_DIR), models="ship*")
    assert set(ship.view.tables) == {"shipments"} and ship.report.ok  # catalog types make it self-sufficient


def test_scaffold_honors_lint_config():
    cfg = LintConfig(security_deny=(*DEFAULT_SECURITY_DENY, "phone"), security_allowlist=("user_email",))
    res = scaffold_view(from_dbt=str(DBT_DIR), lint_config=cfg)
    assert res.report.ok, res.report.render()
    dims = {d.name for d in res.view.dimensions}
    assert "phone" not in dims  # denied by the extended list
    assert "user_email" in dims  # allow-listed through the deny-list...
    assert any("user_email" in n and "PII" in n for n in res.notes)  # ...but still flagged for review


def test_generator_candidates_inference_and_dev_warning():
    member = TableDraft(
        name="member", source="dev_int_dw.member", primary_key=["id"],
        columns=[ColumnDraft("id", "INT64"), ColumnDraft("groupName", "STRING"), ColumnDraft("registerAt")],
    )
    funding = TableDraft(
        name="fundingTx", source="dev_int_dw.fundingTx", primary_key=["id"], partition_column="requestedAt",
        columns=[ColumnDraft("id", "INT64"), ColumnDraft("memberId", "INT64"),
                 ColumnDraft("requestedAt"), ColumnDraft("netAmount", "NUMERIC")],
    )
    res = generate([funding, member], name="kz_unit", origin="unit", infer_types=True)
    assert not res.report.ok  # no declared joins: the orphan errors stay honest
    # camelCase foreign keys become paste-ready candidate joins, never silent auto-joins
    assert "candidate joins" in res.text
    assert "from: fundingTx (memberId)" in res.text and "to: member (id)" in res.text
    assert "dev-looking schema 'dev_int_dw'" in res.text
    req = next(d for d in res.view.dimensions if d.name == "requestedAt")
    assert req.time_type == "timestamp" and req.grains  # --infer-types: named time column
    assert any("inferred from column names" in n for n in res.notes)
    part = res.view.tables["fundingTx"].partition
    assert part and part.dimension == "requestedAt"  # inference unlocked the partition hint


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
    metric_names = {m.name for m in view.metrics}
    assert {"customers_count", "orders_count", "order_items_count", "products_count"} <= metric_names
    assert "sum_line_no" not in metric_names and "sum_quantity" in metric_names
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
