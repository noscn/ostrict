"""Warehouse connectors and the config-file factory."""

from __future__ import annotations

from pathlib import Path

from .base import WarehouseConnector

__all__ = ["WarehouseConnector", "from_config"]


def from_config(cfg: dict, base_dir: str | Path = ".") -> WarehouseConnector:
    kind = (cfg or {}).get("type")
    if kind == "duckdb":
        from .duckdb import DuckDBConnector

        seed = cfg.get("seed")
        seed_sql = (Path(base_dir) / seed).read_text() if seed else None
        return DuckDBConnector(database=cfg.get("database", ":memory:"), seed_sql=seed_sql)
    if kind == "bigquery":
        from .bigquery import BigQueryConnector

        return BigQueryConnector(
            project=cfg["project"],
            credentials=cfg.get("credentials"),
            max_bytes=cfg.get("max_bytes"),
            location=cfg.get("location"),
        )
    raise ValueError(f"unknown connector type '{kind}' (duckdb | bigquery)")
