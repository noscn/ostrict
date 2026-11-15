# Contributing

## Scope: strict and minimalist, by design

The product here is the *strictness*: deterministic byte-identical SQL, a small
auditable compiler core, and refusals-with-hints instead of best-effort guesses.
Proposals are judged against that bar, so some perfectly good ideas will be
declined. Expect a "no" to:

- anything that makes compilation non-deterministic — session state, clocks,
  randomness, or LLM calls inside the compiler;
- raw-SQL or raw-row escape hatches (passthrough predicates, `SELECT *`,
  unbounded dumps);
- loosening a lint rule instead of fixing the model it caught;
- new back-ends without a maintained test story (dialects already come free via
  sqlglot transpilation; a connector needs real tests like DuckDB's);
- core bloat that belongs in a plugin — `plugins/base.py` hooks exist for RBAC
  filter injection, caching, lineage and audit.

Always welcome: bug reports with a failing fixture, correctness fixes with
tests, new lint rules carrying an SV-R* code, golden questions, documentation.

## The loop

```bash
pip install -e '.[dev]'
ruff check .
pytest -q
strict-semantic lint tests/fixtures/ecommerce.sql
strict-semantic eval tests/fixtures/golden_questions.yaml
```

Every change lands by pull request with green CI; `main` is protected and takes
no direct pushes, maintainers included. A feature needs a test; a behavior
change that departs from the original blueprint needs an entry in
[docs/REVISIONS.md](docs/REVISIONS.md) saying why.

## Security issues

Report vulnerabilities privately through GitHub Security Advisories
(*Security → Report a vulnerability*), not public issues. See also
"Security & deployment responsibility" in the [README](README.md).
