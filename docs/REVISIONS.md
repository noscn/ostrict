# Blueprint revisions — v5.0 → v5.1 (as built)

The v5.0 blueprint was implemented in full, with the deviations below. Each was
forced by a verified fact or a contradiction inside the blueprint; everything
else (architecture, rule set, error codes, directory layout, milestones, golden
tests) is as specified.

## Corrections of fact

1. **"Apache Ossie" could not be verified to exist.** No such Apache project was
   found; the name resembles OSI (Open Semantic Interchange). Shipping a parser
   claiming compatibility with an unverifiable spec would be wrong, so the
   secondary input is this project's own documented YAML contract
   (`parsers/yaml_spec.py`). It carries exactly the DDL's information, satisfies
   the dual-input acceptance test (identical ASTs by construction — both parsers
   feed one builder), and an adapter for a real interchange format can map onto
   the same raw shape once its spec is pinned down.
2. **`FastMCP` was renamed.** In mcp 2.x the server class is
   `mcp.server.mcpserver.MCPServer`, and anticipated failures must be raised as
   `ToolError` or the agent sees only "Error executing tool …". The server wraps
   every refusal as `ToolError` carrying `[ERR_CODE] message | hint`.
   Transports: stdio today; mcp 2.x replaced SSE with streamable-http
   (`server.run("streamable-http")`) when a networked deployment is wanted.
3. **sqlglot (verified on 30.31.0) has no grammar for `CREATE SEMANTIC VIEW`.**
   The DDL parser therefore walks *sqlglot's own token stream* (never regexes
   over SQL) to split the five section lists, and hands every expression back to
   sqlglot for parsing/validation — honoring the blueprint's intent.

## Contradictions resolved (strictness wins)

4. **Multi-base-table queries were unspecified.** Metrics whose base tables
   differ are each aggregated at their own grain and combined by
   `UNION ALL + GROUP BY + MAX` (drill-across). `FULL OUTER JOIN` was rejected
   because NULL dimension values (e.g. orphan rows) never match in a join key
   but do group — the UNION form is the one that stays correct.
5. **Filters must be reachable from *every* requested metric's base table**
   (strict reading of SV-R3.4). Consequence: `customer_count` cannot share a
   request with an `order_date` filter — the compiler refuses rather than
   silently applying different filters to different metrics. Agents split such
   requests; the refusal says so.
6. **SV-R2.1's "event-only leaf" exemption** is lint configuration
   (`LintConfig.no_pk_tables`); it waives the PRIMARY KEY, never the grain comment.
7. **`ERR_EXCESSIVE_LIMIT`** now means: a default `LIMIT` (1000) is always
   injected, and a requested limit above the configured cap is *refused* —
   v5.0 described silent injection and an error code without saying when it fired.

## Hardening from production use (BigQuery)

8. **Partition pruning is first-class.** A table may declare
   `Partition: <column> BY <dimension> (lookback Nd, lookahead Nd).` — filters
   on that (view-wide unique) dimension are converted into predicates on the
   physical partition column and **pushed into the table's own subquery**, the
   only placement BigQuery honors for required partition filters on joined
   tables. The guardrail demands a *lower* bound (`=`, `>=`, `>`, `between`);
   an upper bound alone does not bound a scan. Grained equality filters widen
   the upper bound by the grain length, so pruning windows are always supersets.
9. **Snapshot semantics (SV-R5.7 / SV-R7)** are enforced as: snapshot metrics
   must declare `NON ADDITIVE BY (<primary date>)` at lint time, the snapshot
   table must expose that date as a time dimension, and every query against it
   must either pin it (`= 'YYYY-MM-DD'`) or group by it (plain or day grain).
10. **The BigQuery connector dry-runs first** and refuses anything over
    `max_bytes` (`ERR_COST_LIMIT`), then executes with `maximum_bytes_billed`
    as a hard server-side cap.

## Additions the blueprint implied but did not specify

11. Comment directives are the machine-readable form of SV-R8.1's templates:
    `One row per …`, `Primary date: col.`, `Point-in-time snapshot.`,
    `Partition: …`, `Grains: day, month.`, `Type: timestamp.`, `Values: a, b.`
    The YAML may use explicit keys instead; when both appear they must agree.
12. Kept from the field-proven predecessor: table-level always-on `filter`
    predicates (YAML), post-aggregation metric filters, `order_by`, synonyms,
    NULL-safe derived-metric division (`CAST AS DOUBLE / NULLIF(x, 0)`), and
    `__grain` suffixes on time dimensions (the replacement for the calendar-part
    dimensions SV-R6.6 warns about).
13. SV-R4.1 numeric detection is split: provably numeric expressions are an
    ERROR; suspicious *names* (static analysis cannot know column types) are a WARN.
14. Error codes added beyond v5.0's list: `ERR_PARSE`, `ERR_UNKNOWN_TABLE`,
    `ERR_UNKNOWN_FIELD` (with did-you-mean suggestions), `ERR_UNKNOWN_VIEW`,
    `ERR_INVALID_FILTER`, `ERR_INVALID_METRIC`, `ERR_COST_LIMIT`.

## Status vs. the milestone acceptance criteria

- M1 dual parsers: identical ASTs and identical compiled SQL — passing.
- M2 linter: every rule has a unit test asserting its exact code — passing.
- M3 compiler: 100× byte-identical SQL; zero fan-out proven on DuckDB against
  raw-SQL ground truth (including a 100-order randomized dataset); downward
  fan-out, missing partition filters, raw dumps and oversized limits refused —
  passing.
- M4: MCP server answers all five tools over stdio (tested end-to-end as a
  subprocess); `strict-semantic eval` passes 9/9 golden questions
  deterministically; BigQuery connector implements dry-run estimation.
- Section 9's ten golden tests exist under their exact names in
  `tests/test_golden.py` — all passing. Full suite: 63 tests, ruff clean.

# v0.2.0 — OSI interchange (docs/osi-mapping.md)

The bidirectional OSI adapter implements its blueprint as specified, with
three clarifications:

15. **The OSI field names are this project's own pinning.** The public Open
    Semantic Interchange spec still has no published schema to verify against
    (see item 1), so `interchange/osi.py` implements the contract documented in
    `docs/osi-mapping.md`. Strict-semantic guardrails travel
    in namespaced `custom_extensions.strict_semantic` blocks at the document,
    dataset and field level; `--strip-extensions` produces vanilla OSI plus a
    warning for every guardrail that drops out.
16. **Facts and dimensions regroup by dataset on round-trip.** OSI nests
    fields under datasets, so a view whose facts/dimensions interleave across
    tables comes back regrouped in table order (order within a table is
    preserved). Compilation is unaffected — resolution is by name — and the
    twin fixtures round-trip to byte-identical ASTs.
17. **`values` reconciles outside `build_view`.** The shared raw shape has no
    explicit enumeration key (the native format expresses it only as a
    `Values:` directive), so OSI `fields[].values` apply after construction
    with `_merge`'s agree-or-fail semantics, and `to_native_yaml` appends the
    directive when only the OSI field carried the enumeration.

# v0.3.0 — renamed to ostrict

The project and package were renamed from `strict-semantic-mcp` /
`strict_semantic` to **ostrict** (**O**SI + **strict**; unlike the myth,
Ostrict never buries its head in the sand when AI hallucinates SQL).
Compatibility was kept deliberately:

18. **`import strict_semantic` still works.** A shim package re-exports the
    public API, and a meta-path finder aliases every `strict_semantic.<module>`
    to the *same* `ostrict.<module>` object (one copy of every class, so
    isinstance and equality behave), raising one `DeprecationWarning`.
19. **Entry points and config.** `strict-semantic` / `strict-semantic-mcp`
    remain as aliases of `ostrict` / `ostrict-mcp`, and
    `STRICT_SEMANTIC_CONFIG` is honored when `OSTRICT_CONFIG` is unset; the
    default config file is now `./ostrict.yml`.
20. **The OSI extension namespace stays `custom_extensions.strict_semantic`**,
    so documents exported by 0.2.x import unchanged — renaming it would be a
    format break for a cosmetic win. On PyPI, new releases ship as `ostrict`;
    `strict-semantic-mcp` stays at 0.2.0.

# v0.4.0 — `ostrict scaffold`

Draft generation from dbt metadata or a live `INFORMATION_SCHEMA`, built so
the draft passes SV-R1..R10 at generation time:

21. **The join graph is forest-ified, never repaired.** Candidate
    relationships are accepted first-come while they connect new components
    (union-find); a self-reference or a second path between two tables moves
    into a `# TODO` comment with a paste-ready snippet (SV-R3.1 / SV-R3.5),
    and a relationship onto a partial parent key is skipped the same way
    (SV-R3.2).
22. **Heuristics are labeled, never hidden.** Connector mode guesses primary
    keys by name convention (`<singular>_id`, `id`, else the first column
    with a note) and infers joins only onto confidently-keyed parents; every
    guess lands in the TODO header and on stderr.
23. **`scaffold` writes drafts, `import` gates models.** A draft that still
    fails lint (say, a table with no date column) is written anyway, with the
    report on stderr and exit 1 — it is a starting point for a human — while
    `ostrict import` keeps refusing to write an invalid conversion.

# v0.4.1 — scaffold hardening (from a real 171-model dry run)

Running the scaffold against a large camelCase dbt project with no declared
column types shaped these fixes:

24. **Types come from dbt's own artifacts, not guesses.** `catalog.json`
    (`dbt docs generate`) is auto-discovered next to the manifest or under
    `target/` and fills every column type the yml leaves out — zero scanning;
    an explicit `data_type` still wins. The opt-in `--infer-types` covers
    projects without a catalog by drafting `*At`/`*_at`/`*Date`/`*_date`
    columns as time dimensions, each inference counted in the header.
25. **Declared knowledge is reused before anything is sampled.**
    `accepted_values` tests and `meta: {semantic: {values: [...]}}` become
    `Values:` directives in the author's order, and `meta.semantic.kind`
    (fact | dimension | skip) pins classification, so a regenerated scaffold
    keeps an engineer's tuning.
26. **Flag, don't silently pass.** `--config` applies the project's lint
    config at generation time (`security_deny` is now configurable alongside
    the allow-list and exemptions); PII-looking columns that survive the
    deny-list are kept but flagged `TODO(PII-check)`; camelCase/`*_id`
    foreign keys nobody declared become paste-ready candidate-join snippets
    instead of silent orphans; dev-looking schemas (`dev_*`, `sandbox_*`) are
    called out; sequence-like numerics get a FACT but no sample SUM, while
    every keyed table gets a COUNT metric.

# v0.5.0 — `ostrict ui`

27. **The studio is a viewer, never an editor.** `ostrict ui` (optional
    `[ui]` extra: FastAPI + uvicorn) serves one self-contained HTML page —
    no Node.js, no build step, pure SVG — with zero endpoints that write to
    `.yaml`/`.sql` files, no SQL runner and no data grid. Broken specs still
    render, with every lint finding attached to the node or edge it concerns
    (SV-R3.5 multi-path findings land on the child table, where the resolver
    reports them), because debugging broken models is the point. Hot reload
    reuses the MCP server's `(mtime_ns, size)` fingerprint pattern; files
    that fail to parse are listed with their `ERR_PARSE` message instead of
    taking the studio down.

# v0.5.1 — external audit hardening (F1–F8)

28. **One filter-expansion path, exact answers at the edges.** An external
    code-level audit of v0.5.0 produced eight findings; all are fixed. The
    big one (F1/F4): `get_dimension_values` expanded always-on table filters
    with a snapshot loop, so a filter chain (A's filter reads B, B's filter
    reads C) silently dropped C's restriction, and pulled-in tables skipped
    the child→parent reachability guard. Aggregate and dimension-value
    queries now share one queue-based `_expand_table_filters`, so the
    sanctioned lookup path can never drift from the main one again. Also:
    MCP results keep DECIMAL/NUMERIC values exact as digit strings instead
    of rounding through float (F2); schema-mode scaffolding keys drafts by
    `(schema, table)` and renames duplicates instead of merging two
    same-named tables into one draft (F3); `truncated` is now a fact — the
    executed statement overfetches one row past LIMIT while the reported
    SQL keeps the requested limit (F5); an `in` filter mixing
    timezone-aware and naive date/times is refused as `ERR_INVALID_FILTER`
    instead of crashing with a `TypeError` (F6); `on_model_loaded`
    memoization lives outside the plugin instance, so `__slots__` plugins
    work (F7); and `max_rows=0` now means zero rows, with negative values
    refused (F8).

# v0.5.2 — re-audit fixes (F9, F10)

29. **Fixes hold up under their own audit.** The re-audit of v0.5.1 confirmed
    all eight fixes and caught two subtleties the fixes themselves introduced.
    F9: the scaffold's PK heuristic ran on the renamed logical name, so a
    cross-schema `s1_orders` hunted for `s1_order_id` and fell back to the
    first column — it now always sees the physical table name, and when a
    foreign key matches confident parents in several schemas, the child's own
    schema wins instead of dropping the relationship as ambiguous (a lone
    cross-schema match still links). F10: the memo for plugins that cannot be
    weakly referenced was id-keyed forever — a recycled `id()` could hand a
    new plugin an old memo and silently skip its `on_model_loaded`. It is now
    a small LRU (64) that holds the instance strongly, which makes id reuse
    impossible while an entry lives and caps the cost; an evicted plugin just
    gets the callback again. Also hardened, from the re-audit's margin note:
    when an `after_compile` plugin rewrites the SQL so no LIMIT is left, the
    executed query is now capped at the connector (`max_rows`) with the
    request's own limit and `truncated` stays exact, instead of fetching
    unbounded rows and guessing.

# v0.5.3 — re-audit fix (F11)

30. **A generator of plugins hooks everything, not just the first pass.**
    `compile_semantic_query` and `compile_dimension_values` iterate `plugins`
    several times (model-loaded notification, `before_compile`,
    `after_compile`), so a one-shot iterator was consumed by the first pass:
    `on_model_loaded` fired while every later hook was silently skipped —
    dangerous when a plugin injects policy filters the caller believes are
    active. All three public entry points now materialize `plugins` once up
    front (`execute_semantic_query` already did), with generator regression
    tests for each.

# v0.5.4 — studio readability on large views

31. **A 20-table view must read as a diagram, not as spaghetti.** The studio's
    layered layout now runs barycenter ordering sweeps (each column reordered
    by the mean position of its neighbors, alternating direction), so tables
    that share parents sit together and edge crossings collapse. Edges attach
    through per-connection anchor fans and staggered horizontal cubic Beziers
    instead of one shared point, so parallel joins separate instead of
    flattening into a band; join labels moved off the canvas and into a
    hover/selection tooltip (`fk → pk` on a readable background). Hovering or
    selecting a table dims everything outside its ancestor/descendant lineage
    to 0.18 opacity. Cards gained a compact mode (header, role, finding flag,
    field counts) that turns on automatically at 8+ tables, with a header
    toggle back to the detailed field lists. The sidebar's derived-metrics and
    view-level-findings sections are collapsible accordions that auto-collapse
    past 5 items, and the sidebar itself toggles away for a full-canvas
    diagram. A sidebar search (`/`) finds any table, fact, dimension or metric
    — a derived metric resolves recursively to the home tables of the base
    metrics it references — and focuses the canvas on just that field's
    lineage subgraph, colibri-style: unrelated tables leave the layout
    entirely, the subgraph is relaid out and refitted, the focus chip shows
    the field's definition (its expression and doc comment) with the
    referenced base fields highlighted inside their table cards — kept
    visible even past the "+N more" fold — and the chip's ✕ (or Esc)
    restores the full view. Still one self-contained HTML file: no
    external libraries, same endpoints, same 1.5 s hot-reload poll.

# v0.5.5 — audit round 4: F11 proven end to end

32. **A policy filter injected by a generator-supplied plugin reaches the
    rows.** Round 4 re-raised F11 and the CI asks against an older snapshot:
    re-verification shows every public entry point has materialized
    ``plugins`` since v0.5.3 (the compiler's three entry points are the only
    plugin consumers in the package — the MCP server takes none), and the CI
    version matrix (3.10–3.13) with per-module coverage landed separately.
    The genuinely missing piece was an end-to-end regression: the v0.5.3
    generator tests assert hook order only, never that ``before_compile``'s
    rewritten request constrains execution. A plugin passed as a one-shot
    generator now proves all four lifecycle hooks fire and that its injected
    RBAC filter (``region = 'EU'``) actually gates the rows DuckDB returns —
    the exact silent-skip scenario that made F11 dangerous.
