"""Structured request / response contracts (Pydantic v2) for the compiler and MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

FILTER_OPS = (
    "=",
    "!=",
    "<>",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "not in",
    "between",
    "like",
    "not like",
    "is null",
    "is not null",
)


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: str = "="
    value: Any = None


class SemanticQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: str = ""
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)  # output name; "-name" = descending
    limit: int | None = None


class CompiledQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str
    dialect: str
    columns: list[str]
    tables: list[str]  # logical tables touched
    join_paths: dict[str, list[str]]  # base table -> join sequence
    estimated_bytes: int | None = None


class QueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    rows: list[dict]
    truncated: bool = False
    sql: str = ""
