"""Command line: `ostrict lint|compile|eval|export|import|scaffold|ui`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .linter import LintConfig, lint
from .models.errors import CompilerError
from .parsers import load_view, load_views


def _lint_config(path: str | None) -> LintConfig | None:
    if not path:
        return None
    cfg = (yaml.safe_load(Path(path).read_text()) or {}).get("lint") or {}
    kwargs = {}
    if cfg.get("security_deny"):  # extend/replace the default deny-list from config
        kwargs["security_deny"] = tuple(cfg["security_deny"])
    return LintConfig(
        security_allowlist=tuple(cfg.get("security_allowlist") or ()),
        no_pk_tables=tuple(cfg.get("no_pk_tables") or ()),
        no_primary_date_tables=tuple(cfg.get("no_primary_date_tables") or ()),
        **kwargs,
    )


def _views_from(paths: list[str]):
    for path in paths:
        p = Path(path)
        if p.is_dir():
            yield from load_views(p).values()
        else:
            yield load_view(p)


def cmd_lint(args) -> int:
    worst = 0
    for view in _views_from(args.paths):
        report = lint(view, _lint_config(args.config))
        print(report.render())
        if not report.ok:
            worst = 1
    return worst


def cmd_compile(args) -> int:
    from .compiler import compile_semantic_query
    from .models.query import SemanticQueryRequest

    view = load_view(args.view)
    request = SemanticQueryRequest(
        metrics=args.metric,
        dimensions=args.dimension,
        filters=[json.loads(f) for f in args.filter],
        order_by=args.order_by,
        limit=args.limit,
    )
    compiled = compile_semantic_query(view, request, dialect=args.dialect, lint_config=_lint_config(args.config))
    print(compiled.sql)
    return 0


def cmd_eval(args) -> int:
    from .eval.runner import run_eval

    report = run_eval(args.cases, seed=args.seed)
    print(report.render())
    return 0 if report.ok else 1


def cmd_export(args) -> int:
    from .interchange import export_osi

    out = export_osi(load_view(args.view), fmt=args.format, strip_extensions=args.strip_extensions)
    for w in out.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.output:
        Path(args.output).write_text(out.text)
    else:
        print(out.text, end="")
    return 0


def cmd_import(args) -> int:
    from .interchange import import_osi, to_native_yaml

    view = import_osi(Path(args.file), validate=False)
    if not args.no_lint:
        report = lint(view, _lint_config(args.config))
        if report.findings:
            print(report.render(), file=sys.stderr)
        if not report.ok:
            return 1  # ERROR findings refuse the conversion; --no-lint writes it for inspection
    text = to_native_yaml(view)
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")
    return 0


def cmd_scaffold(args) -> int:
    from .connectors import from_config
    from .scaffold import scaffold_view

    connector = None
    if args.connector:
        cfg_path = Path(args.connector).resolve()
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        connector = from_config(cfg.get("connector") or cfg, cfg_path.parent)
    result = scaffold_view(
        from_dbt=args.from_dbt,
        connector=connector,
        models=args.models,
        name=args.name,
        dialect=args.dialect,
        sample_values=args.sample_values,
        catalog=args.catalog,
        lint_config=_lint_config(args.config),
        infer_types=args.infer_types,
    )
    for n in result.notes:
        print(f"note: {n}", file=sys.stderr)
    if not result.report.ok:
        print(result.report.render(), file=sys.stderr)
    if args.output:
        Path(args.output).write_text(result.text)
    else:
        print(result.text, end="")
    return 0 if result.report.ok else 1  # the draft is still written: it is a starting point, not a gate


def cmd_ui(args) -> int:
    if args.export:
        from .ui.export import export_studio_html  # fastapi-free: works without the [ui] extras

        html = export_studio_html(args.path, lint_config=_lint_config(args.config))
        Path(args.export).write_text(html)
        print(f"wrote {args.export} — standalone studio snapshot, opens in any browser with no server")
        return 0
    try:
        import uvicorn

        from .ui.app import create_app
    except ImportError:
        print('Install UI extras with: pip install "ostrict[ui]"', file=sys.stderr)
        return 1
    app = create_app(args.path, lint_config=_lint_config(args.config))
    url = f"http://{args.host}:{args.port}"
    print(f"ostrict ui serving {url}  (read-only — Ctrl+C to stop)")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ostrict")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("lint", help="lint semantic view files or folders (SV-R1..R10)")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--config", default=None, help="strict_semantic.yml whose lint: section to apply")
    sp.set_defaults(fn=cmd_lint)

    sp = sub.add_parser("compile", help="compile a structured request to SQL (no execution)")
    sp.add_argument("view")
    sp.add_argument("-m", "--metric", action="append", default=[])
    sp.add_argument("-d", "--dimension", action="append", default=[])
    sp.add_argument("-f", "--filter", action="append", default=[], help='JSON filter object')
    sp.add_argument("-o", "--order-by", action="append", default=[])
    sp.add_argument("-l", "--limit", type=int)
    sp.add_argument("--dialect", default=None, help="bigquery | duckdb | snowflake | postgres")
    sp.add_argument("--config", default=None, help="strict_semantic.yml whose lint: section to apply")
    sp.set_defaults(fn=cmd_compile)

    sp = sub.add_parser("eval", help="run a Golden Questions YAML deterministically (SV-R10.2)")
    sp.add_argument("cases", help="golden questions YAML file")
    sp.add_argument("--seed", default=None, help="SQL file to seed the DuckDB fixture database")
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("export", help="export a .sql/.yaml view as an OSI (Open Semantic Interchange) document")
    sp.add_argument("view")
    sp.add_argument("--format", choices=("yaml", "json"), default="yaml")
    sp.add_argument("-o", "--output", default=None, help="output file (stdout if omitted)")
    sp.add_argument(
        "--strip-extensions",
        action="store_true",
        help="emit vanilla OSI without custom_extensions; stripped guardrails are warned to stderr",
    )
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("import", help="convert an OSI YAML/JSON file to the native spec (linted by default)")
    sp.add_argument("file")
    sp.add_argument("-o", "--output", default=None, help="output file (stdout if omitted)")
    sp.add_argument("--no-lint", action="store_true", help="skip the SV-R1..R10 gate, for inspection only")
    sp.add_argument("--config", default=None, help="strict_semantic.yml whose lint: section to apply")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("scaffold", help="draft a semantic view YAML from a dbt project or a warehouse schema")
    sp.add_argument("--from-dbt", default=None, help="dbt manifest.json or project directory")
    sp.add_argument("--connector", default=None, help="config YAML whose connector: section to read the schema with")
    sp.add_argument("--models", default=None,
                    help="comma-separated models/tables; dbt mode also takes globs (int_dw_*) and tag:<name>")
    sp.add_argument("--catalog", default=None,
                    help="dbt catalog.json for column types (auto-discovered next to the manifest / in target/)")
    sp.add_argument("--config", default=None,
                    help="ostrict.yml whose lint: section (security_deny/allowlist, exemptions) to apply")
    sp.add_argument("--infer-types", action="store_true",
                    help="draft untyped *At/_at/*Date/_date columns as time dimensions (labeled in the header)")
    sp.add_argument("--name", default=None, help="semantic view name (default: project or schema name)")
    sp.add_argument("--dialect", default=None, help="bigquery | duckdb | snowflake | postgres")
    sp.add_argument(
        "--sample-values",
        action="store_true",
        help="sample low-cardinality text columns into 'Values:' (SELECT DISTINCT ... LIMIT 21; off = zero scan)",
    )
    sp.add_argument("-o", "--output", default=None, help="output file (stdout if omitted)")
    sp.set_defaults(fn=cmd_scaffold)

    sp = sub.add_parser("ui", help="serve the local read-only visual studio (join DAG + linter overlay)")
    sp.add_argument("path", nargs="?", default=None,
                    help="a view file, a folder of views, or an ostrict.yml config "
                         "(default: $OSTRICT_CONFIG or ./ostrict.yml)")
    sp.add_argument("--config", default=None, help="ostrict.yml whose lint: section to apply")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8501)
    sp.add_argument("--no-browser", action="store_true", help="do not auto-open the browser")
    sp.add_argument("--export", default=None, metavar="OUT_HTML",
                    help="write a standalone HTML snapshot (data embedded, opens from disk, "
                         "no [ui] extras needed) and exit instead of serving")
    sp.set_defaults(fn=cmd_ui)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except CompilerError as e:
        print(f"error [{e.code}]: {e}" + (f"\nhint: {e.hint}" if e.hint else ""), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
