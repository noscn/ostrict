"""ostrict: strict semantic views, linted at load time and compiled
deterministically to SQL. The view is a blueprint only — never materialized."""

from .models.ast import SemanticView
from .models.errors import CompilerError, LintFinding, Severity
from .models.query import CompiledQuery, Filter, QueryResult, SemanticQueryRequest
from .parsers import load_view, load_views

__version__ = "0.5.6"
__all__ = [
    "CompiledQuery",
    "CompilerError",
    "Filter",
    "LintFinding",
    "QueryResult",
    "SemanticQueryRequest",
    "SemanticView",
    "Severity",
    "load_view",
    "load_views",
    "__version__",
]
