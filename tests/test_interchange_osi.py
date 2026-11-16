"""v0.2.0 OSI interchange: lossless round-trip, mandatory lint gate on import,
strip-extensions warnings, and the export/import CLI end to end."""

import json
import subprocess
import sys

import pytest
import yaml
from conftest import FIXTURES, JAN

from strict_semantic.compiler import compile_semantic_query
from strict_semantic.interchange import export_osi, import_osi, to_native_yaml
from strict_semantic.models.errors import CompilerError
from strict_semantic.parsers import load_view


def _fresh():
    # pydantic equality covers private attrs, so compare views nothing has compiled yet
    return load_view(FIXTURES / "ecommerce.sql")


def _with_numeric_dimension(doc: dict) -> str:
    items = next(d for d in doc["datasets"] if d["name"] == "order_items")
    items["fields"].append({"name": "discounted_total", "kind": "dimension", "expression": "quantity * unit_price"})
    return yaml.safe_dump(doc, sort_keys=False)


def test_osi_roundtrip_ast_equality():
    original = _fresh()
    as_yaml = export_osi(original)
    as_json = export_osi(original, fmt="json")
    assert as_yaml.warnings == ()
    assert import_osi(as_yaml.text) == original
    assert import_osi(as_json.text) == original


def test_osi_roundtrip_compiled_sql_parity(view):
    req = {
        "metrics": ["revenue", "aov"],  # drill-across + derived ratio
        "dimensions": ["region", "order_date__month"],
        "filters": JAN,  # exercises partition pruning on orders and order_items
        "order_by": ["-revenue"],
        "limit": 100,
    }
    roundtripped = import_osi(export_osi(view).text)
    assert (
        compile_semantic_query(roundtripped, req, dialect="bigquery").sql
        == compile_semantic_query(view, req, dialect="bigquery").sql
    )


def test_osi_import_enforces_linter(view):
    text = _with_numeric_dimension(yaml.safe_load(export_osi(view, strip_extensions=True).text))
    with pytest.raises(CompilerError) as e:
        import_osi(text)
    assert e.value.code == "ERR_NUMERIC_IN_DIMENSIONS"  # SV-R4.1


def test_osi_strip_extensions_warnings():
    original = _fresh()
    out = export_osi(original, strip_extensions=True)
    assert "custom_extensions" not in out.text
    assert any("partition" in w for w in out.warnings)
    # the comment directives still carry every guardrail for strict-semantic importers
    assert import_osi(out.text) == original


def test_cli_export_and_import_end_to_end(tmp_path):
    cli = [sys.executable, "-m", "strict_semantic.cli"]
    osi_file = tmp_path / "ecommerce.osi.json"
    native_file = tmp_path / "imported.yaml"

    run = subprocess.run(
        [*cli, "export", str(FIXTURES / "ecommerce.sql"), "--format", "json", "-o", str(osi_file)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(osi_file.read_text())["semantic_model"]["name"] == "ecommerce"

    run = subprocess.run([*cli, "import", str(osi_file), "-o", str(native_file)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert load_view(native_file) == _fresh()

    # a vanilla OSI file with a numeric dimension is refused (exit 1, nothing written) unless --no-lint
    bad = tmp_path / "bad.osi.yaml"
    bad.write_text(_with_numeric_dimension(yaml.safe_load(export_osi(_fresh(), strip_extensions=True).text)))
    rejected = tmp_path / "rejected.yaml"
    run = subprocess.run([*cli, "import", str(bad), "-o", str(rejected)], capture_output=True, text=True)
    assert run.returncode == 1 and "ERR_NUMERIC_IN_DIMENSIONS" in run.stderr
    assert not rejected.exists()
    run = subprocess.run([*cli, "import", str(bad), "--no-lint", "-o", str(rejected)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert rejected.exists()


def test_native_emission_refuses_undirectable_values(view):
    # the native 'Values: a, b.' grammar splits on commas and ends at a period, so a
    # value containing either cannot be appended as a directive without reading back
    # as different values; import keeps it in memory, native emission refuses it
    doc = yaml.safe_load(export_osi(view, strip_extensions=True).text)
    customers = next(d for d in doc["datasets"] if d["name"] == "customers")
    region = next(f for f in customers["fields"] if f["name"] == "region")
    region["description"] = "Customer home region."  # no Values: directive
    region["values"] = ["Acme, Inc.", "US"]
    imported = import_osi(yaml.safe_dump(doc, sort_keys=False))
    assert next(d for d in imported.dimensions if d.name == "region").values == ("Acme, Inc.", "US")
    with pytest.raises(CompilerError) as e:
        to_native_yaml(imported)
    assert e.value.code == "ERR_PARSE" and "Acme, Inc." in str(e.value)
