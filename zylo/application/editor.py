"""Use cases for editors: symbols of a document and completion candidates.

These are independent of any editor protocol; `interface/lsp.py` turns them into Language Server Protocol messages.
"""

import re
from dataclasses import dataclass, field

from zylo.domain import ast
from zylo.domain.builtins import BUILTINS
from zylo.syntax.formatter import expr_text
from zylo.syntax.lexer import KEYWORDS
from zylo.syntax.parser import parse_collect


@dataclass
class Symbol:
    """A named thing defined in a document.

    Attributes:
        name: Its name.
        kind: "function", "struct", "enum", "method", "field", "variant" or "variable".
        line: 1-based line where it starts.
        col: 1-based column of its name.
        end_line: 1-based last line it occupies.
        detail: A short description, such as a function's signature.
        children: Nested symbols (a struct's fields and methods).
    """

    name: str
    kind: str
    line: int
    col: int
    end_line: int
    detail: str = ""
    children: list["Symbol"] = field(default_factory=list)


@dataclass
class Completion:
    """A completion candidate.

    Attributes:
        label: The text to insert.
        kind: "keyword", "function", "struct", "enum" or "variable".
        detail: A short description.
    """

    label: str
    kind: str
    detail: str = ""


def _type_text(t: ast.TypeRef) -> str:
    if t.name == "?":
        return _type_text(t.args[0]) + "?"
    return f"{t.name}[{', '.join(_type_text(a) for a in t.args)}]" if t.args else t.name


def _signature(f: ast.FnDef) -> str:
    params = (["self"] if f.has_self else []) + [
        f"{p.name}: {_type_text(p.type)}" + (f" = {expr_text(p.default)}" if p.default is not None else "")
        for p in f.params
    ]
    return f"fn {f.name}({', '.join(params)})" + (f" -> {_type_text(f.ret)}" if f.ret else "")


def _name_col(line_text: str, name: str, fallback: int) -> int:
    """1-based column of `name` as a whole word in a source line."""
    m = re.search(rf"\b{re.escape(name)}\b", line_text)
    return m.start() + 1 if m else fallback


def document_symbols(source: str) -> list[Symbol]:
    """List the functions, structs, enums and top-level variables of a document.

    The document may have syntax errors: whatever parsed is listed.

    Args:
        source: The document's text.

    Returns:
        The symbols in source order; a struct holds its fields and methods as children.
    """
    program, _ = parse_collect(source)
    lines = source.splitlines()
    total = max(len(lines), 1)
    items = [s for s in program.body if isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef)) or
             (isinstance(s, ast.Assign) and isinstance(s.target, ast.Name))]
    starts = [s.line for s in items]

    def end_of(i: int) -> int:
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else total
        while end > starts[i] and not lines[end - 1].strip():
            end -= 1
        return end

    def at(line: int) -> str:
        return lines[line - 1] if 0 < line <= len(lines) else ""

    symbols: list[Symbol] = []
    for i, s in enumerate(items):
        if isinstance(s, ast.StructDef):
            sym = Symbol(s.name, "struct", s.line, _name_col(at(s.line), s.name, s.col), end_of(i))
            sym.children += [Symbol(f.name, "field", f.line, _name_col(at(f.line), f.name, f.col), f.line,
                                    _type_text(f.type) + (f" = {expr_text(f.default)}" if f.default is not None else ""))
                             for f in s.fields]
            for j, m in enumerate(s.methods):
                nxt = s.methods[j + 1].line - 1 if j + 1 < len(s.methods) else sym.end_line
                sym.children.append(Symbol(m.name, "method", m.line, _name_col(at(m.line), m.name, m.col), max(nxt, m.line),
                                           _signature(m)))
        elif isinstance(s, ast.EnumDef):
            sym = Symbol(s.name, "enum", s.line, _name_col(at(s.line), s.name, s.col), end_of(i))
            sym.children += [Symbol(v.name, "variant", v.line, _name_col(at(v.line), v.name, v.col), v.line,
                                    f"({', '.join(_type_text(t) for t in v.fields)})" if v.fields else "")
                             for v in s.variants]
            for j, m in enumerate(s.methods):
                nxt = s.methods[j + 1].line - 1 if j + 1 < len(s.methods) else sym.end_line
                sym.children.append(Symbol(m.name, "method", m.line, _name_col(at(m.line), m.name, m.col), max(nxt, m.line),
                                           _signature(m)))
        elif isinstance(s, ast.FnDef):
            kind_line = at(s.line)
            sym = Symbol(s.name, "function", s.line, _name_col(kind_line, s.name, s.col), end_of(i), _signature(s))
        else:
            sym = Symbol(s.target.id, "variable", s.line, s.col, s.line)
        symbols.append(sym)
    return symbols


def completions(source: str) -> list[Completion]:
    """Suggest what can be typed: keywords, built-in functions, and the document's own functions and structs.

    Args:
        source: The document's text.

    Returns:
        The candidates, without duplicates.
    """
    items = [Completion(k, "keyword") for k in sorted(KEYWORDS)]
    items += [Completion(b, "function", "built-in") for b in sorted(BUILTINS)]
    seen = {c.label for c in items}
    for sym in document_symbols(source):
        if sym.name not in seen and sym.kind in ("function", "struct", "enum", "variable"):
            items.append(Completion(sym.name, sym.kind, sym.detail))
            seen.add(sym.name)
    return items
