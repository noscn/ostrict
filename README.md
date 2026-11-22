# ostrict

[![CI](https://github.com/noscn/ostrict/actions/workflows/ci.yml/badge.svg)](https://github.com/noscn/ostrict/actions/workflows/ci.yml)

**A strict CI linter for semantic views, and a deterministic, FinOps-safe
semantic compiler + MCP server for AI data agents.**

*Ostrict* = **O**SI + **strict**. Unlike the myth, Ostrict never buries its
head in the sand when AI hallucinates SQL.

Most data warehouses have no native, strictly-enforced semantic views. When
text-to-SQL agents query them through loose YAML or flexible graph layers,
the failure modes are always the same: non-deterministic SQL, fan-out double
counting, ambiguous join paths, raw-row dumps, and runaway scan costs.

This project makes the semantic model a **blueprint, never a materialization**:

```
.sql DDL / .yaml spec ──parse──▶ canonical AST (Pydantic v2)
        ──lint (SV-R1..R10)──▶ valid blueprint
        ──compile(request)──▶ one deterministic SQL statement ──connector──▶ rows
```

- **Dual input, one AST.** `CREATE SEMANTIC VIEW` DDL (human-authored) and a
  native YAML contract (automation-friendly) build byte-identical models.
- **Static ground rules (SV-R1..R10).** Primary keys and grain comments, a
  directed many-to-one join DAG with exactly one path between any two tables,
  no numeric dimensions, no `CURRENT_DATE`, snapshot metrics declared
  `NON ADDITIVE BY`, globally unique field names and synonyms, a security
  deny-list, comment templates. `ostrict lint` blocks CI on ERRORs.
- **Deterministic compilation.** Same request, same SQL, byte for byte. Joins
  only walk child→parent (zero fan-out, enforced); metrics of different grains
  drill across via `UNION ALL`; derived ratios divide NULL-safely; every
  expression is built as a sqlglot AST — no string concatenation anywhere.
- **FinOps guardrails.** At least one metric per query (no raw dumps),
  partition filters required and pushed into each table's subquery (the only
  placement BigQuery honors on joined tables), `LIMIT` injected and capped,
  BigQuery dry-run byte ceiling.
- **MCP server (mcp ≥ 2).** Five tools: `list_semantic_views`,
  `inspect_semantic_view`, `get_dimension_values`, `compile_semantic_query`,
  `execute_semantic_query`. The agent picks names from the catalog and never
  writes SQL; every refusal carries an error code and a repair hint.
- **Golden Questions eval.** `ostrict eval cases.yaml` grades
  answer/refuse/clarify cases against DuckDB deterministically — no LLM judge.
- **Scaffolding.** `ostrict scaffold --from-dbt <project>` (or `--connector`
  for a live schema) drafts a view that passes SV-R1..R10 on its first lint:
  deny-listed columns dropped, keys kept out of dimensions, colliding names
  prefixed, the join graph kept to a forest — everything else lands as
  `# TODO` comments for a human.
- **OSI interchange.** `ostrict export|import` converts views to and
  from Open Semantic Interchange YAML/JSON losslessly — guardrails travel in a
  namespaced `custom_extensions.strict_semantic` block, and every import must
  pass the full SV-R1..R10 gate.
- **Open-core plugin hooks.** `on_model_loaded`, `before_compile` (RBAC filter
  injection), `after_compile` (cache/lineage), `on_query_executed` (audit).

## Quickstart

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev,mcp]'
.venv/bin/python -m pytest                                  # 87 tests
.venv/bin/ostrict lint tests/fixtures/ecommerce.sql
.venv/bin/ostrict eval tests/fixtures/golden_questions.yaml
.venv/bin/ostrict compile tests/fixtures/ecommerce.yaml \
    -m revenue -m aov -d region \
    -f '{"field":"order_date","op":">=","value":"2026-01-01"}' \
    -f '{"field":"order_date","op":"<","value":"2026-02-01"}' --dialect bigquery
```

Run the MCP server against the demo warehouse (in-memory DuckDB):

```bash
OSTRICT_CONFIG=$PWD/examples/config.duckdb.yml .venv/bin/ostrict-mcp
```

or register it with Claude Code:

```bash
claude mcp add ostrict -e OSTRICT_CONFIG=$PWD/examples/config.duckdb.yml -- $PWD/.venv/bin/ostrict-mcp
```

For BigQuery, copy `examples/config.bigquery.example.yml`, point `views:` at
your private spec folder, and set `max_bytes`.

## Writing a semantic view

The `CREATE SEMANTIC VIEW` DDL (`.sql`) and the native YAML (`.yml`) are
equivalent; metadata the DDL has no slot for travels in structured comment
directives (enforced by the linter as SV-R8.1 templates):

```sql
CREATE SEMANTIC VIEW ecommerce
  TABLES (
    orders AS shop.orders PRIMARY KEY (order_id)
      COMMENT = 'One row per order. Primary date: order_date. Partition: order_date BY order_date. Source: shop.orders.',
    customers AS shop.customers PRIMARY KEY (customer_id)
      COMMENT = 'One row per customer. Primary date: signup_date. Source: shop.customers.'
  )
  RELATIONSHIPS ( orders_customers AS orders (customer_id) REFERENCES customers (customer_id) )
  DIMENSIONS (
    customers.region AS region WITH SYNONYMS ('market') COMMENT = 'Customer home region. Values: EU, US, APAC.',
    orders.order_date AS order_date COMMENT = 'Date the order was placed. Grains: day, week, month.'
  )
  METRICS (
    orders.order_count AS COUNT(order_id) COMMENT = 'Number of orders placed. Population: all orders. Additivity: additive.'
  )
  COMMENT = 'Retail sales.'
```

Directives: `One row per …` (grain), `Primary date: col.`,
`Point-in-time snapshot.`, `Partition: col BY dimension (lookback Nd, lookahead Nd).`,
`Grains: day, month.`, `Type: timestamp.`, `Values: a, b, c.`
See `tests/fixtures/ecommerce.{sql,yaml}` for the full twin fixtures.

## Scaffolding a view

Don't start from a blank file — draft one from what you already have:

```bash
ostrict scaffold --from-dbt path/to/dbt_project --models orders,customers -o views/draft.yml
ostrict scaffold --connector config.duckdb.yml --models shop.orders,shop.customers --sample-values
```

dbt mode reads `manifest.json` or the project directory — descriptions,
`unique` tests as primary keys, `relationships` tests as joins,
`accepted_values` tests as `Values:` enumerations, `partition_by` as a
conservative partition hint, and real column types from dbt's `catalog.json`
(auto-discovered, or `--catalog`) — never touching the warehouse. `--models`
takes names, globs (`int_dw_*`) and dbt tags (`tag:core`); `--config` applies
your lint config (deny-list, allow-list, exemptions) at generation time; the
opt-in `--infer-types` drafts untyped `*At`/`*_at`/`*Date` columns as time
dimensions, labeled as inferences. Engineers can pin decisions in dbt with
`meta: {semantic: {kind: fact|dimension|skip, values: [...]}}`, so a
regenerated scaffold keeps their tuning. Connector mode reads
`INFORMATION_SCHEMA` instead and guesses keys and joins from `*_id` naming;
`--sample-values` additionally runs a bounded `SELECT DISTINCT … LIMIT 21`
per text column. Every keyed table gets a `COUNT` metric, sequence-like
columns get no sample `SUM`, and columns that merely look like personal data
are kept but flagged `TODO(PII-check)`. The draft is linted at generation
time; anything the ground rules would reject — a second join path, a
deny-listed column — becomes a `# TODO` comment instead, name-matched foreign
keys become paste-ready candidate-join snippets, and `ostrict scaffold`'s
output is accepted by `ostrict lint` with zero errors.

## Querying

```json
{"metrics": ["revenue", "aov"], "dimensions": ["region", "order_date__month"],
 "filters": [{"field": "order_date", "op": "between", "value": ["2026-01-01", "2026-03-31"]},
             {"field": "aov", "op": ">", "value": 50}],
 "order_by": ["-revenue"], "limit": 100}
```

Time dimensions take a `__grain` suffix. Filters on dimensions apply before
aggregation, filters on metrics after. Dimension-only queries are refused
(`ERR_METRIC_REQUIRED`) — value discovery goes through `get_dimension_values`,
a bounded `SELECT DISTINCT … LIMIT ≤ 50`.

## OSI interchange

Exchange views with other semantic-layer tools as OSI (Open Semantic
Interchange) documents:

```bash
ostrict export tests/fixtures/ecommerce.sql -o ecommerce.osi.yaml
ostrict export tests/fixtures/ecommerce.yaml --format json --strip-extensions
ostrict import ecommerce.osi.yaml -o views/imported.yaml
```

Exports are lossless: strict-semantic guardrails (dialect, partition pruning
bounds, snapshot flags, time types) ride in `custom_extensions.strict_semantic`
blocks, and an export → import round-trip rebuilds an equal AST.
`--strip-extensions` emits vanilla OSI instead and warns on stderr for every
guardrail that drops out. Imports are gated: the document is linted
(SV-R1..R10) before it is written as a native spec, so external models cannot
bypass the ground rules (`--no-lint` converts anyway, for inspection). The
field-by-field mapping lives in [docs/osi-mapping.md](docs/osi-mapping.md).

## Layout

```
src/ostrict/
├── models/        # canonical AST, error codes, request/response contracts
├── parsers/       # CREATE SEMANTIC VIEW DDL (token-stream) + native YAML -> one builder
├── interchange/   # OSI (Open Semantic Interchange) export/import adapter
├── scaffold/      # draft views from dbt metadata or INFORMATION_SCHEMA
├── linter/        # SV-R1..R10 engine: structure / logic / governance rules
├── compiler/      # join-graph resolver, FinOps guardrails, sqlglot SQL builder
├── connectors/    # DuckDB, BigQuery (dry-run + byte cap), protocol
├── plugins/       # open-core lifecycle hooks
├── eval/          # Golden Questions runner (deterministic, no LLM)
└── mcp_server/    # the five MCP tools over stdio
```

Design departures from the original blueprint, with reasons, are in
[docs/REVISIONS.md](docs/REVISIONS.md).

## Security & deployment responsibility

The server speaks **stdio** and is designed to run next to the agent, on your
machine, with your credentials. If you expose it over a network (HTTP/SSE,
a gateway, a shared host), authentication, transport security and tenant
isolation are yours to provide — nothing here does that for you.

Expect defense in depth from the warehouse side too: run the connector with a
**least-privilege, read-only** account scoped to the datasets the views name.
The compiler never concatenates SQL strings — filter values become typed
sqlglot literals, operators come from a fixed whitelist, and every identifier
must resolve against the linted model — but the SQL that reaches your warehouse
should still be gated by the warehouse's own permissions. Per the Apache 2.0
license, the software is provided as is, without warranty; securing the
deployment is the deployer's responsibility.

Report vulnerabilities privately via GitHub Security Advisories, not public
issues.

## Contributing

Scope is deliberately strict and minimal — read
[CONTRIBUTING.md](CONTRIBUTING.md) before proposing features. Short version:
determinism is non-negotiable, escape hatches get declined, and extensions
belong in plugins.

## Renamed from strict-semantic-mcp

v0.3.0 renamed the project to **ostrict**. Old integrations keep working:
`import strict_semantic` is a deprecated alias of `ostrict` (same module
objects), the `strict-semantic` / `strict-semantic-mcp` entry points remain,
and `STRICT_SEMANTIC_CONFIG` is honored when `OSTRICT_CONFIG` is unset. The
OSI extension namespace stays `custom_extensions.strict_semantic`, so 0.2.x
documents import unchanged. On PyPI, new releases ship as `ostrict`;
`strict-semantic-mcp` stays at 0.2.0.

## License

Apache 2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). Fixtures and examples
are a generic e-commerce schema; keep your company's semantic views in a
private repository and point the server at them via `OSTRICT_CONFIG`.
