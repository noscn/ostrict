"""Command line: `strict-semantic lint|compile|eval|export|import`."""

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
    return LintConfig(
        security_allowlist=tuple(cfg.get("security_allowlist") or ()),
        no_pk_tables=tuple(cfg.get("no_pk_tables") or ()),
        no_primary_date_tables=tuple(cfg.get("no_primary_date_tables") or ()),
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="strict-semantic")
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

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except CompilerError as e:
        print(f"error [{e.code}]: {e}" + (f"\nhint: {e.hint}" if e.hint else ""), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
