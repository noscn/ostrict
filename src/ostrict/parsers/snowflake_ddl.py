"""Parse Snowflake ``CREATE [OR REPLACE] SEMANTIC VIEW`` DDL into the canonical AST.

sqlglot (30.x) has no grammar for this statement, so we walk sqlglot's own token
stream — never regexes over SQL — splitting the five section lists on top-level
commas and handing every expression back to sqlglot. Character offsets on the
tokens let us slice expression text verbatim from the source.
"""

from __future__ import annotations

import sqlglot
from sqlglot.tokens import Token, TokenType

from ..models.ast import SemanticView
from ..models.errors import ERR_PARSE, CompilerError
from .common import build_view

SECTIONS = ("TABLES", "RELATIONSHIPS", "FACTS", "DIMENSIONS", "METRICS")
_TOP = ("COMMENT", "AI_SQL_GENERATION", *SECTIONS)


class _Cursor:
    def __init__(self, tokens: list[Token], sql: str, origin: str):
        self.toks, self.sql, self.origin, self.i = tokens, sql, origin, 0

    def done(self) -> bool:
        return self.i >= len(self.toks)

    def peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def word(self, k: int = 0) -> str:
        t = self.peek(k)
        return t.text.upper() if t else ""

    def take(self) -> Token:
        t = self.peek()
        if t is None:
            self.fail("unexpected end of statement")
        self.i += 1
        return t

    def expect(self, word: str) -> Token:
        if self.word() != word:
            got = self.peek().text if self.peek() else "end of statement"
            self.fail(f"expected {word}, got '{got}'")
        return self.take()

    def expect_type(self, tt: TokenType) -> Token:
        t = self.peek()
        if t is None or t.token_type != tt:
            self.fail(f"expected {tt.name}, got '{t.text if t else 'end of statement'}'")
        return self.take()

    def string(self) -> str:
        if self.word() == "=":
            self.take()
        return self.expect_type(TokenType.STRING).text

    def ident_list(self) -> list[str]:
        """( a, b, ... ) of plain identifiers or strings."""
        self.expect_type(TokenType.L_PAREN)
        out = []
        while self.word() != ")":
            t = self.take()
            if t.token_type == TokenType.COMMA:
                continue
            out.append(t.text)
        self.take()  # )
        return out

    def slice(self, start: Token, end: Token) -> str:
        return self.sql[start.start : end.end + 1]

    def until(self, stop_words: tuple[str, ...]) -> str:
        """Consume tokens until a depth-0 stop word (or the end); return the raw text."""
        depth = 0
        first = last = None
        while not self.done():
            t = self.peek()
            if depth == 0 and t.text.upper() in stop_words:
                break
            if t.token_type == TokenType.L_PAREN:
                depth += 1
            elif t.token_type == TokenType.R_PAREN:
                depth -= 1
            first = first or t
            last = t
            self.take()
        if first is None:
            self.fail(f"expected an expression before {'/'.join(stop_words)}")
        return self.slice(first, last)

    def fail(self, msg: str):
        t = self.peek()
        loc = f" (line {t.line})" if t else ""
        raise CompilerError(ERR_PARSE, f"{self.origin}: {msg}{loc}")


def _items(c: _Cursor) -> list[_Cursor]:
    """Split a parenthesized section into per-item cursors on top-level commas."""
    c.expect_type(TokenType.L_PAREN)
    out, buf, depth = [], [], 0
    while True:
        t = c.take()
        if t.token_type == TokenType.L_PAREN:
            depth += 1
        elif t.token_type == TokenType.R_PAREN:
            if depth == 0:
                break
            depth -= 1
        if depth == 0 and t.token_type == TokenType.COMMA:
            out.append(buf)
            buf = []
            continue
        buf.append(t)
    if buf:
        out.append(buf)
    return [_Cursor(item, c.sql, c.origin) for item in out if item]


def _table_item(c: _Cursor) -> tuple[str, dict]:
    name = c.take().text
    if c.word() == "AS":
        c.take()
    # sqlglot tokenizes "PRIMARY KEY" as one token; accept both spellings
    body: dict = {"source": c.until(("PRIMARY", "PRIMARY KEY", "COMMENT")).strip()}
    while not c.done():
        if c.word() in ("PRIMARY", "PRIMARY KEY"):
            if c.take().text.upper() == "PRIMARY":
                c.expect("KEY")
            body["primary_key"] = c.ident_list()
        elif c.word() == "COMMENT":
            c.take()
            body["comment"] = c.string()
        else:
            c.fail(f"unexpected '{c.peek().text}' in table definition")
    return name, body


def _relationship_item(c: _Cursor) -> dict:
    name = ""
    if c.word(1) == "AS":  # named form: rel_name AS child (...) REFERENCES parent (...)
        name = c.take().text
        c.take()
    child = c.take().text
    columns = c.ident_list()
    c.expect("REFERENCES")
    parent = c.take().text
    parent_columns = c.ident_list() if c.word() == "(" else None
    out = {"name": name or None, "child": child, "columns": columns, "parent": parent}
    if parent_columns:
        out["parent_columns"] = parent_columns
    return out


def _field_item(c: _Cursor, section: str) -> dict:
    table = None
    name = c.take().text
    if c.peek() and c.peek().token_type == TokenType.DOT:
        c.take()
        table, name = name, c.take().text
    c.expect("AS")
    item: dict = {"table": table, "name": name, "expr": c.until(("WITH", "NON", "COMMENT"))}
    while not c.done():
        if c.word() == "WITH":
            c.take()
            c.expect("SYNONYMS")
            item["synonyms"] = c.ident_list()
        elif c.word() == "NON":
            c.take()
            c.expect("ADDITIVE")
            c.expect("BY")
            cols = c.ident_list()
            if len(cols) != 1:
                c.fail("NON ADDITIVE BY takes exactly one column")
            item["non_additive_by"] = cols[0]
        elif c.word() == "COMMENT":
            c.take()
            item["comment"] = c.string()
        else:
            c.fail(f"unexpected '{c.peek().text}' in {section.lower()} definition")
    return item


def parse_ddl(sql: str, origin: str = "ddl") -> dict:
    """DDL text -> the raw dict shape shared with the YAML parser."""
    tokens = list(sqlglot.tokenize(sql, read="snowflake"))
    c = _Cursor(tokens, sql, origin)
    c.expect("CREATE")
    if c.word() == "OR":
        c.take()
        c.expect("REPLACE")
    # sqlglot may tokenize "SEMANTIC VIEW" as one token or two
    if c.word() == "SEMANTIC VIEW":
        c.take()
    else:
        c.expect("SEMANTIC")
        c.expect("VIEW")
    name = c.until(_TOP).strip()

    raw: dict = {"name": name, "dialect": "snowflake", "tables": {}, "relationships": []}
    raw.update({"facts": [], "dimensions": [], "metrics": []})
    while not c.done():
        word = c.word()
        if word == "TABLES":
            c.take()
            for item in _items(c):
                tname, body = _table_item(item)
                raw["tables"][tname] = body
        elif word == "RELATIONSHIPS":
            c.take()
            raw["relationships"] += [_relationship_item(i) for i in _items(c)]
        elif word in ("FACTS", "DIMENSIONS", "METRICS"):
            c.take()
            raw[word.lower()] += [_field_item(i, word) for i in _items(c)]
        elif word == "COMMENT":
            c.take()
            raw["comment"] = c.string()
        elif word == "AI_SQL_GENERATION":
            c.take()
            raw["ai_sql_generation"] = c.string()
        elif c.peek().token_type == TokenType.SEMICOLON:
            c.take()
        else:
            c.fail(f"unexpected '{c.peek().text}'")
    return raw


def parse_ddl_view(sql: str, origin: str = "ddl") -> SemanticView:
    return build_view(parse_ddl(sql, origin), origin)
