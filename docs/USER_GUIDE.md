# User guide — asking questions without writing SQL

This guide is for the people who *use* the numbers: analysts, operators,
finance, product. You don't need to know SQL, and you won't see any unless you
ask for it.

## What you're talking to

Your data team has described the business in a **semantic view**: a catalog of

- **metrics** — the numbers that can be computed (`revenue`, `order_count`,
  `aov`, …), each with one agreed definition,
- **dimensions** — the things you can break those numbers down or filter by
  (`region`, `channel`, `order_date`, …).

When you ask a question in chat, the AI assistant picks metrics and dimensions
from that catalog and this tool turns them into one correct database query. The
assistant never writes SQL itself, so it cannot invent a definition, join
tables the wrong way, or double count. If a question can't be answered safely,
you get a refusal that says why and what to ask instead — that's a feature.

## Asking your first question

Just ask in plain language, naming a number, an optional breakdown, and a time
window:

> *"What was revenue by region in January?"*
> *"Order count per week for the last 8 weeks, web channel only."*
> *"Average order value by month this quarter — which region is highest?"*

Behind the scenes the assistant will list the available views, inspect the one
that fits, and run the query. You'll get the figures plus, on request, the
exact SQL that produced them (*"show me the SQL for that"*).

Three habits make every question work on the first try:

1. **Name a time window.** "In January", "last 4 weeks", "between Oct 1 and
   Oct 27". Large tables *require* a date bound — an open-ended question will be
   refused with a reminder, not run.
2. **One grain at a time.** Say "per day", "weekly", "by month" — date
   breakdowns come in fixed grains (day / week / month, sometimes hour, quarter,
   year).
3. **Use the catalog's words when you know them.** Synonyms are supported
   ("market" may mean "region"), and typos get a *did-you-mean* suggestion. To
   see what exists, ask: *"what metrics and dimensions can I ask about?"*

Not sure how a category is spelled in the data ("US" or "USA"?) — ask
*"what values does region have?"*. That uses a safe lookup, never a data dump.

## Reading a "no"

Every refusal names a reason and a fix. The common ones, in plain words:

| You'll see | What it means | What to do |
|---|---|---|
| *metric required / no raw rows* | Lists of individual records aren't served through this tool | Ask for a number (a count, a sum) instead of a list |
| *add a bounding filter on &lt;date&gt;* | The table is too big to scan without a time window | Add "in October", "since June 1", … |
| *not an ancestor / would duplicate rows* | That breakdown doesn't mathematically fit that metric (it would double count) | Ask that metric with a breakdown from its own area, or split into two questions |
| *must be pinned to one date* | A snapshot figure (like stock on hand) can't be summed across days | Ask "as of &lt;date&gt;" or "per day" |
| *unknown metric … did you mean* | A name isn't in the catalog | Use the suggestion, or ask what's available |
| *limit exceeds the cap* | The result would be too large | Narrow the window or the breakdown |

If a metric you need doesn't exist yet, that's not a dead end — see
"Requesting a new metric" below.

## Can I trust the numbers?

- **One definition.** "Revenue" means exactly what the catalog says it means,
  for everyone, in every chat. Definitions are code-reviewed files, not prompt
  text.
- **Live.** Queries run against the warehouse at the moment you ask, so very
  recent figures can be fresher than yesterday's reports. For day-level
  comparisons against a published report, ask for completed days (two or more
  days back).
- **Auditable.** Ask *"explain that query"* to see the SQL and the estimated
  scan before anything runs, or *"show the SQL"* afterward.
- **Deterministic.** The same question compiles to byte-identical SQL every
  time. No model mood swings in your KPIs.

## Requesting a new metric

Send your data team a note shaped like this — it maps 1:1 onto what they
maintain:

> **Name:** repeat_buyer_rate
> **In words:** of the customers who ordered this period, the share who had
> ordered before.
> **Built from:** orders (completed only).
> **Breakdowns I need:** region, month.
> **Example check:** for January it should be about 34% (from the old report).

A maintainer adds a few lines to a YAML file, the linter checks the definition
is safe, and it shows up in everyone's catalog — see
[CONTRIBUTING a metric](#contributing-a-metric-for-maintainers) below.

## Contributing a metric (for maintainers)

The semantic views are plain files (`.yml`, or `CREATE SEMANTIC VIEW` DDL
`.sql`) kept in a repository. The loop:

```bash
# 1. edit the view: add a dimension/metric with a comment, or a new table
$EDITOR views/sales.yml

# 2. lint — every ground rule (SV-R1..R10) runs here, same as CI
strict-semantic lint views --config strict_semantic.yml

# 3. see the SQL a request would produce (nothing executes)
strict-semantic compile views/sales.yml -m my_new_metric -d region \
    -f '{"field":"order_date","op":">=","value":"2026-01-01"}' --dialect bigquery

# 4. add a golden question with the expected answer, and run the eval
strict-semantic eval golden_questions.yaml
```

Rules of thumb the linter will hold you to: every table declares its key and
grain; numbers live in FACTS/METRICS, never DIMENSIONS; a metric is one
aggregation (ratios combine *named* metrics); names and synonyms are unique
view-wide; every field carries a real comment — that comment is exactly what
the AI reads to choose it, so write it for a colleague, not a compiler.

## Glossary

- **Metric** — a number computed over many rows (total, count, average, ratio).
- **Dimension** — a label you group or filter by (region, channel, a date).
- **Grain** — the size of a date bucket: day, week, month…
- **Filter** — a condition narrowing the rows ("web only", "October").
- **Semantic view** — the catalog of all of the above for one subject area; a
  blueprint the query is built from, never a stored copy of the data.
