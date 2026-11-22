"""Open-core plugin lifecycle (SV-R9.1 and the blueprint's enterprise hooks).

Subclass and override what you need; every hook is a no-op by default and runs
in registration order. `before_compile` is where warehouse-enforced security
belongs (e.g. injecting a tenant filter) — never prose in AI_SQL_GENERATION.
"""

from __future__ import annotations

from ..models.ast import SemanticView
from ..models.query import CompiledQuery, QueryResult, SemanticQueryRequest


class CompilerPlugin:
    def on_model_loaded(self, view: SemanticView) -> None:  # audit / registry
        # Fires once per model object, before its first compile on this plugin;
        # a reloaded spec is a new object and fires again.
        return None

    def before_compile(self, view: SemanticView, request: SemanticQueryRequest) -> SemanticQueryRequest:
        return request  # e.g. RBAC: request.filters.append(Filter(field=..., op="=", value=tenant))

    def after_compile(self, view: SemanticView, request: SemanticQueryRequest, compiled: CompiledQuery) -> CompiledQuery:
        return compiled  # e.g. semantic cache lookup / lineage logging

    def on_query_executed(
        self, view: SemanticView, request: SemanticQueryRequest, compiled: CompiledQuery, result: QueryResult
    ) -> None:
        return None  # e.g. audit log
