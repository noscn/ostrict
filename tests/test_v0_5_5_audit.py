"""v0.5.5: audit round 4 — the F11 fix proven end to end.

Round 4 re-raised F11 (a one-shot ``plugins`` generator) and the CI asks
against an older snapshot; every public entry point has materialized
``plugins`` since v0.5.3 (the compiler's three entry points are the only
consumers in the package), and the CI matrix + coverage landed separately.
What WAS missing is this regression: the v0.5.3 generator tests assert hook
ORDER only, never that a policy filter injected by ``before_compile``
actually constrains the executed rows — the exact scenario that made F11
dangerous for RBAC plugins.
"""

from conftest import JAN

from ostrict.compiler import execute_semantic_query
from ostrict.models.query import Filter
from ostrict.plugins.base import CompilerPlugin


class RecordingRbacPlugin(CompilerPlugin):
    """Records every lifecycle hook and injects an RBAC row filter."""

    def __init__(self):
        self.calls = []

    def on_model_loaded(self, v):
        self.calls.append("loaded")

    def before_compile(self, v, request):
        self.calls.append("before")
        return request.model_copy(
            update={"filters": [*request.filters, Filter(field="region", op="=", value="EU")]}
        )

    def after_compile(self, v, request, compiled):
        self.calls.append("after")
        return compiled

    def on_query_executed(self, v, request, compiled, result):
        self.calls.append("executed")


def test_generator_plugin_rbac_filter_constrains_executed_rows(view, connector):
    req = {"metrics": ["revenue"], "dimensions": ["region"], "filters": JAN}
    baseline = execute_semantic_query(view, req, connector)
    assert {r["region"] for r in baseline.rows} > {"EU"}  # several regions exist without the gate

    plugin = RecordingRbacPlugin()
    gated = execute_semantic_query(view, req, connector, plugins=(p for p in [plugin]))

    # all four lifecycle hooks fired, in order, despite the one-shot iterator
    assert plugin.calls == ["loaded", "before", "after", "executed"]
    # and the injected filter reached the warehouse: only the permitted region came back
    eu = next(r for r in baseline.rows if r["region"] == "EU")
    assert gated.rows == [eu]
