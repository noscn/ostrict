"""The studio's local server: a handful of read-only JSON endpoints + one page.

Strictly read-only by construction — there is no endpoint that writes to a
`.yaml` or `.sql` file. Hot reload works the same way as the MCP server's spec
cache: every API call fingerprints the watched files on ``(mtime_ns, size)``
and re-parses only when something changed, so saving a spec in an editor is
reflected on the browser's next 1.5 s poll without restarting the process.
Path resolution and the snapshot cache live in :mod:`ostrict.ui.state`,
shared with the standalone exporter (:mod:`ostrict.ui.export`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..linter import LintConfig
from .state import _STATIC, UIState, summaries


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
        return {"stamp": stamp, "views": summaries(snap), "broken": snap["broken"]}

    @app.get("/api/views")
    def api_views() -> dict:
        snap, _ = state.snapshot()
        return {"views": summaries(snap), "broken": snap["broken"]}

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
