"""BigQuery connector: dry-run byte estimation and a hard maximum_bytes_billed cap.

Lesson baked in from production use: BigQuery only honors required partition
filters on *joined* tables when they sit inside the table's own subquery — the
compiler does that; this connector adds the cost ceiling on top.
"""

from __future__ import annotations

from typing import Any

from ..models.errors import ERR_COST_LIMIT, CompilerError


class BigQueryConnector:
    dialect = "bigquery"

    def __init__(
        self,
        project: str,
        credentials: str | None = None,
        max_bytes: int | None = None,
        location: str | None = None,
        labels: dict[str, str] | None = None,
    ):
        from google.cloud import bigquery

        self._bq = bigquery
        self.client = (
            bigquery.Client.from_service_account_json(credentials, project=project)
            if credentials
            else bigquery.Client(project=project)
        )
        self.max_bytes = max_bytes
        self.location = location
        self.labels = labels or {"tool": "ostrict"}

    def estimate_bytes(self, sql: str) -> int | None:
        cfg = self._bq.QueryJobConfig(dry_run=True, use_query_cache=False)
        return self.client.query(sql, job_config=cfg, location=self.location).total_bytes_processed

    def execute(self, sql: str, max_rows: int | None = None) -> list[dict[str, Any]]:
        if self.max_bytes is not None:
            est = self.estimate_bytes(sql)
            if est is not None and est > self.max_bytes:
                raise CompilerError(
                    ERR_COST_LIMIT,
                    f"query would scan {est / 1e9:,.1f} GB, over the {self.max_bytes / 1e9:,.1f} GB limit",
                    hint="narrow the date range or drop dimensions",
                )
        cfg = self._bq.QueryJobConfig(maximum_bytes_billed=self.max_bytes, labels=self.labels)
        rows = self.client.query(sql, job_config=cfg, location=self.location).result(max_results=max_rows)
        return [dict(r.items()) for r in rows]
