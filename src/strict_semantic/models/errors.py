"""Error codes shared by the linter (findings) and the compiler (exceptions).

Every blueprint rule maps to exactly one code here, so tests, CI logs and MCP
error payloads all speak the same language.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    POLICY = "policy"


# ---- load / lint time -------------------------------------------------------
ERR_PARSE = "ERR_PARSE"
ERR_UNKNOWN_TABLE = "ERR_UNKNOWN_TABLE"
ERR_DISCONNECTED_TABLES = "ERR_DISCONNECTED_TABLES"  # SV-R1.1
ERR_MISSING_GRAIN_OR_PK = "ERR_MISSING_GRAIN_OR_PK"  # SV-R2.1
ERR_MISSING_PRIMARY_DATE = "ERR_MISSING_PRIMARY_DATE"  # SV-R2.3
ERR_ORPHAN_TABLE = "ERR_ORPHAN_TABLE"  # SV-R2.10
ERR_CYCLIC_RELATIONSHIP = "ERR_CYCLIC_RELATIONSHIP"  # SV-R3.1
ERR_PARTIAL_PK_JOIN = "ERR_PARTIAL_PK_JOIN"  # SV-R3.2
ERR_AMBIGUOUS_MULTI_PATH = "ERR_AMBIGUOUS_MULTI_PATH"  # SV-R3.5
ERR_NUMERIC_IN_DIMENSIONS = "ERR_NUMERIC_IN_DIMENSIONS"  # SV-R4.1
ERR_INVALID_METRIC = "ERR_INVALID_METRIC"  # SV-R5 shape
ERR_NON_DETERMINISTIC_TIME = "ERR_NON_DETERMINISTIC_TIME"  # SV-R5.3
ERR_MISSING_NON_ADDITIVE_BY = "ERR_MISSING_NON_ADDITIVE_BY"  # SV-R5.7
ERR_INVALID_RATIO_METRIC = "ERR_INVALID_RATIO_METRIC"  # SV-R5.8
ERR_SECURITY_PLUMBING_EXPOSED = "ERR_SECURITY_PLUMBING_EXPOSED"  # SV-R6.2
ERR_DUPLICATE_FIELD_NAME = "ERR_DUPLICATE_FIELD_NAME"  # SV-R6.4
ERR_DUPLICATE_SYNONYM = "ERR_DUPLICATE_SYNONYM"  # SV-R8.5

# ---- compile time -----------------------------------------------------------
ERR_FANOUT_JOIN_FORBIDDEN = "ERR_FANOUT_JOIN_FORBIDDEN"  # SV-R3.4
ERR_SNAPSHOT_AGGREGATION_VIOLATION = "ERR_SNAPSHOT_AGGREGATION_VIOLATION"  # SV-R7
ERR_METRIC_REQUIRED = "ERR_METRIC_REQUIRED"  # SV-R10.7
ERR_MISSING_PARTITION_FILTER = "ERR_MISSING_PARTITION_FILTER"
ERR_EXCESSIVE_LIMIT = "ERR_EXCESSIVE_LIMIT"
ERR_UNKNOWN_FIELD = "ERR_UNKNOWN_FIELD"
ERR_UNKNOWN_VIEW = "ERR_UNKNOWN_VIEW"
ERR_INVALID_FILTER = "ERR_INVALID_FILTER"
ERR_COST_LIMIT = "ERR_COST_LIMIT"


@dataclass(frozen=True)
class LintFinding:
    rule: str  # e.g. "SV-R3.5"
    code: str  # e.g. ERR_AMBIGUOUS_MULTI_PATH or a WARN_* code
    severity: Severity
    message: str
    where: str = ""  # e.g. "table orders", "metric revenue"

    def render(self) -> str:
        loc = f" [{self.where}]" if self.where else ""
        return f"{self.severity.value.upper():6} {self.rule:<9} {self.code}{loc}: {self.message}"


class CompilerError(Exception):
    """A refused model or request, with a stable machine-readable code."""

    def __init__(self, code: str, message: str, hint: str = ""):
        self.code = code
        self.hint = hint
        super().__init__(message)

    def payload(self) -> dict:
        out = {"code": self.code, "message": str(self)}
        if self.hint:
            out["hint"] = self.hint
        return out
