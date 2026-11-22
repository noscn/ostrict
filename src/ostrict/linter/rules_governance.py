"""SV-R8 (comments, synonyms, instructions) and SV-R9 (security posture)."""

from __future__ import annotations

import re
from collections import Counter

from ..models.errors import ERR_DUPLICATE_SYNONYM, LintFinding, Severity

E, W, P = Severity.ERROR, Severity.WARN, Severity.POLICY
_SECURITY_PROSE = ("only show", "restrict", "permission", " role", "tenant", "mask", "hide ")


def _f(rule, code, severity, message, where=""):
    return LintFinding(rule=rule, code=code, severity=severity, message=message, where=where)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def r8_templates(view, graph, cfg):
    """SV-R8.1: structured comment templates (table: Source; metric: Population/Additivity)."""
    out = []
    for t in view.tables.values():
        if "Source:" not in t.comment:
            out.append(_f("SV-R8.1", "WARN_COMMENT_TEMPLATE", W, "add 'Source: <src>.' to the comment", f"table {t.name}"))
    for m in view.metrics:
        missing = [k for k in (("Population:",) if not m.is_derived else ()) + ("Additivity:",) if k not in m.comment]
        if missing:
            out.append(
                _f("SV-R8.1", "WARN_COMMENT_TEMPLATE", W,
                   f"add {' and '.join(missing)} to the comment", f"metric {m.name}")
            )
    return out


def r8_non_tautological(view, graph, cfg):
    """SV-R8.2: comments must say more than the field name does."""
    out = []
    for kind, items in (("dimension", view.dimensions), ("metric", view.metrics), ("fact", view.facts)):
        for f in items:
            where = f"{kind} {f.name}"
            body = f.comment.split(".")[0] if f.comment else ""
            if len(f.comment.strip()) < 25:
                out.append(_f("SV-R8.2", "WARN_COMMENT_TOO_SHORT", W, "comments under 25 characters say nothing", where))
            elif _normalize(body) == _normalize(f.name):
                out.append(_f("SV-R8.2", "WARN_TAUTOLOGICAL_COMMENT", W, "the comment merely repeats the name", where))
    return out


def r8_prose_cap(view, graph, cfg):
    """SV-R8.4: very long metric comments hide rules that belong in the structure."""
    return [
        _f("SV-R8.4", "WARN_COMMENT_TOO_LONG", W,
           f"{len(m.comment)} chars (cap {cfg.max_comment_length}); enforce the rule structurally instead",
           f"metric {m.name}")
        for m in view.metrics
        if len(m.comment) > cfg.max_comment_length
    ]


def r8_unique_synonyms(view, graph, cfg):
    """SV-R8.5: synonyms are globally unique and never collide with field names."""
    out = []
    fields = [f.name for f in (*view.dimensions, *view.facts, *view.metrics)]
    taken = Counter(fields)
    syns = Counter()
    for f in (*view.dimensions, *view.metrics):
        syns.update(f.synonyms)
    for s, n in sorted(syns.items()):
        if n > 1:
            out.append(_f("SV-R8.5", ERR_DUPLICATE_SYNONYM, E, f"synonym '{s}' is declared {n} times"))
        elif taken[s]:
            out.append(_f("SV-R8.5", ERR_DUPLICATE_SYNONYM, E, f"synonym '{s}' collides with a field name"))
    return out


def r9_no_security_in_prose(view, graph, cfg):
    """SV-R9.1: access control never rides in AI_SQL_GENERATION prose."""
    text = view.ai_sql_generation.lower()
    hits = sorted({w.strip() for w in _SECURITY_PROSE if w in text})
    if hits:
        return [
            _f("SV-R9.1", "POLICY_SECURITY_IN_PROSE", P,
               f"AI_SQL_GENERATION mentions {hits}; enforce security deterministically via the "
               "before_compile() plugin hook or warehouse row/column policies, never prose")
        ]
    return []


RULES = [r8_templates, r8_non_tautological, r8_prose_cap, r8_unique_synonyms, r9_no_security_in_prose]
