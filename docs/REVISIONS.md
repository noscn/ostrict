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
