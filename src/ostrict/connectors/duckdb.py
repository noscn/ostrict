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
        if max_rows is not None and max_rows < 0:
            raise ValueError(f"max_rows must be >= 0, got {max_rows}")
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall() if max_rows is None else cur.fetchmany(max_rows)
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def estimate_bytes(self, sql: str) -> int | None:
        return None
