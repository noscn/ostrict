# Architecture Blueprint: Bidirectional OSI (Open Semantic Interchange) Adapter (`v0.2.0`)

**Project:** `strict-semantic-mcp`  
**Target Version:** `v0.2.0`  
**Objective:** Add lossless, deterministic bidirectional conversion (`export` and `import`) between the internal canonical `SemanticView` AST and the Open Semantic Interchange (OSI / Ossie) JSON/YAML specification—without altering the project's existing `.sql` DDL or native `.yaml` input contracts.

---

## 1. Architectural Principle: Canonical Core, Pluggable Interchange

The project's internal Pydantic AST (`src/strict_semantic/models/ast.py`) and its native input formats (`.sql` DDL and native `.yaml`) remain **100% unchanged and backward-compatible**. 

External interchange formats like OSI are implemented as a dedicated adapter layer under `src/strict_semantic/interchange/`:

```text
[External OSI YAML/JSON] ──import──▶ [Canonical SemanticView AST] ──export──▶ [External OSI YAML/JSON]
                                                │
                                   (Lint SV-R1..R10 & Compile)
                                                │
                                                ▼
                                   [Deterministic BigQuery SQL]
```

### Core Guarantees
1. **Zero Breaking Changes:** Existing `.sql`, `.yaml`, CLI flags, and MCP tool signatures remain untouched.
2. **Strict Import Gate:** Any external OSI file imported into `strict-semantic-mcp` must pass the full `SV-R1..R10` linter (`ensure_valid`) before it can be compiled or queried.
3. **Lossless Round-Trip via `custom_extensions`:** Standard OSI fields map cleanly to `SemanticView` fields. `strict-semantic-mcp`-specific guardrails (such as `Partition` lookback/lookahead bounds, `snapshot` flags, `time_type`, and `ai_sql_generation`) are preserved inside a namespaced `custom_extensions.strict_semantic` block upon export and restored upon import.

---

## 2. Module Layout & New Files

Add the following files to `src/strict_semantic/`:

```text
src/strict_semantic/
├── interchange/
│   ├── __init__.py          # Public API: export_osi, import_osi
│   └── osi.py               # Bidirectional AST <-> OSI dictionary/YAML/JSON mapper
tests/
└── test_interchange_osi.py  # Unit & round-trip parity tests
```

---

## 3. Data Mapping Specification (`SemanticView` AST ↔ OSI Schema)

### 3.1 Top-Level Mapping
| Canonical AST (`SemanticView`) | OSI Specification Field | Notes / Fallback |
| :--- | :--- | :--- |
| `view.name` | `semantic_model.name` | Required string. |
| `view.comment` | `semantic_model.description` | Maps 1:1. |
| `view.dialect` | `custom_extensions.strict_semantic.dialect` | Defaults to `"snowflake"` if absent on import. |
| `view.ai_sql_generation` | `semantic_model.ai_context.instructions` | Also mirrored in `custom_extensions.strict_semantic.ai_sql_generation`. |

### 3.2 Datasets (`tables` ↔ `datasets`)
| Canonical AST (`LogicalTable`) | OSI Specification Field | Notes / Fallback |
| :--- | :--- | :--- |
| `table.name` | `datasets[].name` | Logical table alias. |
| `table.source` | `datasets[].source` | Physical table reference or inline SQL `SELECT`. |
| `table.primary_key` | `datasets[].primary_key` | List of column names (`tuple[str, ...]`). |
| `table.comment` | `datasets[].description` | Must preserve `One row per ...` and `Primary date: ...` directives. |
| `table.primary_date` | `custom_extensions.strict_semantic.primary_date` | Also parsed from `description` directives on import. |
| `table.snapshot` | `custom_extensions.strict_semantic.snapshot` | Boolean; also parsed from `Point-in-time snapshot.` directive. |
| `table.partition` | `custom_extensions.strict_semantic.partition` | Dict `{column, dimension, lookback_days, lookahead_days}`. |
| `table.filter` | `datasets[].filter` (or `custom_extensions`) | Always-on row predicate. |

### 3.3 Relationships (`relationships` ↔ `relationships`)
| Canonical AST (`Relationship`) | OSI Specification Field | Notes |
| :--- | :--- | :--- |
| `rel.name` | `relationships[].name` | Relationship identifier. |
| `rel.child` | `relationships[].from_dataset` | Many-side (child) table name. |
| `rel.child_columns` | `relationships[].from_columns` | List of foreign key columns. |
| `rel.parent` | `relationships[].to_dataset` | One-side (parent) table name. |
| `rel.parent_columns` | `relationships[].to_columns` | List of primary key columns. |

### 3.4 Facts & Dimensions (`facts`, `dimensions` ↔ `datasets[].fields`)
In OSI, facts and dimensions live under each dataset's `fields` array with a `kind` discriminator (`"fact"` vs `"dimension"`):

* **Facts (`Fact`):**
  * `name` ↔ `fields[].name`
  * `kind` = `"fact"`
  * `expr` ↔ `fields[].expression`
  * `comment` ↔ `fields[].description`
* **Dimensions (`Dimension`):**
  * `name` ↔ `fields[].name`
  * `kind` = `"dimension"`
  * `expr` ↔ `fields[].expression`
  * `comment` ↔ `fields[].description`
  * `synonyms` ↔ `fields[].synonyms`
  * `grains` ↔ `fields[].time_grains` (if non-empty, marks a time dimension)
  * `time_type` ↔ `custom_extensions.strict_semantic.time_type` (`"date"` or `"timestamp"`)
  * `values` ↔ `fields[].values`

### 3.5 Metrics (`metrics` ↔ `metrics`)
| Canonical AST (`Metric`) | OSI Specification Field | Notes |
| :--- | :--- | :--- |
| `metric.name` | `metrics[].name` | Globally unique metric name. |
| `metric.table` | `metrics[].dataset` | `None` for derived metrics; table name for base metrics. |
| `metric.expr` | `metrics[].expression` | Canonical SQL expression. |
| `metric.comment` | `metrics[].description` | Preserves `Population:` and `Additivity:` templates. |
| `metric.synonyms` | `metrics[].synonyms` | List of unique synonyms. |
| `metric.non_additive_by` | `metrics[].non_additive_by` | Snapshot date column name (if applicable). |

---

## 4. Import Validation & Guardrail Rules

When importing an external OSI file (`import_osi`):
1. **Strict Parsing:** Reject malformed YAML/JSON or missing required OSI keys (`semantic_model.name`, `datasets`) with `CompilerError(ERR_PARSE, ...)`.
2. **Directive Reconciliation:** If a field is specified both in `custom_extensions.strict_semantic` and via a `description` comment directive (e.g., `Primary date: col.`), they must agree—otherwise raise `ERR_PARSE` (matching `parsers/common.py`'s `_merge` behavior).
3. **Mandatory Linting:** By default, `import_osi(..., validate=True)` runs `ensure_valid(view)` so imported OSI models cannot bypass `SV-R1..R10` (e.g., ambiguous multi-paths, numeric dimensions, or missing primary keys).
4. **Export Warning on Lossy Stripping:** Provide an optional flag `strip_extensions: bool = False` on `export_osi`. When `strip_extensions=True`, omit `custom_extensions` to produce a pure vanilla OSI document, and return a list of warning strings detailing which `strict-semantic` guardrails (like partition lookback bounds) were stripped.

---

## 5. CLI Additions (`src/strict_semantic/cli.py`)

Add two new subcommands to the `strict-semantic` CLI:

### 5.1 `strict-semantic export`
Exports a `.sql` DDL or native `.yaml` semantic view into OSI format:
```bash
strict-semantic export tests/fixtures/ecommerce.sql --format yaml -o ecommerce.osi.yaml
strict-semantic export tests/fixtures/ecommerce.yaml --format json --strip-extensions
```
* `--format`: `yaml` (default) or `json`.
* `-o / --output`: Output file path (prints to `stdout` if omitted).
* `--strip-extensions`: Omit `custom_extensions.strict_semantic` and print stripped guardrail warnings to `stderr`.

### 5.2 `strict-semantic import`
Converts an external OSI `.yaml` or `.json` file into `strict-semantic-mcp`'s native `.yaml` format (and runs the `SV-R1..R10` linter):
```bash
strict-semantic import external_model.osi.yaml -o views/imported_model.yaml
```
* Runs `lint()` automatically and exits with status `1` if any `ERROR` findings occur (unless `--no-lint` is passed for inspection).

---

## 6. Acceptance Criteria & Test Plan (`tests/test_interchange_osi.py`)

Before closing `v0.2.0`, `pytest` must pass all existing 71 tests plus the following new test cases:

1. **`test_osi_roundtrip_ast_equality`:** Load `tests/fixtures/ecommerce.sql`, export to OSI YAML and OSI JSON, import both back into `SemanticView`, and assert `imported_view == original_view` (100% byte-identical AST).
2. **`test_osi_roundtrip_compiled_sql_parity`:** Compile a multi-metric query (including partition pruning and derived ratios) from the original view and from the round-tripped OSI view; assert identical compiled BigQuery SQL.
3. **`test_osi_import_enforces_linter`:** Import a vanilla OSI file that contains an ambiguous multi-path join (`SV-R3.5`) or a numeric dimension (`SV-R4.1`); assert `CompilerError` is raised with the exact rule error code.
4. **`test_osi_strip_extensions_warnings`:** Export a view with `Partition: order_date BY order_date (lookback 30d)` using `strip_extensions=True`; verify `custom_extensions` is absent from the output and a warning about stripped partition bounds is returned.
5. **`test_cli_export_and_import_end_to_end`:** Run `strict-semantic export` and `strict-semantic import` via CLI subprocess and verify exit codes and output files.

---

## 7. Kickoff Prompt for AI Coding Agent

Copy and paste the following prompt to your AI Coding Agent (Claude Code / Cursor):

> "Read `docs/osi-interoperability-blueprint.md` (or this specification) and implement `v0.2.0` bidirectional OSI (Open Semantic Interchange) support in `strict-semantic-mcp`:
> 1. Create `src/strict_semantic/interchange/osi.py` and `src/strict_semantic/interchange/__init__.py` implementing `export_osi` and `import_osi` per Section 3 & 4. Keep existing `.sql` and native `.yaml` parsers completely unchanged.
> 2. Update `src/strict_semantic/cli.py` to add `strict-semantic export` and `strict-semantic import` commands per Section 5.
> 3. Add `tests/test_interchange_osi.py` covering all 5 acceptance tests in Section 6.
> 4. Update `README.md` with brief CLI examples for `export` and `import`.
> 5. Run `pytest` and `ruff check .` and confirm all tests pass cleanly."
