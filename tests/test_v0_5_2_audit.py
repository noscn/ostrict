"""v0.5.2: regression tests for the re-audit findings.

F9 PK heuristic on physical names after cross-schema renames, F10 bounded
identity-safe memo for un-weakrefable plugins, and the connector-level cap
when a plugin rewrites away the LIMIT.
"""

from conftest import JAN

from ostrict.compiler import (
    _MODELS_SEEN_BY_ID,
    MAX_UNWEAKABLE_PLUGIN_CACHE,
    compile_semantic_query,
    execute_semantic_query,
)
from ostrict.connectors.duckdb import DuckDBConnector
from ostrict.plugins.base import CompilerPlugin
from ostrict.scaffold.db_reader import read_schema

# ------------------------------------------------------------------------- F9


def test_renamed_cross_schema_tables_keep_confident_pk():
    con = DuckDBConnector()
    con.con.execute(
        "CREATE SCHEMA s1; CREATE SCHEMA s2;"
        "CREATE TABLE s1.orders(non_key_col VARCHAR, order_id INTEGER);"  # pk NOT first
        "CREATE TABLE s2.orders(non_key_col VARCHAR, order_id INTEGER);"
        "CREATE TABLE s1.order_items(item_id INTEGER, order_id INTEGER);"
    )
    drafts = {d.name: d for d in read_schema(con, ["s1.orders", "s2.orders", "s1.order_items"])}
    # the heuristic sees the PHYSICAL name 'orders', so 'order_id' is found confidently
    assert drafts["s1_orders"].primary_key == ["order_id"]
    assert drafts["s2_orders"].primary_key == ["order_id"]
    for name in ("s1_orders", "s2_orders"):
        assert not any("guessed primary key" in n for n in drafts[name].notes)


def test_ambiguous_parent_prefers_the_childs_schema():
    con = DuckDBConnector()
    con.con.execute(
        "CREATE SCHEMA s1; CREATE SCHEMA s2;"
        "CREATE TABLE s1.orders(non_key_col VARCHAR, order_id INTEGER);"
        "CREATE TABLE s2.orders(non_key_col VARCHAR, order_id INTEGER);"
        "CREATE TABLE s1.order_items(item_id INTEGER, order_id INTEGER);"
    )
    drafts = {d.name: d for d in read_schema(con, ["s1.orders", "s2.orders", "s1.order_items"])}
    rels = [(r.column, r.parent) for r in drafts["order_items"].relationships]
    assert rels == [("order_id", "s1_orders")]  # not dropped as ambiguous, not s2


def test_lone_cross_schema_parent_still_links():
    con = DuckDBConnector()
    con.con.execute(
        "CREATE SCHEMA s1; CREATE SCHEMA s2;"
        "CREATE TABLE s1.customers(customer_id INTEGER, region VARCHAR);"
        "CREATE TABLE s2.orders(order_id INTEGER, customer_id INTEGER);"
    )
    drafts = {d.name: d for d in read_schema(con, ["s1.customers", "s2.orders"])}
    rels = [(r.column, r.parent) for r in drafts["orders"].relationships]
    assert rels == [("customer_id", "customers")]  # a single match is never schema-gated


# ------------------------------------------------------------------------ F10


class _Slotted:  # duck-typed plugin without __dict__ or __weakref__
    __slots__ = ()

    def on_model_loaded(self, v):
        FIRED.append(id(self))

    def before_compile(self, v, request):
        return request

    def after_compile(self, v, request, compiled):
        return compiled

    def on_query_executed(self, v, request, compiled, result):
        return None


FIRED: list[int] = []


def test_unweakrefable_plugin_memo_is_bounded_and_identity_safe(view):
    FIRED.clear()
    rounds = MAX_UNWEAKABLE_PLUGIN_CACHE + 10
    for _ in range(rounds):  # instances die each round, so CPython reuses ids freely
        p = _Slotted()
        compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb", plugins=[p])
        del p
    # every fresh instance fired exactly once: a recycled id can never inherit an
    # old memo, because cached entries hold their instance strongly
    assert len(FIRED) == rounds
    assert len(_MODELS_SEEN_BY_ID) <= MAX_UNWEAKABLE_PLUGIN_CACHE


def test_unweakrefable_plugin_still_memoizes_while_alive(view):
    FIRED.clear()
    p = _Slotted()
    for _ in range(3):
        compile_semantic_query(view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb", plugins=[p])
    assert FIRED == [id(p)]  # once per (plugin, view), as for weakrefable plugins


# ------------------------------------------- probe fallback: LIMIT rewritten away


class _StripLimit(CompilerPlugin):
    def after_compile(self, v, request, compiled):
        sql = compiled.sql[: compiled.sql.rindex("LIMIT")].rstrip()
        return compiled.model_copy(update={"sql": sql})


def test_plugin_that_strips_limit_is_still_capped(view, connector):
    req = {"metrics": ["revenue"], "dimensions": ["region"], "filters": JAN,
           "order_by": ["-revenue"], "limit": 2}
    r = execute_semantic_query(view, req, connector, plugins=[_StripLimit()])
    assert len(r.rows) == 2 and r.truncated is True  # capped at the connector, flag exact

    r3 = execute_semantic_query(view, {**req, "limit": 3}, connector, plugins=[_StripLimit()])
    assert len(r3.rows) == 3 and r3.truncated is False  # exactly 3 region rows exist


def test_stripped_limit_sql_really_has_no_limit(view, connector):
    """Guard the fixture itself: the fallback path must be the one exercised."""
    compiled = compile_semantic_query(
        view, {"metrics": ["revenue"], "dimensions": ["region"], "filters": JAN, "limit": 2},
        dialect="duckdb", plugins=[_StripLimit()],
    )
    assert "LIMIT" not in compiled.sql
