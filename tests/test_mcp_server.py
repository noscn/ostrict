"""Milestone 4: the MCP server answers all five tools over stdio (real subprocess)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).parent.parent
JAN = [
    {"field": "order_date", "op": ">=", "value": "2026-01-01"},
    {"field": "order_date", "op": "<", "value": "2026-02-01"},
]


def _payload(res):
    data = res.structured_content  # mcp 2.x: {"result": ...} for non-object returns
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    if data is not None:
        return data
    return json.loads(res.content[0].text)


async def _session_run():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ostrict.mcp_server.server"],
        cwd=str(ROOT),
        env={**os.environ, "OSTRICT_CONFIG": str(ROOT / "examples" / "config.duckdb.yml")},
    )
    out = {}
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)

            res = await s.call_tool("list_semantic_views", {})
            out["views"] = _payload(res)

            res = await s.call_tool("inspect_semantic_view", {"view": "ecommerce"})
            out["inspect"] = _payload(res)

            res = await s.call_tool(
                "get_dimension_values", {"view": "ecommerce", "dimension": "channel", "filters": JAN}
            )
            out["values"] = _payload(res)["values"]

            res = await s.call_tool(
                "compile_semantic_query", {"view": "ecommerce", "metrics": ["revenue"], "filters": JAN}
            )
            out["compile"] = _payload(res)

            res = await s.call_tool(
                "execute_semantic_query",
                {"view": "ecommerce", "metrics": ["revenue", "order_count"], "dimensions": ["region"],
                 "filters": JAN, "order_by": ["-revenue"]},
            )
            out["execute"] = _payload(res)

            res = await s.call_tool("execute_semantic_query", {"view": "ecommerce", "dimensions": ["region"]})
            out["refusal"] = (res.is_error, res.content[0].text)

            res = await s.call_tool("execute_semantic_query", {"view": "nope", "metrics": ["revenue"]})
            out["unknown_view"] = (res.is_error, res.content[0].text)
    return out


def test_mcp_server_stdio_end_to_end():
    out = asyncio.run(_session_run())
    assert out["tools"] == [
        "compile_semantic_query",
        "execute_semantic_query",
        "get_dimension_values",
        "inspect_semantic_view",
        "list_semantic_views",
    ]
    assert out["views"][0]["name"] == "ecommerce"
    assert {m["name"] for m in out["inspect"]["metrics"]} >= {"revenue", "aov", "stock_units"}
    assert out["inspect"]["required_filters"] == ["order_date", "snapshot_date"]
    assert out["values"] == ["store", "web"]
    assert out["compile"]["columns"] == ["revenue"] and "SELECT" in out["compile"]["sql"]
    rows = out["execute"]["rows"]
    assert rows[0] == {"region": "EU", "revenue": 70.0, "order_count": 2}
    err, text = out["refusal"]
    assert err and "ERR_METRIC_REQUIRED" in text and "get_dimension_values" in text
    err, text = out["unknown_view"]
    assert err and "ERR_UNKNOWN_VIEW" in text
