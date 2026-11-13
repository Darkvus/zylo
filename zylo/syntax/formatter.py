"""Formatter: prints a Zylo program in the canonical style.

The formatter parses the source and prints the syntax tree again, so formatting can never change what a program
means; the tests check that the tree is identical before and after and that formatting twice changes nothing.
Comments are not part of the tree: they are collected from the source and put back next to the statements they
were written beside.

Style: four-space indentation, one space around binary operators and after commas, double-quoted strings, one blank
line around top-level definitions and between methods, and at most one consecutive blank line. A line longer than the
line length (88 by default) is wrapped: bracketed lists go one item per line with a trailing comma, long boolean,
arithmetic and method-chain expressions are broken inside parentheses, and long `match` or-patterns and imports are
wrapped in parentheses.
"""

from decimal import Decimal

from zylo.domain import ast
from zylo.syntax.parser import parse

INDENT = "    "
DEFAULT_LINE_LENGTH = 88
DQ = '"'

# Precedence of the binary operators; the same table the parser uses.
PREC = {"or": 1, "and": 2, "==": 3, "!=": 3, "<": 4, ">": 4, "<=": 4, ">=": 4, "in": 4, "not in": 4,
        "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}
IS_PREC = 3
NOT_PREC = 3
ATOM = 100  # postfix expressions and literals never need parentheses
AUGMENTED = {"+", "-", "*", "/", "%"}
ESCAPES = {"\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0"}


def format_source(src: str, line_length: int = DEFAULT_LINE_LENGTH) -> str:
    """Format a Zylo source text.

    Args:
        src: The program text.
        line_length: Lines that would be longer than this are wrapped (when they can be: a long string stays whole).

    Returns:
        The formatted text, ending with a single newline (empty for an empty program).

    Raises:
        ZyloError: If the program has a syntax error.
    """
    return _Formatter(src, line_length).run()


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


def tparams_text(tparams: list[ast.TypeParam]) -> str:
    """Return the type parameters of a definition as written (`[T, K: Ord + Show]`), or nothing."""
    if not tparams:
        return ""
    return "[" + ", ".join(p.name + (": " + " + ".join(p.bounds) if p.bounds else "") for p in tparams) + "]"


def targs_text(targs: list[ast.TypeRef]) -> str:
    """Return explicit type arguments as written (`[int, str]`), or nothing."""
    return f"[{', '.join(type_text(a) for a in targs)}]" if targs else ""


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
        case ast.Name(i, targs=targs):
            return i + targs_text(targs), ATOM
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
        case ast.Call(func, args, named=named, targs=targs):
            return f"{func}{targs_text(targs)}({_args(args, q, named)})", ATOM
        case ast.CallValue(callee, args):
            return f"{expr_text(callee, ATOM, q)}({_args(args, q)})", ATOM
        case ast.Lambda():
            params = ", ".join(p.name + (f": {type_text(p.type)}" if p.type else "") for p in e.params)
            ret = f" -> {type_text(e.ret)}" if e.ret else ""
            return f"fn({params}){ret} => {expr_text(e.expr, 0, q)}", 0
        case ast.MethodCall(v, name, args, named=named, targs=targs):
            return f"{expr_text(v, ATOM, q)}.{name}{targs_text(targs)}({_args(args, q, named)})", ATOM
        case ast.Attr(v, name, targs=targs):
            return f"{expr_text(v, ATOM, q)}.{name}{targs_text(targs)}", ATOM
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



# ---- line wrapping -------------------------------------------------------------------------------------

def _pad(col: int) -> str:
    return " " * col


def _end_col(text: str, first: int) -> int:
    """The column after `text` that starts at column `first`."""
    return len(text.rsplit("\n", 1)[-1]) if "\n" in text else first + len(text)


class _Layout:
    """Prints expressions that do not fit on one line.

    An expression is first tried on one line. When it does not fit, the outermost construct is broken: the items of a
    call or literal go one per line with a trailing comma, the operands of a boolean or arithmetic expression and the
    links of a method chain go one per line inside parentheses. Each part is then tried flat again, recursively.

    Every method takes the column where the text starts (`first`), the indentation of the line it starts on (`ind`) and
    the width of what must follow it on its last line (`last`), such as `:` or a comma. `ctx` says where an operator
    expression sits: `"stmt"` (no bracket around it yet, so parentheses are added) or `"item"` (inside brackets already,
    where its lines continue at the indentation of the first).
    """

    def __init__(self, limit: int):
        self.limit = limit

    def expr(self, e: ast.Expr, ind: int, first: int, last: int, min_prec: int = 0, ctx: str = "item") -> str:
        """Print an expression starting at column `first`, wrapping it if it is too long."""
        flat = expr_text(e, min_prec)
        if first + len(flat) + last <= self.limit:
            return flat
        if _expr(e, '"')[1] < min_prec:  # the grammar needs parentheses here: use them to break the line
            return "(\n" + _pad(ind + 4) + self.expr(e, ind + 4, ind + 4, 0, 0, "item") + "\n" + _pad(ind) + ")"
        return self.broken(e, ind, first, last, ctx)

    def broken(self, e: ast.Expr, ind: int, first: int, last: int, ctx: str) -> str:
        """Break the outermost construct of `e`."""
        match e:
            case ast.BinOp():
                return self.operators(self.binop_parts(e), ind, first, last, ctx)
            case ast.UnaryOp("not", x):
                return "not " + self.expr(x, ind, first + 4, last, NOT_PREC, ctx)
            case ast.UnaryOp(op, x):
                return op + self.expr(x, ind, first + 1, last, ATOM, ctx)
            case ast.IsCheck(v, kind):
                return self.expr(v, ind, first, last + len(f" is {kind}"), IS_PREC, ctx) + f" is {kind}"
            case ast.Try(v):
                return self.expr(v, ind, first, last + 1, ATOM, ctx) + "?"
            case ast.Attr(v, name, targs=targs):
                tail = f".{name}{targs_text(targs)}"
                return self.expr(v, ind, first, last + len(tail), ATOM, ctx) + tail
            case ast.Index() | ast.Slice():
                tail = expr_text(e)[len(expr_text(e.value, ATOM)):]
                receiver = self.expr(e.value, ind, first, last + len(tail), ATOM, ctx)
                if isinstance(e, ast.Index) and _end_col(receiver, first) + len(tail) + last > self.limit:
                    inner = ind + 4  # the receiver cannot shrink: break inside the brackets instead
                    return receiver + "[\n" + _pad(inner) + self.expr(e.index, inner, inner, 0) + "\n" + _pad(ind) + "]"
                return receiver + tail
            case ast.Lambda() if e.expr is not None:
                head = expr_text(e)[:-len(expr_text(e.expr))]
                return head + self.expr(e.expr, ind, first + len(head), last, 0, ctx)
            case ast.Call(func, args, named=named, targs=targs):
                return self.bracket(f"{func}{targs_text(targs)}(", self.items(args, named), ")", ind, first, last)
            case ast.CallValue(callee, args):
                head = self.expr(callee, ind, first, last + 1, ATOM, ctx)
                return self.bracket(head + "(", self.items(args), ")", ind, _end_col(head, first), last)
            case ast.MethodCall():
                if self.splits_into_lines(e, ind, first, last, ctx):
                    return self.operators(self.chain_parts(e), ind, first, last, ctx)
                name = e.name + targs_text(e.targs)
                if not e.args and not e.named:
                    return self.expr(e.value, ind, first, last + len(name) + 3, ATOM, ctx) + f".{name}()"
                head = self.expr(e.value, ind, first, last + len(name) + 2, ATOM, ctx) + f".{name}("
                return self.bracket(head, self.items(e.args, e.named), ")", ind, _end_col(head, first), last)
            case ast.ListLit(items):
                return self.bracket("[", self.items(items), "]", ind, first, last)
            case ast.TupleLit(items):
                return self.bracket("(", self.items(items), ")", ind, first, last)
            case ast.SetLit(items):
                return self.bracket("{", self.items(items), "}", ind, first, last)
            case ast.DictLit(keys, values):
                return self.bracket("{", [(expr_text(k) + ": ", v) for k, v in zip(keys, values)], "}", ind, first, last)
        return expr_text(e)  # a string, a number, an f-string: it cannot be broken

    @staticmethod
    def items(args: list[ast.Expr], named: list[ast.NamedArg] | None = None) -> list[tuple[str, ast.Expr]]:
        """The `(prefix, expression)` items of an argument list."""
        return [("", a) for a in args] + [(f"{n.name}=", n.value) for n in named or []]

    def bracket(self, head: str, items: list[tuple[str, ast.Expr]], close: str, ind: int, first: int, last: int) -> str:
        """Print `head items close`: on the line if it fits, otherwise one item per line with a trailing comma."""
        if not items:
            return head + close
        flat = ", ".join(prefix + expr_text(x) for prefix, x in items)
        if _end_col(head, first) + len(flat) + len(close) + last <= self.limit:
            return head + flat + close
        inner = ind + 4
        lines = [_pad(inner) + prefix + self.expr(x, inner, inner + len(prefix), 1) + "," for prefix, x in items]
        return head + "\n" + "\n".join(lines) + "\n" + _pad(ind) + close

    @staticmethod
    def binop_parts(e: ast.BinOp) -> list:
        """The operands of a chain of operators of the same strength, each with its operator and required precedence."""
        p = PREC[e.op]
        rights = []
        node: ast.Expr = e
        while isinstance(node, ast.BinOp) and PREC[node.op] == p:
            quiet = node.op in ("and", "or") and isinstance(node.right, ast.UnaryOp) and node.right.op == "not"
            rights.append((node.op + " ", node.right, 0 if quiet else p + 1))
            node = node.left
        return [lambda lay, ind, first, last, c, n=node: lay.expr(n, ind, first, last, p, c)] + [
            (lambda lay, ind, first, last, c, op=op, n=n, mp=mp: op + lay.expr(n, ind, first + len(op), last, mp, c))
            for op, n, mp in reversed(rights)]

    def operators(self, parts: list, ind: int, first: int, last: int, ctx: str) -> str:
        """Print `parts` (one callable per line) joined by newlines; inside parentheses when nothing encloses them."""
        if ctx == "stmt":
            at = ind + 4
            body = [p(self, at, at, 0, "item") for p in parts]
            return "(\n" + _pad(at) + ("\n" + _pad(at)).join(body) + "\n" + _pad(ind) + ")"
        out = [parts[0](self, ind, first, last if len(parts) == 1 else 0, "item")]
        out += [p(self, ind, ind, last if n == len(parts) - 2 else 0, "item") for n, p in enumerate(parts[1:])]
        return ("\n" + _pad(ind)).join(out)

    def chain(self, e: ast.Expr) -> tuple[ast.Expr, list[tuple[str, ast.Expr]]]:
        """Split `a.b(x).c.d(y)?[0]` into its base `a` and its links, innermost first."""
        links: list[tuple[str, ast.Expr]] = []
        while True:
            kind = {ast.MethodCall: "m", ast.Attr: "a", ast.Try: "t", ast.Index: "i", ast.Slice: "i", ast.CallValue: "c"}.get(type(e))
            if kind is None:
                break
            links.append((kind, e))
            e = e.callee if kind == "c" else e.value
        links.reverse()
        return e, links

    def chain_segments(self, e: ast.Expr) -> tuple[ast.Expr, list[list]]:
        """The base of a method chain and its lines: a `.name` that follows a call starts a new line."""
        base, links = self.chain(e)
        segments: list[list] = [[]]
        after_call = False
        for kind, node in links:
            if kind in ("m", "a") and after_call:
                segments.append([])
            segments[-1].append((kind, node))
            after_call = kind in ("m", "c") or (kind in ("t", "i") and after_call)
        return base, segments

    def splits_into_lines(self, e: ast.Expr, ind: int, first: int, last: int, ctx: str) -> bool:
        """Whether a method chain of two or more calls reads better one call per line.

        That is when every line then fits; otherwise it is better to break the arguments of the call that is too long
        (`json.parse(` then the argument on its own line, then `).unwrap()`).
        """
        base, links = self.chain(e)
        if sum(kind == "m" for kind, _ in links) < 2:
            return False
        base, segments = self.chain_segments(e)
        for n, seg in enumerate(segments):
            width = ind + 4 if ctx == "stmt" else (first if n == 0 else ind)
            width += len(expr_text(base, ATOM)) if n == 0 else 0
            width += sum(len(self.link_flat(k, x)) for k, x in seg)
            if width + (last if n == len(segments) - 1 and ctx != "stmt" else 0) > self.limit:
                return False
        return len(segments) > 1

    def chain_parts(self, e: ast.Expr) -> list:
        """The lines of a method chain."""
        base, segments = self.chain_segments(e)
        parts = []
        for n, seg in enumerate(segments):
            parts.append(lambda lay, ind, first, last, c, n=n, seg=seg: lay.segment(base if n == 0 else None, seg, ind, first, last))
        return parts

    def segment(self, base: ast.Expr | None, seg: list, ind: int, first: int, last: int) -> str:
        """Print one line of a method chain: the base (first line only) and some links."""
        flats = [self.link_flat(k, n) for k, n in seg]
        rest = sum(len(f) for f in flats)
        text = ""
        if base is not None:
            text = self.expr(base, ind, first, last + rest, ATOM)
        for i, (kind, node) in enumerate(seg):
            col = _end_col(text, first)
            after = last + sum(len(f) for f in flats[i + 1:])
            if kind == "m" and (node.args or node.named):
                text += self.bracket(f".{node.name}{targs_text(node.targs)}(", self.items(node.args, node.named), ")", ind, col, after)
            elif kind == "c" and node.args:
                text += self.bracket("(", self.items(node.args), ")", ind, col, after)
            else:
                text += flats[i]
        return text

    @staticmethod
    def link_flat(kind: str, node: ast.Expr) -> str:
        """A link of a chain written on one line."""
        match kind:
            case "m":
                return f".{node.name}{targs_text(node.targs)}({_args(node.args, DQ, node.named)})"
            case "a":
                return f".{node.name}{targs_text(node.targs)}"
            case "t":
                return "?"
            case "c":
                return f"({_args(node.args, DQ)})"
        return expr_text(node)[len(expr_text(node.value, ATOM)):]


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

    def __init__(self, src: str, line_length: int = DEFAULT_LINE_LENGTH):
        self.prog = parse(src)
        self.layout = _Layout(line_length)
        self.limit = line_length
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
        self.out.extend(text.split("\n"))

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
        first_line, _, rest = text.partition("\n")  # a wrapped statement: the comment goes on its first line
        self.add(INDENT * indent + first_line + trailing + ("\n" + rest if rest else ""))

    def fit(self, indent: int, prefix: str, e: ast.Expr, last: int = 0) -> str:
        """Return `prefix` followed by the expression, wrapped if the line would be too long.

        Args:
            indent: Indentation level of the statement.
            prefix: The text before the expression on its line (`x = `, `if `).
            e: The expression.
            last: The width of the text that follows it on its last line (a `:`).
        """
        col = indent * len(INDENT)
        return prefix + self.layout.expr(e, col, col + len(prefix), last, 0, "stmt")

    def value(self, indent: int, prefix: str, e: ast.Expr, last: int = 0) -> str:
        """Like `fit` for the right side of `=` or `return`, where a tuple is written without parentheses."""
        if not isinstance(e, ast.TupleLit):
            return self.fit(indent, prefix, e, last)
        col = indent * len(INDENT)
        flat = prefix + _bare_tuple(e)
        if col + len(flat) + last <= self.limit:
            return flat
        return prefix + self.layout.bracket("(", self.layout.items(e.items), ")", col, col + len(prefix), last)

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
                self.line(s.line, indent, self.fit(indent, "", e), first_in_block)
            case ast.Assign(value=ast.Lambda(name=str()) as lam):  # a local `fn name(...)`
                params = [f"{p.name}: {type_text(p.type)}" for p in lam.params]
                ret = f" -> {type_text(lam.ret)}" if lam.ret else ""
                self.header(s, indent, self.wrap_signature(indent, f"fn {lam.name}", params, ret, 1), first_in_block)
                self.block(lam.body, indent + 1)
            case ast.Assign():
                self.line(s.line, indent, self.assign_text(s, indent), first_in_block)
            case ast.Unpack(targets, value):
                self.line(s.line, indent, self.value(indent, f"{', '.join(t.id for t in targets)} = ", value), first_in_block)
            case ast.If():
                self.if_stmt(s, indent, first_in_block, "if")
            case ast.While(cond, body):
                self.header(s, indent, self.fit(indent, "while ", cond, 1), first_in_block)
                self.block(body, indent + 1)
            case ast.For():
                names = ", ".join(s.targets) if s.targets else s.var
                self.header(s, indent, self.fit(indent, f"for {names} in ", s.iter, 1), first_in_block)
                self.block(s.body, indent + 1)
            case ast.Return(value):
                self.line(s.line, indent, "return" if value is None else self.value(indent, "return ", value), first_in_block)
            case ast.Break():
                self.line(s.line, indent, "break", first_in_block)
            case ast.Continue():
                self.line(s.line, indent, "continue", first_in_block)
            case ast.ExternFn():
                tail = f" = {quote(s.template)}"
                self.line(s.line, indent, "extern " + self.signature(s, indent, len(tail), 7) + tail, first_in_block)
            case ast.FnDef():
                self.header(s, indent, self.signature(s, indent, 1), first_in_block)
                self.block(s.body, indent + 1)
            case ast.StructDef():
                self.struct(s, indent, first_in_block)
            case ast.EnumDef():
                self.header(s, indent, f"enum {s.name}{tparams_text(s.tparams)}", first_in_block)
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
                self.header(s, indent, self.fit(indent, "match ", s.value, 1), first_in_block)
                for n, arm in enumerate(s.arms):
                    self.header(arm, indent + 1, self.arm_head(arm, indent + 1), first_in_block=n == 0)
                    self.block(arm.body, indent + 2)
            case ast.Import():
                self.line(s.line, indent, f"import {s.module}" + (f" as {s.alias}" if s.alias else ""), first_in_block)
            case ast.FromImport():
                names = [n + (f" as {a}" if a else "") for n, a in s.names]
                head = f"{'pub ' if s.pub else ''}from {s.module} import "
                flat = head + ", ".join(names)
                if len(INDENT) * indent + len(flat) > self.limit and len(names) > 1:
                    flat = head + "(\n" + "".join(f"{INDENT * (indent + 1)}{n},\n" for n in names) + INDENT * indent + ")"
                self.line(s.line, indent, flat, first_in_block)
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

    def assign_text(self, s: ast.Assign, indent: int) -> str:
        """Return an assignment, keeping `x += 1` when that is how it was written."""
        target = expr_text(s.target)
        v = s.value
        if isinstance(v, ast.BinOp) and v.op in AUGMENTED and v.left is s.target and s.annotation is None:
            return self.fit(indent, f"{target} {v.op}= ", v.right)  # the parser reuses the target node for `x += 1`
        ann = f": {type_text(s.annotation)}" if s.annotation else ""
        return self.value(indent, f"{target}{ann} = ", v)

    def signature(self, f: ast.FnDef, indent: int, last: int, lead: int = 0) -> str:
        """Return `fn name(params) -> ret` for a function or method, one parameter per line if it is too long.

        Args:
            f: The function.
            indent: Indentation level of the line it is written on.
            last: The width of what follows on the last line (`:`, or ` = "template"` for an `extern fn`).
            lead: The width of what precedes it on its line (`extern `).
        """
        params = (["self"] if f.has_self else []) + [
            f"{p.name}: {type_text(p.type)}" + (f" = {expr_text(p.default)}" if p.default is not None else "")
            for p in f.params
        ]
        ret = f" -> {type_text(f.ret)}" if f.ret else ""
        return self.wrap_signature(indent, f"fn {f.name}{tparams_text(f.tparams)}", params, ret, last, lead)

    def wrap_signature(self, indent: int, head: str, params: list[str], ret: str, last: int, lead: int = 0) -> str:
        """Write `head(params)ret` on one line, or with the parameters one per line when it does not fit."""
        flat = f"{head}({', '.join(params)}){ret}"
        if len(INDENT) * indent + lead + len(flat) + last <= self.limit or not params:
            return flat
        inner = INDENT * (indent + 1)
        return head + "(\n" + "".join(f"{inner}{p},\n" for p in params) + INDENT * indent + ")" + ret

    def arm_head(self, arm: ast.MatchArm, indent: int) -> str:
        """Write a `match` arm's patterns and guard; a long or-pattern goes in parentheses, one pattern per line."""
        pats = [self.pattern_text(a) for a in [arm, *arm.alts]]
        col = len(INDENT) * indent
        flat = " | ".join(pats)
        if len(pats) > 1 and col + len(flat) + 1 > self.limit:
            inner = INDENT * (indent + 1)
            head = "(\n" + inner + ("\n" + inner + "| ").join(pats) + "\n" + INDENT * indent + ")"
            guard_col = col + 1
        else:
            head, guard_col = flat, col + len(flat)
        if arm.guard is None:
            return head
        return head + " if " + self.layout.expr(arm.guard, col, guard_col + 4, 1, 0, "stmt")

    def if_stmt(self, s: ast.If, indent: int, first_in_block: bool, kw: str) -> None:
        """Write an `if` with its `elif` and `else` branches."""
        self.header(s, indent, self.fit(indent, f"{kw} ", s.cond, 1), first_in_block)
        self.block(s.then, indent + 1)
        if len(s.else_) == 1 and isinstance(s.else_[0], ast.If) and s.else_[0].col == s.col:
            self.if_stmt(s.else_[0], indent, False, "elif")  # an `elif` sits at the `if`'s own column
        elif s.else_:
            self.add(INDENT * indent + "else:")
            self.block(s.else_, indent + 1)

    def struct(self, s: ast.StructDef, indent: int, first_in_block: bool) -> None:
        """Write a struct: its fields, then its methods separated by blank lines."""
        self.header(s, indent, f"struct {s.name}{tparams_text(s.tparams)}", first_in_block)
        if s.doc is not None:
            self.line(s.doc_line, indent + 1, quote(s.doc), first_in_block=True)
        for n, f in enumerate(s.fields):
            decl = f"{f.name}: {type_text(f.type)}"
            text = self.fit(indent + 1, decl + " = ", f.default) if f.default is not None else decl
            self.line(f.line, indent + 1, text, first_in_block=n == 0 and s.doc is None)
            if f.doc is not None:
                self.line(f.doc_line, indent + 1, quote(f.doc))
        for m in s.methods:
            self.blank()
            self.stmt(m, indent + 1, first_in_block=True)

