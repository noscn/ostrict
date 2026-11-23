"""v0.5.0 `ostrict ui`: graph payload roles, lint mapping, endpoints, hot reload, CLI."""

import shutil
import sys

import pytest

fastapi = pytest.importorskip("fastapi")
from conftest import FIXTURES  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ostrict.cli import main as cli_main  # noqa: E402
from ostrict.parsers import load_view, parse_yaml_view  # noqa: E402
from ostrict.ui.app import create_app  # noqa: E402
from ostrict.ui.graph_payload import build_ui_payload  # noqa: E402

ROLE_PLAY = """
name: hr
dialect: bigquery
comment: Role-playing demo over one shared physical employee table.
tables:
  assignments:
    source: dw.assignments
    primary_key: [id]
    comment: 'One row per assignment. Primary date: assigned_on. Source: dw.assignments.'
  manager:
    source: dw.employee
    primary_key: [id]
    comment: 'One row per employee, acting as manager. Primary date: hired_on. Source: dw.employee.'
  specialist:
    source: DW.employee
    primary_key: [id]
    comment: 'One row per employee, acting as specialist. Primary date: hired_on. Source: dw.employee.'
relationships:
  - name: a_manager
    from: assignments (manager_id)
    to: manager (id)
  - name: a_specialist
    from: assignments (specialist_id)
    to: specialist (id)
dimensions:
  manager.manager_region: {expr: region, comment: 'Home region of the managing employee record.'}
metrics:
  assignments.assignment_count:
    expr: COUNT(id)
    comment: 'Number of assignments on file. Population: all assignments. Additivity: additive.'
"""

BROKEN = """
name: broken
dialect: bigquery
comment: Deliberately broken fixture for the studio's error overlays.
tables:
  customers:
    source: shop.customers
    primary_key: [customer_id]
    comment: 'One row per customer. Source: shop.customers.'
  orders:
    source: shop.orders
    primary_key: [order_id]
    comment: 'One row per order. Primary date: order_date. Source: shop.orders.'
  shipments:
    source: shop.shipments
    primary_key: [shipment_id]
    comment: 'One row per shipment. Primary date: shipped_at. Source: shop.shipments.'
relationships:
  - name: orders_customers
    from: orders (customer_id)
    to: customers (customer_id)
  - name: shipments_orders
    from: shipments (order_id)
    to: orders (order_id)
  - name: shipments_customers
    from: shipments (customer_id)
    to: customers (customer_id)
  - name: orders_customers_by_name
    from: orders (customer_name)
    to: customers (name)
dimensions:
  customers.region: {expr: region, comment: 'Home region of the customer record.'}
metrics:
  orders.order_count:
    expr: COUNT(order_id)
    comment: 'Number of orders placed. Population: all orders. Additivity: additive.'
"""


def test_roles_on_ecommerce_fixture():
    p = build_ui_payload(load_view(FIXTURES / "ecommerce.sql"))
    roles = {n["name"]: n["role"] for n in p["nodes"]}
    assert roles["daily_inventory_snapshot"] == "snapshot"
    assert roles["order_items"] == "fact" and roles["orders"] == "fact"
    assert roles["products"] == "dimension"
    assert not any(n["role_playing"] for n in p["nodes"])  # all sources distinct
    assert p["ok"] and p["summary"]["errors"] == 0
    assert [m["name"] for m in p["derived_metrics"]] == ["aov"]  # derived metrics live at view level
    assert all("aov" not in [m["name"] for m in n["metrics"]] for n in p["nodes"])


def test_role_playing_detection():
    p = build_ui_payload(parse_yaml_view(ROLE_PLAY))
    nodes = {n["name"]: n for n in p["nodes"]}
    assert nodes["assignments"]["role"] == "fact" and not nodes["assignments"]["role_playing"]
    for name, sibling in (("manager", "specialist"), ("specialist", "manager")):
        assert nodes[name]["role"] == "dimension"
        assert nodes[name]["role_playing"] and nodes[name]["shared_source_with"] == [sibling]


def test_broken_spec_builds_payload_with_mapped_findings():
    p = build_ui_payload(parse_yaml_view(BROKEN))  # never raises on lint errors
    assert not p["ok"] and p["summary"]["errors"] >= 3
    nodes = {n["name"]: n for n in p["nodes"]}
    # diamond multi-path lands on the child table, where the resolver reports it
    assert any(f["rule"] == "SV-R3.5" for f in nodes["shipments"]["findings"])
    # missing primary date lands on the table node
    assert any(f["code"] == "ERR_MISSING_PRIMARY_DATE" for f in nodes["customers"]["findings"])
    # partial-key join marks its edge red
    edges = {e["name"]: e for e in p["edges"]}
    assert edges["orders_customers_by_name"]["status"] == "error"
    assert any(f["rule"] == "SV-R3.2" for f in edges["orders_customers_by_name"]["findings"])
    assert edges["orders_customers"]["status"] == "ok"


def test_http_endpoints_and_hot_reload(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    shutil.copy(FIXTURES / "ecommerce.yaml", views / "ecommerce.yaml")
    (views / "broken.yaml").write_text(BROKEN)
    client = TestClient(create_app(views))

    page = client.get("/")
    assert page.status_code == 200 and "ostrict" in page.text.lower() and "<svg" in page.text

    listing = client.get("/api/views").json()
    assert [v["name"] for v in listing["views"]] == ["broken", "ecommerce"]
    assert next(v for v in listing["views"] if v["name"] == "ecommerce")["errors"] == 0
    assert next(v for v in listing["views"] if v["name"] == "broken")["errors"] >= 3

    data = client.get("/api/views/ecommerce").json()
    assert {n["name"] for n in data["nodes"]} >= {"orders", "customers", "order_items"}
    assert client.get("/api/views/nope").status_code == 404

    snips = client.get("/api/mcp-snippets").json()
    assert "ostrict-mcp" in snips["claude_code"] and "mcpServers" in snips["claude_desktop"]

    stamp1 = client.get("/api/state").json()["stamp"]
    text = (views / "ecommerce.yaml").read_text().replace(
        "Retail e-commerce sales and inventory", "Edited retail sales and inventory"
    )
    (views / "ecommerce.yaml").write_text(text)
    assert client.get("/api/views/ecommerce").json()["comment"].startswith("Edited retail")
    assert client.get("/api/state").json()["stamp"] != stamp1


def test_app_accepts_config_file_and_single_file(tmp_path):
    views = tmp_path / "v"
    views.mkdir()
    shutil.copy(FIXTURES / "ecommerce.yaml", views / "ecommerce.yaml")
    cfg = tmp_path / "ostrict.yml"
    cfg.write_text("views: ./v\nconnector:\n  type: duckdb\nlint:\n  security_allowlist: []\n")
    via_config = TestClient(create_app(cfg)).get("/api/views").json()
    assert [v["name"] for v in via_config["views"]] == ["ecommerce"]
    snips = TestClient(create_app(cfg)).get("/api/mcp-snippets").json()
    assert str(cfg) in snips["claude_code"]  # resolved config path is pre-filled
    via_file = TestClient(create_app(views / "ecommerce.yaml")).get("/api/views").json()
    assert [v["name"] for v in via_file["views"]] == ["ecommerce"]


def test_page_ships_v054_studio_affordances(tmp_path):
    """v0.5.4: the served page carries the large-view affordances (compact cards,
    sidebar toggle, collapsible sections) and stays a single self-contained file."""
    views = tmp_path / "views"
    views.mkdir()
    shutil.copy(FIXTURES / "ecommerce.yaml", views / "ecommerce.yaml")
    page = TestClient(create_app(views)).get("/").text
    for marker in ('id="modebtn"', 'id="sbtoggle"', '<details class="acc"', "COMPACT_AT"):
        assert marker in page
    assert "<script src=" not in page and "https://" not in page  # no external libraries or CDNs


def test_cli_help_and_missing_extras(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        cli_main(["ui", "--help"])
    assert e.value.code == 0
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # import uvicorn -> ImportError
    assert cli_main(["ui", "--no-browser"]) == 1
    assert 'pip install "ostrict[ui]"' in capsys.readouterr().err
