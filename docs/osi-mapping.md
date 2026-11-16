# OSI mapping

How `strict-semantic export` / `strict-semantic import` (and the
`strict_semantic.interchange` API) map OSI (Open Semantic Interchange)
documents onto the canonical `SemanticView` AST.

The public OSI initiative has not published a schema to verify against, so this
document is the contract this project emits and accepts. Standard OSI slots
map 1:1 onto the AST; guardrails that vanilla OSI has no slot for ride in a
namespaced `custom_extensions.strict_semantic` block at the document, dataset
and field level. An export → import round-trip rebuilds an equal view.

## Document shape

```yaml
semantic_model:
  name: ecommerce                     # required
  description: Retail e-commerce sales and inventory.
  ai_context:
    instructions: Route revenue and inventory questions here.
datasets:                             # required
- name: orders                        # required
  source: shop.orders                 # required; physical table or a SELECT
  primary_key: [order_id]
  description: 'One row per order. Primary date: order_date.'
  filter: "status = 'completed'"      # optional always-on row predicate
  fields:
  - name: order_date
    kind: dimension                   # fact | dimension (default: dimension)
    expression: order_date            # required
    description: 'Date the order was placed. Grains: day, week, month.'
    synonyms: [placed_on]
    time_grains: [day, week, month]   # non-empty marks a time dimension
    values: []                        # declared enumeration, for categoricals
    custom_extensions:
      strict_semantic:
        time_type: date               # date | timestamp (default: date)
  custom_extensions:
    strict_semantic:
      primary_date: order_date
      snapshot: false
      partition: {column: order_date, dimension: order_date, lookback_days: 0, lookahead_days: 0}
relationships:
- name: orders_customers
  from_dataset: orders                # required: the many (child) side
  from_columns: [customer_id]         # required
  to_dataset: customers               # required: the one (parent) side
  to_columns: [customer_id]           # default: the parent's primary key
metrics:
- name: revenue                       # required
  dataset: order_items                # absent -> a derived metric over other metrics
  expression: SUM(line_revenue)       # required
  description: 'Total revenue. Population: all order lines. Additivity: additive.'
  synonyms: []
  non_additive_by: null               # snapshot date column, for snapshot metrics
custom_extensions:
  strict_semantic:
    dialect: snowflake                # dialect the expressions are written in (default: snowflake)
    ai_sql_generation: Route revenue and inventory questions here.   # mirror of ai_context.instructions
```

JSON is accepted and emitted the same way (`--format json`); YAML is a JSON
superset, so one reader handles both. Empty values are omitted on export, and
key order is fixed, so exports are deterministic. Unknown OSI keys are ignored
on import.

## Field-by-field mapping

| Canonical AST | OSI | Notes |
| :--- | :--- | :--- |
| `view.name` | `semantic_model.name` | required |
| `view.comment` | `semantic_model.description` | |
| `view.ai_sql_generation` | `semantic_model.ai_context.instructions` | mirrored in `custom_extensions.strict_semantic.ai_sql_generation`; both present must agree |
| `view.dialect` | `custom_extensions.strict_semantic.dialect` | `snowflake` when absent |
| `table.name` / `.source` / `.primary_key` / `.comment` / `.filter` | `datasets[].name` / `.source` / `.primary_key` / `.description` / `.filter` | `name`, `source` required |
| `table.primary_date` / `.snapshot` / `.partition` | `datasets[].custom_extensions.strict_semantic.*` | partition: `{column, dimension, lookback_days, lookahead_days}` |
| `fact.*` | `datasets[].fields[]` with `kind: fact` | `name`, `expression` required |
| `dimension.*` | `datasets[].fields[]` with `kind: dimension` | plus `synonyms`, `time_grains` (= AST `grains`), `values` |
| `dimension.time_type` | `fields[].custom_extensions.strict_semantic.time_type` | `date` when absent |
| `relationship.child` / `.child_columns` | `relationships[].from_dataset` / `.from_columns` | required |
| `relationship.parent` / `.parent_columns` | `relationships[].to_dataset` / `.to_columns` | `to_columns` defaults to the parent's primary key; `name` defaults to `<child>_to_<parent>` |
| `metric.table` | `metrics[].dataset` | absent = derived metric |
| `metric.expr` / `.comment` / `.synonyms` / `.non_additive_by` | `metrics[].expression` / `.description` / `.synonyms` / `.non_additive_by` | `name`, `expression` required |

Every expression is re-canonicalized through sqlglot in the view's dialect on
import, exactly as the native parsers do.

## Import rules

Imports feed the same builder as the two native formats, so the semantics are
identical by construction:

- Malformed YAML/JSON, a missing required key, a duplicate dataset name, or an
  unknown field `kind` is refused with `ERR_PARSE`.
- Comment directives in descriptions (`Primary date: …`, `Partition: …`,
  `Grains: …`, `Type: …`, `Values: …`) are still honored. A value present both
  as a directive and in `custom_extensions` (or, for `values`, in the field)
  must agree, or the import is refused with `ERR_PARSE`. The snapshot flag is
  set by either source.
- Every import passes the full SV-R1..R10 gate (`ensure_valid`) by default —
  an external model cannot bypass the ground rules. `import_osi(...,
  validate=False)` / `strict-semantic import --no-lint` skip it for inspection;
  the CLI otherwise refuses with exit 1 and writes nothing on ERROR findings.

## Stripped exports

`export_osi(view, strip_extensions=True)` / `strict-semantic export
--strip-extensions` emits vanilla OSI: every `custom_extensions` block is
omitted, and one warning is returned (printed to stderr by the CLI) per
guardrail that drops out — a non-default dialect, primary dates, snapshot
flags, partition pruning bounds, and timestamp time types. Descriptions keep
their comment directives verbatim, so this project's importer still recovers
those guardrails from a stripped document; vanilla OSI consumers do not
interpret them.

## Round-trip guarantees and caveats

- Export → import rebuilds an equal `SemanticView` (asserted by the test
  suite for YAML and JSON, and for compiled-SQL parity).
- OSI nests facts/dimensions under datasets, so a view whose fields interleave
  across tables round-trips with them regrouped in table order; order within a
  table is preserved, and compilation — which resolves by name — is unaffected.
- `strict-semantic import` writes the native YAML contract. Explicit keys are
  emitted only where the description carries no equivalent directive; an
  enumeration that exists only as OSI `values` is appended to the comment as a
  `Values:` directive, the one slot the native format has for it.

## CLI

```bash
strict-semantic export views/sales.yml -o sales.osi.yaml
strict-semantic export views/sales.sql --format json --strip-extensions
strict-semantic import sales.osi.yaml -o views/imported.yml   # linted by default
```
