"""What the studio watches and serves — shared by the server and the exporter.

``UIState`` resolves a path (a view file, a folder of views, or an ostrict.yml
config) and keeps a parsed snapshot behind the same ``(mtime_ns, size)``
fingerprint cache the MCP server uses, so both the live server and a one-shot
export read the specs identically. Deliberately fastapi-free: `--export`
works on a core install without the ``[ui]`` extras.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

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


def summaries(snap: dict) -> list[dict]:
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
