"""DuckDB connector: local zero-cost testing and the eval runner's fixture engine."""

from __future__ import annotations

from typing import Any


class DuckDBConnector:
    dialect = "duckdb"

    def __init__(self, database: str = ":memory:", seed_sql: str | None = None, connection=None):
        import duckdb

        self.con = connection or duckdb.connect(database)
        if seed_sql:
            self.con.execute(seed_sql)

    def execute(self, sql: str, max_rows: int | None = None) -> list[dict[str, Any]]:
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def estimate_bytes(self, sql: str) -> int | None:
        return None
