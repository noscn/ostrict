"""v0.5.3: regression tests for re-audit finding F11.

``plugins`` may arrive as a one-shot iterator (a generator). The compile
functions iterate the plugins several times — notify, before_compile,
after_compile — so an unmaterialized iterator would fire ``on_model_loaded``
and then silently skip every later hook. All three public entry points must
materialize it once up front.
"""

from conftest import JAN

from ostrict.compiler import compile_dimension_values, compile_semantic_query, execute_semantic_query
from ostrict.plugins.base import CompilerPlugin


class Recorder(CompilerPlugin):
    def __init__(self):
        self.calls = []

    def on_model_loaded(self, v):
        self.calls.append("loaded")

    def before_compile(self, v, request):
        self.calls.append("before")
        return request

    def after_compile(self, v, request, compiled):
        self.calls.append("after")
        return compiled

    def on_query_executed(self, v, request, compiled, result):
        self.calls.append("executed")


def test_compile_accepts_a_generator_of_plugins(view):
    rec = Recorder()
    compile_semantic_query(
        view, {"metrics": ["revenue"], "filters": JAN}, dialect="duckdb", plugins=(p for p in [rec])
    )
    assert rec.calls == ["loaded", "before", "after"]  # no hook silently skipped


def test_dimension_values_accepts_a_generator_of_plugins(view):
    rec = Recorder()
    compile_dimension_values(view, "channel", filters=JAN, dialect="duckdb", plugins=(p for p in [rec]))
    assert rec.calls == ["loaded", "before"]  # value lookups have no after_compile hook


def test_execute_accepts_a_generator_of_plugins(view, connector):
    rec = Recorder()
    result = execute_semantic_query(
        view, {"metrics": ["revenue"], "filters": JAN}, connector, plugins=(p for p in [rec])
    )
    assert rec.calls == ["loaded", "before", "after", "executed"]
    assert result.rows
