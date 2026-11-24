"""Render the studio to one standalone HTML file.

The export is the exact page the server serves, with the resolved snapshot
embedded as an inert JSON island instead of fetched — so it opens from disk
(``file://``), attaches to a ticket or a wiki, and needs no Python on the
reader's machine. Like the server it is read-only by construction; unlike the
server it is a picture of the model at one moment, stamped with its export
time, and never polls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..linter import LintConfig
from .state import _STATIC, UIState, summaries

_ISLAND = '<script id="bootdata" type="application/json">null</script>'


def export_studio_html(path: str | Path | None = None, lint_config: LintConfig | None = None) -> str:
    """Return the studio page with the current snapshot embedded (static mode)."""
    state = UIState(path, lint_config)
    snap, _ = state.snapshot()
    data = {
        "stamp": "static",
        # paths reduce to basenames: a snapshot gets shared, and absolute local
        # paths mean nothing (and say too much) on another machine
        "views": [{**v, "file": Path(v["file"]).name} for v in summaries(snap)],
        "broken": [{**b, "file": Path(b["file"]).name} for b in snap["broken"]],
        "graphs": {
            name: {"file": Path(entry["file"]).name, **entry["payload"]}
            for name, entry in sorted(snap["views"].items())
        },
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    # Every `<` escapes to <. In JSON, `<` can only occur inside strings,
    # where < is the same character — so no spec comment can smuggle a
    # `</script>` or `<!--` that would break out of the island.
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    page = _STATIC.read_text()
    if page.count(_ISLAND) != 1:
        raise RuntimeError("studio page is missing its bootdata island — broken install?")
    return page.replace(_ISLAND, _ISLAND.replace("null", blob, 1), 1)
