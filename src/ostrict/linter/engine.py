"""Static Ground Rules Linter: orchestrates SV-R1 through SV-R10 over a view.

ERROR findings block compilation (``ensure_valid``); WARN/POLICY findings are
for CI mode (``ostrict lint``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..compiler.resolver import build_graph
from ..models.ast import SemanticView
from ..models.errors import CompilerError, LintFinding, Severity
from . import rules_governance, rules_logic, rules_structure

DEFAULT_SECURITY_DENY = (
    "*_email",
    "*_emails",
    "hrbps",
    "erbps",
    "has_rights_*",
    "manager_chain_*",
    "acl_*",
    "row_access_*",
)


@dataclass(frozen=True)
class LintConfig:
    security_deny: tuple[str, ...] = DEFAULT_SECURITY_DENY
    security_allowlist: tuple[str, ...] = ()
    no_pk_tables: tuple[str, ...] = ()  # SV-R2.1 event-only leaf exemption
    no_primary_date_tables: tuple[str, ...] = ()  # SV-R2.3 exemption: pure dimension tables
    max_fields_per_table: int = 50  # SV-R2.4
    max_tables: int = 10  # SV-R2.5
    max_case_conditions: int = 2  # SV-R5.1
    max_repeated_predicates: int = 5  # SV-R5.2
    max_comment_length: int = 400  # SV-R8.4


@dataclass
class LintReport:
    view: str
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity != Severity.ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def render(self) -> str:
        head = f"view '{self.view}': {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        return "\n".join([head, *(f.render() for f in self.findings)])


ALL_RULES = [*rules_structure.RULES, *rules_logic.RULES, *rules_governance.RULES]


def lint(view: SemanticView, config: LintConfig | None = None) -> LintReport:
    cfg = config or LintConfig()
    graph = build_graph(view)
    findings: list[LintFinding] = []
    for rule in ALL_RULES:
        findings += rule(view, graph, cfg)
    return LintReport(view=view.name, findings=list(dict.fromkeys(findings)))


def ensure_valid(view: SemanticView, config: LintConfig | None = None) -> LintReport:
    """Lint and raise (with the first error's code) if any ERROR finding exists."""
    report = lint(view, config)
    if not report.ok:
        first = report.errors[0]
        rest = f" (+{len(report.errors) - 1} more)" if len(report.errors) > 1 else ""
        raise CompilerError(first.code, f"view '{view.name}' failed lint: {first.message}{rest}")
    return report
