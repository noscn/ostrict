"""v0.3.0 rename to ostrict: entry points old and new, config env vars, import alias."""

import importlib
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import pytest
from conftest import FIXTURES


def test_console_scripts_old_and_new():
    eps = {e.name: e.value for e in distribution("ostrict").entry_points if e.group == "console_scripts"}
    assert eps == {
        "ostrict": "ostrict.cli:main",
        "ostrict-mcp": "ostrict.mcp_server.server:main",
        "strict-semantic": "ostrict.cli:main",
        "strict-semantic-mcp": "ostrict.mcp_server.server:main",
    }
    bindir = Path(sys.executable).parent  # console scripts land next to the interpreter
    for script in ("ostrict", "strict-semantic"):
        run = subprocess.run(
            [str(bindir / script), "lint", str(FIXTURES / "ecommerce.sql")], capture_output=True, text=True
        )
        assert run.returncode == 0, (script, run.stderr)


def test_config_env_var_priority_and_fallback(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    views = tmp_path / "views"
    views.mkdir()
    shutil.copy(FIXTURES / "ecommerce.yaml", views / "ecommerce.yaml")
    cfg = tmp_path / "ostrict.yml"
    cfg.write_text("views: ./views\nconnector:\n  type: duckdb\n")
    from ostrict.mcp_server.server import _State

    monkeypatch.setenv("OSTRICT_CONFIG", str(cfg))
    monkeypatch.setenv("STRICT_SEMANTIC_CONFIG", str(tmp_path / "nope.yml"))  # must lose
    assert "ecommerce" in _State().views()

    monkeypatch.delenv("OSTRICT_CONFIG")
    monkeypatch.setenv("STRICT_SEMANTIC_CONFIG", str(cfg))  # pre-rename name still honored
    assert "ecommerce" in _State().views()


def test_import_strict_semantic_alias():
    for name in [m for m in list(sys.modules) if m == "strict_semantic" or m.startswith("strict_semantic.")]:
        del sys.modules[name]
    with pytest.warns(DeprecationWarning, match="renamed to 'ostrict'"):
        legacy = importlib.import_module("strict_semantic")
    import ostrict

    assert legacy.SemanticView is ostrict.SemanticView
    from strict_semantic.parsers import load_view as legacy_load  # submodules alias too

    from ostrict.parsers import load_view

    assert legacy_load is load_view
    assert sys.modules["strict_semantic.parsers"] is sys.modules["ostrict.parsers"]
