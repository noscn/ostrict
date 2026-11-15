"""Warehouse connector protocol — the only database-specific surface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WarehouseConnector(Protocol):
    dialect: str

    def execute(self, sql: str, max_rows: int | None = None) -> list[dict[str, Any]]: ...

    def estimate_bytes(self, sql: str) -> int | None:
        """Bytes the query would scan, or None when the engine cannot tell."""
        ...
