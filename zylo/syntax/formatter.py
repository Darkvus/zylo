"""Formatter: prints a Zylo program in the canonical style.

The formatter parses the source and prints the syntax tree again, so formatting can never change what a program
means; the tests check that the tree is identical before and after and that formatting twice changes nothing.
Comments are not part of the tree: they are collected from the source and put back next to the statements they
were written beside.

Style: four-space indentation, one space around binary operators and after commas, double-quoted strings, one blank
line around top-level definitions and between methods, and at most one consecutive blank line.
"""

from decimal import Decimal

from zylo.domain import ast
from zylo.syntax.parser import parse

INDENT = "    "

# Precedence of the binary operators; the same table the parser uses.
PREC = {"or": 1, "and": 2, "==": 3, "!=": 3, "<": 4, ">": 4, "<=": 4, ">=": 4, "in": 4, "not in": 4,
        "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}
IS_PREC = 3
NOT_PREC = 3
ATOM = 100  # postfix expressions and literals never need parentheses
AUGMENTED = {"+", "-", "*", "/", "%"}
ESCAPES = {"\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0"}


def format_source(src: str) -> str:
    """Format a Zylo source text.

    Args:
        src: The program text.

    Returns:
        The formatted text, ending with a single newline (empty for an empty program).

    Raises:
        ZyloError: If the program has a syntax error.
    """
    return _Formatter(src).run()


# ---- expressions ---------------------------------------------------------------------------------

def quote(text: str, q: str = '"') -> str:
    """Return a string literal for `text`, escaped for the quote character `q`."""
    body = "".join(ESCAPES.get(c, c) for c in text).replace(q, "\\" + q)
    return f"{q}{body}{q}"


def float_text(v: float) -> str:
    """Return a float literal the lexer can read back (it has no exponent form)."""
    text = repr(v)
    if "e" in text or "E" in text:
        text = format(Decimal(text), "f")
    return text if "." in text else text + ".0"


def type_text(t: ast.TypeRef) -> str:
    """Return a written type."""
    if t.name == "?":
        inner = type_text(t.args[0])
        return f"({inner})?" if t.args[0].name == "fn" else inner + "?"  # `fn(int) -> int?` returns an Option
    if t.name == "fn":
        ret = f" -> {type_text(t.ret)}" if t.ret else ""
        return f"fn({', '.join(type_text(a) for a in t.args)}){ret}"
    if t.args:
        return f"{t.name}[{', '.join(type_text(a) for a in t.args)}]"
    return t.name


def expr_text(e: ast.Expr, min_prec: int = 0, q: str = '"') -> str:
    """Print an expression, adding parentheses only where the grammar needs them.

    Args:
        e: The expression.
        min_prec: The binding strength the context requires; a looser operation is wrapped in parentheses.
        q: The quote character for strings (an f-string's fields must use the other one).

    Returns:
        The expression's source text.
    """
    text, prec = _expr(e, q)
    return f"({text})" if prec < min_prec else text


def _args(items: list[ast.Expr], q: str, named: list[ast.NamedArg] | None = None) -> str:
    parts = [expr_text(a, 0, q) for a in items]
    parts += [f"{n.name}={expr_text(n.value, 0, q)}" for n in named or []]
    return ", ".join(parts)


def _expr(e: ast.Expr, q: str) -> tuple[str, int]:
    """Return an expression's text and its own precedence."""
    match e:
        case ast.Int(v):
            return str(v), ATOM
        case ast.Float(v):
            return float_text(v), ATOM
        case ast.Str(v):
            return quote(v, q), ATOM
        case ast.Bool(v):
            return ("true" if v else "false"), ATOM
        case ast.NoneLit():
            return "none", ATOM
        case ast.Name(i):
            return i, ATOM
        case ast.TypeRef():  # the type argument of `from_json(list[int], j)`
            return type_text(e), ATOM
        case ast.BinOp(op, left, right):
            p = PREC[op]
            if op in ("and", "or") and isinstance(right, ast.UnaryOp) and right.op == "not":
                right_text = _expr(right, q)[0]  # `a and not b` needs no parentheses
            else:
                right_text = expr_text(right, p + 1, q)
            return f"{expr_text(left, p, q)} {op} {right_text}", p
        case ast.UnaryOp("not", x):
            return f"not {expr_text(x, NOT_PREC, q)}", NOT_PREC - 1
        case ast.UnaryOp(op, x):
            return f"{op}{expr_text(x, ATOM, q)}", ATOM
        case ast.IsCheck(v, kind):
            return f"{expr_text(v, IS_PREC, q)} is {kind}", IS_PREC
        case ast.Try(v):
            return f"{expr_text(v, ATOM, q)}?", ATOM
        case ast.Call(func, args, named=named):
            return f"{func}({_args(args, q, named)})", ATOM
        case ast.CallValue(callee, args):
            return f"{expr_text(callee, ATOM, q)}({_args(args, q)})", ATOM
        case ast.Lambda():
            params = ", ".join(p.name + (f": {type_text(p.type)}" if p.type else "") for p in e.params)
            ret = f" -> {type_text(e.ret)}" if e.ret else ""
            return f"fn({params}){ret} => {expr_text(e.expr, 0, q)}", 0
        case ast.MethodCall(v, name, args, named=named):
            return f"{expr_text(v, ATOM, q)}.{name}({_args(args, q, named)})", ATOM
        case ast.Attr(v, name):
            return f"{expr_text(v, ATOM, q)}.{name}", ATOM
        case ast.Index(v, i):
            return f"{expr_text(v, ATOM, q)}[{expr_text(i, 0, q)}]", ATOM
        case ast.Slice(v, lo, hi):
            lo_t = expr_text(lo, 0, q) if lo is not None else ""
            hi_t = expr_text(hi, 0, q) if hi is not None else ""
            return f"{expr_text(v, ATOM, q)}[{lo_t}:{hi_t}]", ATOM
        case ast.ListLit(items):
            return f"[{_args(items, q)}]", ATOM
        case ast.TupleLit(items):
            return f"({_args(items, q)})", ATOM
        case ast.SetLit(items):
            return "{" + _args(items, q) + "}", ATOM
        case ast.DictLit(keys, values):
            pairs = ", ".join(f"{expr_text(k, 0, q)}: {expr_text(v, 0, q)}" for k, v in zip(keys, values))
            return "{" + pairs + "}", ATOM
        case ast.FString(parts):
            return _fstring(parts), ATOM
    raise ValueError(f"cannot format {type(e).__name__}")


def _fstring(parts: list) -> str:
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(quote(p)[1:-1].replace("{", "{{").replace("}", "}}"))
        else:
            spec = f":{p.spec}" if p.spec else ""
            out.append("{" + expr_text(p.value, 0, "'") + spec + "}")
    return 'f"' + "".join(out) + '"'


def _bare_tuple(e: ast.Expr) -> str:
    """A tuple on the right of `=` or after `return` is written without parentheses."""
    if isinstance(e, ast.TupleLit):
        return ", ".join(expr_text(x) for x in e.items)
    return expr_text(e)


# ---- statements, with their comments ---------------------------------------------------------------

def _scan_comments(src: str) -> list[tuple[int, str, bool]]:
    """Find the comments of a source text.

    Returns:
        `(line, text, trailing)` in line order, where `trailing` says the comment follows code on its line.
    """
    found = []
    for n, line in enumerate(src.splitlines(), start=1):
        quote_char, i = None, 0
        while i < len(line):
            c = line[i]
            if quote_char:
                if c == "\\":
                    i += 1
                elif c == quote_char:
                    quote_char = None
            elif c in "\"'":
                quote_char = c
            elif c == "#":
                found.append((n, line[i:].rstrip(), bool(line[:i].strip())))
                break
            i += 1
    return found


class _Formatter:
    """Prints a program line by line, interleaving the comments of the original."""

    def __init__(self, src: str):
        self.prog = parse(src)
        self.comments = _scan_comments(src)
        self.ci = 0
        lines = src.splitlines()
        # a line "had a blank line before it" when the nearest non-blank line above is not directly above
        self.blank_before = {n: n > 1 and not lines[n - 2].strip() for n in range(1, len(lines) + 1)}
        self.out: list[str] = []
        self.wrote_in_block = False  # whether a comment was already written at the start of the current block

    def run(self) -> str:
        """Format the whole program."""
        self.block(self.prog.body, 0, top=True)
        for _line, text, _ in self.comments[self.ci:]:  # comments after the last statement
            self.add(text)
        while self.out and not self.out[-1]:
            self.out.pop()
        return "\n".join(self.out) + "\n" if self.out else ""

    # -- output helpers --

    def add(self, text: str) -> None:
        """Append a line, never allowing two blank lines in a row or a blank line first."""
        if not text and (not self.out or not self.out[-1]):
            return
        self.out.append(text)

    def blank(self) -> None:
        """Ask for a blank line (ignored at the start of the output and after another blank line)."""
        self.add("")

    def flush_comments(self, before: int, indent: int, first_in_block: bool) -> None:
        """Write the comments that sit above source line `before`, at this indentation."""
        while self.ci < len(self.comments) and self.comments[self.ci][0] < before:
            line, text, _ = self.comments[self.ci]
            self.ci += 1
            if self.blank_before.get(line) and not (first_in_block and not self.wrote_in_block):
                self.blank()
            self.wrote_in_block = True
            self.add(INDENT * indent + text)

    def line(self, node_line: int, indent: int, text: str, first_in_block: bool = False) -> None:
        """Write one statement line, with the comments above it and the one beside it."""
        self.wrote_in_block = False
        self.flush_comments(node_line, indent, first_in_block)
        if self.blank_before.get(node_line) and not (first_in_block and not self.wrote_in_block):
            self.blank()
        trailing = ""
        if self.ci < len(self.comments) and self.comments[self.ci][0] == node_line and self.comments[self.ci][2]:
            trailing = "  " + self.comments[self.ci][1]
            self.ci += 1
        self.add(INDENT * indent + text + trailing)

    # -- statements --

    def block(self, body: list[ast.Stmt], indent: int, top: bool = False) -> None:
        """Write a list of statements."""
        previous_def = False
        for n, s in enumerate(body):
            is_def = isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef))
            if top and n and (is_def or previous_def):
                self.blank()
            self.stmt(s, indent, first_in_block=n == 0 and not top)
            previous_def = is_def

    def header(self, s: ast.Stmt, indent: int, text: str, first_in_block: bool) -> None:
        """Write a compound statement's header line."""
        self.line(s.line, indent, text + ":", first_in_block)

    def stmt(self, s: ast.Stmt, indent: int, first_in_block: bool = False) -> None:
        """Write one statement and its nested blocks."""
        match s:
            case ast.ExprStmt(e):
                self.line(s.line, indent, expr_text(e), first_in_block)
            case ast.Assign(value=ast.Lambda(name=str()) as lam):  # a local `fn name(...)`
                params = ", ".join(f"{p.name}: {type_text(p.type)}" for p in lam.params)
                ret = f" -> {type_text(lam.ret)}" if lam.ret else ""
                self.header(s, indent, f"fn {lam.name}({params}){ret}", first_in_block)
                self.block(lam.body, indent + 1)
            case ast.Assign():
                self.line(s.line, indent, self.assign_text(s), first_in_block)
            case ast.Unpack(targets, value):
                self.line(s.line, indent, f"{', '.join(t.id for t in targets)} = {_bare_tuple(value)}", first_in_block)
            case ast.If():
                self.if_stmt(s, indent, first_in_block, "if")
            case ast.While(cond, body):
                self.header(s, indent, f"while {expr_text(cond)}", first_in_block)
                self.block(body, indent + 1)
            case ast.For():
                names = ", ".join(s.targets) if s.targets else s.var
                self.header(s, indent, f"for {names} in {expr_text(s.iter)}", first_in_block)
                self.block(s.body, indent + 1)
            case ast.Return(value):
                self.line(s.line, indent, "return" if value is None else f"return {_bare_tuple(value)}", first_in_block)
            case ast.Break():
                self.line(s.line, indent, "break", first_in_block)
            case ast.Continue():
                self.line(s.line, indent, "continue", first_in_block)
            case ast.ExternFn():
                self.line(s.line, indent, f"extern {self.signature(s)} = {quote(s.template)}", first_in_block)
            case ast.FnDef():
                self.header(s, indent, self.signature(s), first_in_block)
                self.block(s.body, indent + 1)
            case ast.StructDef():
                self.struct(s, indent, first_in_block)
            case ast.EnumDef():
                self.header(s, indent, f"enum {s.name}", first_in_block)
                if s.doc is not None:
                    self.line(s.doc_line, indent + 1, quote(s.doc), first_in_block=True)
                for n, v in enumerate(s.variants):
                    shown = f"({', '.join(type_text(t) for t in v.fields)})" if v.fields else ""
                    self.line(v.line, indent + 1, v.name + shown, first_in_block=n == 0 and s.doc is None)
                    if v.doc is not None:
                        self.line(v.doc_line, indent + 1, quote(v.doc))
                for m in s.methods:
                    self.blank()
                    self.stmt(m, indent + 1, first_in_block=True)
            case ast.Match():
                self.header(s, indent, f"match {expr_text(s.value)}", first_in_block)
                for n, arm in enumerate(s.arms):
                    head = self.pattern_text(arm)
                    self.header(arm, indent + 1, head, first_in_block=n == 0)
                    self.block(arm.body, indent + 2)
            case ast.Import():
                self.line(s.line, indent, f"import {s.module}" + (f" as {s.alias}" if s.alias else ""), first_in_block)
            case ast.FromImport():
                names = ", ".join(n + (f" as {a}" if a else "") for n, a in s.names)
                self.line(s.line, indent, f"{'pub ' if s.pub else ''}from {s.module} import {names}", first_in_block)
            case _:
                raise ValueError(f"cannot format {type(s).__name__}")

    @staticmethod
    def pattern_text(arm: ast.MatchArm) -> str:
        """Return the pattern of a `match` arm as written."""
        binds = f"({', '.join(arm.binds)})" if arm.binds else ""
        if arm.literal is not None:
            return expr_text(arm.literal)
        if arm.type is not None:
            return f"{type_text(arm.type)}.{arm.variant}{binds}"
        return arm.variant + binds

    def assign_text(self, s: ast.Assign) -> str:
        """Return an assignment, keeping `x += 1` when that is how it was written."""
        target = expr_text(s.target)
        v = s.value
        if isinstance(v, ast.BinOp) and v.op in AUGMENTED and v.left is s.target and s.annotation is None:
            return f"{target} {v.op}= {expr_text(v.right)}"  # the parser reuses the target node for `x += 1`
        ann = f": {type_text(s.annotation)}" if s.annotation else ""
        return f"{target}{ann} = {_bare_tuple(v)}"

    def signature(self, f: ast.FnDef) -> str:
        """Return `fn name(params) -> ret` for a function or method."""
        params = (["self"] if f.has_self else []) + [
            f"{p.name}: {type_text(p.type)}" + (f" = {expr_text(p.default)}" if p.default is not None else "")
            for p in f.params
        ]
        ret = f" -> {type_text(f.ret)}" if f.ret else ""
        return f"fn {f.name}({', '.join(params)}){ret}"

    def if_stmt(self, s: ast.If, indent: int, first_in_block: bool, kw: str) -> None:
        """Write an `if` with its `elif` and `else` branches."""
        self.header(s, indent, f"{kw} {expr_text(s.cond)}", first_in_block)
        self.block(s.then, indent + 1)
        if len(s.else_) == 1 and isinstance(s.else_[0], ast.If) and s.else_[0].col == s.col:
            self.if_stmt(s.else_[0], indent, False, "elif")  # an `elif` sits at the `if`'s own column
        elif s.else_:
            self.add(INDENT * indent + "else:")
            self.block(s.else_, indent + 1)

    def struct(self, s: ast.StructDef, indent: int, first_in_block: bool) -> None:
        """Write a struct: its fields, then its methods separated by blank lines."""
        self.header(s, indent, f"struct {s.name}", first_in_block)
        if s.doc is not None:
            self.line(s.doc_line, indent + 1, quote(s.doc), first_in_block=True)
        for n, f in enumerate(s.fields):
            default = f" = {expr_text(f.default)}" if f.default is not None else ""
            self.line(f.line, indent + 1, f"{f.name}: {type_text(f.type)}{default}", first_in_block=n == 0 and s.doc is None)
            if f.doc is not None:
                self.line(f.doc_line, indent + 1, quote(f.doc))
        for m in s.methods:
            self.blank()
            self.stmt(m, indent + 1, first_in_block=True)

