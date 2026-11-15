"""Directed child->parent join graph: reachability, unique paths, structural findings.

The graph rules are what make every compiled query safe:
  - edges only go child (many) -> parent (one)        (SV-R3.1)
  - at most one path between any two tables           (SV-R3.5)
  - queries only ever walk upward along these paths   (SV-R3.4, enforced in sql_builder)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.ast import Relationship, SemanticView
from ..models.errors import (
    ERR_AMBIGUOUS_MULTI_PATH,
    ERR_CYCLIC_RELATIONSHIP,
    LintFinding,
    Severity,
)

Path = tuple[Relationship, ...]


@dataclass
class Graph:
    # reach[t][u] = the unique child->parent path from t up to ancestor u
    reach: dict[str, dict[str, Path]]
    findings: list[LintFinding] = field(default_factory=list)

    def path_names(self, start: str, target: str) -> list[str]:
        return [r.name for r in self.reach[start].get(target, ())]


def build_graph(view: SemanticView) -> Graph:
    edges: dict[str, list[Relationship]] = {t: [] for t in view.tables}
    findings: list[LintFinding] = []

    for r in view.relationships:
        if r.child == r.parent:
            findings.append(
                LintFinding(
                    rule="SV-R3.1",
                    code=ERR_CYCLIC_RELATIONSHIP,
                    severity=Severity.ERROR,
                    message=f"relationship '{r.name}' references its own table '{r.child}'; "
                    "model a role-playing alias table instead",
                    where=f"relationship {r.name}",
                )
            )
            continue
        edges[r.child].append(r)

    def walk(start: str, paths: dict[str, Path], node: str, path: Path, seen: tuple[str, ...]) -> None:
        for r in edges[node]:
            if r.parent in seen:
                chain = " -> ".join((*seen, r.parent))
                findings.append(
                    LintFinding(
                        rule="SV-R3.1",
                        code=ERR_CYCLIC_RELATIONSHIP,
                        severity=Severity.ERROR,
                        message=f"relationships form a cycle: {chain}",
                        where=f"relationship {r.name}",
                    )
                )
                continue
            if r.parent in paths:
                a = " -> ".join((start, *(x.parent for x in paths[r.parent])))
                b = " -> ".join((start, *(x.parent for x in (*path, r))))
                findings.append(
                    LintFinding(
                        rule="SV-R3.5",
                        code=ERR_AMBIGUOUS_MULTI_PATH,
                        severity=Severity.ERROR,
                        message=f"two join paths from '{start}' to '{r.parent}' ({a} | {b}); "
                        "keep one relationship or introduce a role-playing alias table",
                        where=f"table {start}",
                    )
                )
                continue
            paths[r.parent] = (*path, r)
            walk(start, paths, r.parent, (*path, r), (*seen, r.parent))

    reach: dict[str, dict[str, Path]] = {}
    for start in view.tables:
        paths: dict[str, Path] = {}
        walk(start, paths, start, (), (start,))
        reach[start] = paths

    return Graph(reach=reach, findings=list(dict.fromkeys(findings)))
