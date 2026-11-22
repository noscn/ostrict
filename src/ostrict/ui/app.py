"""The studio's local server: a handful of read-only JSON endpoints + one page.

Strictly read-only by construction — there is no endpoint that writes to a
`.yaml` or `.sql` file. Hot reload works the same way as the MCP server's spec
cache: every API call fingerprints the watched files on ``(mtime_ns, size)``
and re-parses only when something changed, so saving a spec in an editor is
reflected on the browser's next 1.5 s poll without restarting the process.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..cli import _lint_config
from ..linter import LintConfig
from ..models.errors import CompilerError
from ..parsers import load_view
from .graph_payload import build_ui_payload

_STATIC = Path(__file__).parent / "static" / "index.html"
_SUFFIXES = (".sql", ".yml", ".yaml")


class UIState:
    """Resolve what to watch (a view file, a folder, or an ostrict.yml config)."""

    def __init__(self, path: str | Path | None = None, lint_config: LintConfig | None = None):
        p = Path(
            path or os.environ.get("OSTRICT_CONFIG") or os.environ.get("STRICT_SEMANTIC_CONFIG") or "ostrict.yml"
        ).resolve()
        self.lint_config = lint_config
        self.config_path: Path | None = None
        if p.is_file() and p.suffix.lower() in (".yml", ".yaml"):
            doc = yaml.safe_load(p.read_text()) or {}
            if isinstance(doc, dict) and "tables" not in doc and "views" in doc:
                self.config_path = p  # an ostrict.yml config, not a view spec
                if self.lint_config is None:
                    self.lint_config = _lint_config(str(p))
                p = (p.parent / str(doc.get("views") or "views")).resolve()
        self.views_path = p
        self._cache: dict | None = None
        self._key: tuple | None = None

    def _files(self) -> list[Path]:
        p = self.views_path
        if p.is_dir():
            return [f for f in sorted(p.iterdir()) if f.suffix.lower() in _SUFFIXES]
        return [p]

    def _fingerprint(self) -> tuple:
        out = []
        for f in self._files():
            try:
                st = f.stat()
                out.append((str(f), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(f), -1, -1))
        return tuple(out)

    def snapshot(self) -> tuple[dict, int]:
        key = self._fingerprint()
        if self._cache is None or key != self._key:
            views: dict[str, dict] = {}
            broken: list[dict] = []
            for f in self._files():
                try:
                    v = load_view(f)
                    views[v.name] = {"file": str(f), "payload": build_ui_payload(v, self.lint_config)}
                except CompilerError as e:
                    broken.append({"file": str(f), "code": e.code, "error": str(e)})
                except OSError as e:
                    broken.append({"file": str(f), "code": "ERR_IO", "error": str(e)})
            self._cache = {"views": views, "broken": broken}
            self._key = key
        return self._cache, hash(self._key) & 0x7FFFFFFF


def _summaries(snap: dict) -> list[dict]:
    return [
        {
            "name": name,
            "file": entry["file"],
            "ok": entry["payload"]["ok"],
            "errors": entry["payload"]["summary"]["errors"],
            "warnings": entry["payload"]["summary"]["warnings"],
        }
        for name, entry in sorted(snap["views"].items())
    ]


def _mcp_snippets(state: UIState) -> dict:
    cfg = str(state.config_path) if state.config_path else "<absolute path to your ostrict.yml>"
    exe = shutil.which("ostrict-mcp") or "ostrict-mcp"
    server = {"command": exe, "args": [], "env": {"OSTRICT_CONFIG": cfg}}
    return {
        "config_path": cfg,
        "command": exe,
        "claude_desktop": json.dumps({"mcpServers": {"ostrict": server}}, indent=2),
        "claude_code": f"claude mcp add ostrict -e OSTRICT_CONFIG={cfg} -- {exe}",
        "cursor": json.dumps({"mcpServers": {"ostrict": server}}, indent=2),
    }


def create_app(path: str | Path | None = None, lint_config: LintConfig | None = None) -> FastAPI:
    state = UIState(path, lint_config)
    app = FastAPI(title="ostrict ui", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _STATIC.read_text()

    @app.get("/api/state")
    def api_state() -> dict:
        snap, stamp = state.snapshot()
        return {"stamp": stamp, "views": _summaries(snap), "broken": snap["broken"]}

    @app.get("/api/views")
    def api_views() -> dict:
        snap, _ = state.snapshot()
        return {"views": _summaries(snap), "broken": snap["broken"]}

    @app.get("/api/views/{name}")
    def api_view(name: str):
        snap, _ = state.snapshot()
        entry = snap["views"].get(name)
        if entry is None:
            return JSONResponse({"error": f"unknown view '{name}'"}, status_code=404)
        return {"file": entry["file"], **entry["payload"]}

    @app.get("/api/mcp-snippets")
    def api_snippets() -> dict:
        return _mcp_snippets(state)

    return app
