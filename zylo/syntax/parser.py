"""Parser: tokens to a syntax tree.

A hand-written recursive-descent parser. Statements are dispatched on their leading keyword; expressions use
precedence climbing. It knows the grammar only: whether names and types exist is the checker's concern.
"""

import dataclasses

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.syntax.lexer import Token, tokenize, unescape

# precedence: higher binds tighter. `not` is unary and handled in parse_unary.
PRECEDENCE = {
    "or": 1,
    "and": 2,
    "==": 3, "!=": 3,
    "<": 4, ">": 4, "<=": 4, ">=": 4,
    "in": 4, "not in": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
}
NOT_PREC = 3  # `not a == b` is `not (a == b)`
IS_PREC = 3  # `x is some and y` is `(x is some) and y`
IS_KINDS = {"some", "none", "ok", "err"}
AUG_OPS = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%"}
KIND_NAMES = {
    "NEWLINE": "end of line", "EOF": "end of file", "INDENT": "indentation",
    "DEDENT": "end of block", "IDENT": "a name", "INT": "an integer",
    "FLOAT": "a decimal", "STR": "a string",
}


def describe(tok: Token) -> str:
    """Describe a token for an error message.

    Args:
        tok: The token that was found.

    Returns:
        Its text in quotes, or a readable name such as "end of line" for tokens without text.
    """
    return repr(tok.value) if tok.value else KIND_NAMES.get(tok.kind, tok.kind)


class Parser:
    """Recursive-descent parser over a token list.

    Use `parse()` rather than this class directly.
    """
    def __init__(self, tokens: list[Token]):
        """Create a parser.

        Args:
            tokens: The token list produced by `tokenize`.
        """
        self.toks = tokens
        self.pos = 0

    # ---- utilidades -------------------------------------------------------------

    @property
    def cur(self) -> Token:
        """The token being looked at."""
        return self.toks[self.pos]

    def peek(self, offset: int = 1) -> Token:
        """Look ahead without consuming.

        Args:
            offset: How many tokens ahead (1 is the next one).

        Returns:
            That token, or the last token if it is past the end.
        """
        return self.toks[min(self.pos + offset, len(self.toks) - 1)]

    def advance(self) -> Token:
        """Consume and return the current token."""
        tok = self.cur
        self.pos += 1
        return tok

    def check(self, kind: str, value: str | None = None) -> bool:
        """Test the current token.

        Args:
            kind: Expected token kind.
            value: Expected text, or None to accept any.

        Returns:
            True if the current token matches.
        """
        tok = self.cur
        return tok.kind == kind and (value is None or tok.value == value)

    def match(self, kind: str, value: str | None = None) -> Token | None:
        """Consume the current token if it matches.

        Args:
            kind: Expected token kind.
            value: Expected text, or None to accept any.

        Returns:
            The consumed token, or None when it did not match.
        """
        return self.advance() if self.check(kind, value) else None

    def expect(self, kind: str, value: str | None = None) -> Token:
        """Consume the current token, which must match.

        Args:
            kind: Expected token kind.
            value: Expected text, or None to accept any.

        Returns:
            The consumed token.

        Raises:
            ZyloError: If the current token does not match.
        """
        tok = self.cur
        if tok.kind != kind or (value is not None and tok.value != value):
            want = repr(value) if value else KIND_NAMES.get(kind, kind)
            raise ZyloError(f"expected {want}, found {describe(tok)}", tok.line, tok.col)
        return self.advance()

    # ---- programa y bloques -------------------------------------------------------

    def parse_program(self) -> ast.Program:
        """Parse a whole module.

        Returns:
            The program: every top-level statement in order.
        """
        body = []
        while not self.check("EOF"):
            body.append(self.parse_stmt())
        return ast.Program(body, line=1, col=1)

    def parse_block(self) -> list[ast.Stmt]:
        """Parse `: NEWLINE INDENT statements DEDENT`.

        Returns:
            The statements of the indented block.

        Raises:
            ZyloError: If the block is missing or never closed.
        """
        self.expect("OP", ":")
        self.expect("NEWLINE")
        if not self.check("INDENT"):
            raise ZyloError("expected an indented block", self.cur.line, self.cur.col)
        self.advance()
        body = []
        while not self.check("DEDENT"):
            if self.check("EOF"):
                raise ZyloError("unclosed block", self.cur.line, self.cur.col)
            body.append(self.parse_stmt())
        self.expect("DEDENT")
        return body

    # ---- sentencias ---------------------------------------------------------------

    def parse_stmt(self) -> ast.Stmt:
        """Parse one statement, dispatching on its leading keyword.

        Returns:
            The statement node.
        """
        tok = self.cur
        if tok.kind == "INDENT":
            raise ZyloError("unexpected indentation", tok.line, tok.col)
        if tok.kind == "KW":
            match tok.value:
                case "fn":
                    return self.parse_fn()
                case "extern":
                    self.advance()
                    if not self.check("KW", "fn"):
                        raise ZyloError("expected 'fn' after 'extern'", self.cur.line, self.cur.col)
                    return self.parse_fn(extern=True)
                case "struct":
                    return self.parse_struct()
                case "import":
                    return self.parse_import()
                case "from":
                    return self.parse_from_import()
                case "if":
                    return self.parse_if()
                case "while":
                    return self.parse_while()
                case "for":
                    return self.parse_for()
                case "return":
                    return self.parse_return()
                case "break" | "continue":
                    self.advance()
                    self.expect("NEWLINE")
                    cls = ast.Break if tok.value == "break" else ast.Continue
                    return cls(line=tok.line, col=tok.col)
                case "else" | "elif":
                    raise ZyloError(f"{tok.value!r} without a preceding 'if'", tok.line, tok.col)
        return self.parse_simple()

    def tuple_tail(self, first: ast.Expr, start: Token) -> ast.Expr:
        """After `first`, a `,` makes a tuple: `a, b, c` (a trailing comma is allowed)."""
        if not self.check("OP", ","):
            return first
        items = [first]
        while self.match("OP", ","):
            if self.check("NEWLINE") or self.check("OP", "=") or self.check("OP", ")"):
                break
            items.append(self.parse_expr())
        if len(items) < 2:
            raise ZyloError("a tuple needs at least 2 elements", start.line, start.col)
        return ast.TupleLit(items, line=start.line, col=start.col)

    def parse_simple(self) -> ast.Stmt:
        """Expression, assignment (`=`, `x: T =`), unpacking (`a, b = ...`) or compound assignment (`+=`)."""
        start = self.cur
        expr = self.tuple_tail(self.parse_expr(), start)
        if isinstance(expr, ast.TupleLit) and self.check("OP", "="):
            if not all(isinstance(t, ast.Name) for t in expr.items):
                raise ZyloError("can only unpack into plain variable names", start.line, start.col)
            self.advance()
            value = self.tuple_tail(self.parse_expr(), start)
            self.expect("NEWLINE")
            return ast.Unpack(expr.items, value, line=start.line, col=start.col)
        if isinstance(expr, ast.Name) and self.check("OP", ":"):
            self.advance()
            ann = self.parse_type()
            self.expect("OP", "=")
            value = self.tuple_tail(self.parse_expr(), start)
            self.expect("NEWLINE")
            return ast.Assign(expr, ann, value, line=start.line, col=start.col)
        if self.check("OP", "="):
            self._check_target(expr, start)
            self.advance()
            value = self.tuple_tail(self.parse_expr(), start)
            self.expect("NEWLINE")
            return ast.Assign(expr, None, value, line=start.line, col=start.col)
        if self.cur.kind == "OP" and self.cur.value in AUG_OPS:
            self._check_target(expr, start)
            op = AUG_OPS[self.advance().value]
            rhs = self.parse_expr()
            self.expect("NEWLINE")
            value = ast.BinOp(op, expr, rhs, line=start.line, col=start.col)
            return ast.Assign(expr, None, value, line=start.line, col=start.col)
        self.expect("NEWLINE")
        return ast.ExprStmt(expr, line=start.line, col=start.col)

    def _check_target(self, expr: ast.Expr, start: Token) -> None:
        if not isinstance(expr, (ast.Name, ast.Index, ast.Attr)):
            raise ZyloError("cannot assign to this expression", start.line, start.col)

    def parse_if(self) -> ast.If:
        """Parse an `if` statement with its `elif` and `else` branches."""
        kw = self.advance()  # `if` o `elif`
        cond = self.parse_expr()
        then = self.parse_block()
        else_: list[ast.Stmt] = []
        if self.check("KW", "elif"):
            else_ = [self.parse_if()]
        elif self.match("KW", "else"):
            else_ = self.parse_block()
        return ast.If(cond, then, else_, line=kw.line, col=kw.col)

    def parse_while(self) -> ast.While:
        """Parse a `while` loop."""
        kw = self.advance()
        cond = self.parse_expr()
        return ast.While(cond, self.parse_block(), line=kw.line, col=kw.col)

    def parse_for(self) -> ast.For:
        """Parse a `for` loop, including tuple unpacking (`for a, b in xs`)."""
        kw = self.advance()
        names = [self.expect("IDENT").value]
        while self.match("OP", ","):
            names.append(self.expect("IDENT").value)
        self.expect("KW", "in")
        it = self.parse_expr()
        body = self.parse_block()
        return ast.For(names[0], it, body, targets=names if len(names) > 1 else None, line=kw.line, col=kw.col)

    def parse_return(self) -> ast.Return:
        """Parse a `return`, with an optional value or tuple (`return a, b`)."""
        kw = self.advance()
        value = None if self.check("NEWLINE") else self.tuple_tail(self.parse_expr(), self.cur)
        self.expect("NEWLINE")
        return ast.Return(value, line=kw.line, col=kw.col)

    def parse_fn(self, in_struct: bool = False, extern: bool = False) -> ast.FnDef:
        """Parse a function, a method or an `extern fn`.

        Args:
            in_struct: True inside a struct body, where a leading `self` parameter is allowed.
            extern: True when the `extern` keyword was already consumed: the body is a Rust string after `=`.

        Returns:
            The function node (an `ExternFn` when `extern` is True).
        """
        kw = self.advance()
        name = self.expect("IDENT").value
        self.expect("OP", "(")
        params: list[ast.Param] = []
        has_self = False
        if not self.check("OP", ")"):
            if in_struct and self._is_self_param():
                self.advance()
                has_self = True
                if self.match("OP", ","):
                    params.append(self.parse_param())
            else:
                params.append(self.parse_param())
            while self.match("OP", ","):
                params.append(self.parse_param())
        self.expect("OP", ")")
        ret = self.parse_type() if self.match("OP", "->") else None
        if extern:
            self.expect("OP", "=")
            template = self.expect("STR").value
            self.expect("NEWLINE")
            return ast.ExternFn(name, params, ret, [], False, False, template, line=kw.line, col=kw.col)
        body = self.parse_block()
        return ast.FnDef(name, params, ret, body, has_self, line=kw.line, col=kw.col)

    def _is_self_param(self) -> bool:
        nxt = self.peek()
        return self.check("IDENT", "self") and not (nxt.kind == "OP" and nxt.value == ":")

    def parse_dotted(self) -> str:
        """Parse a dotted name such as `utils.text` and return it as one string."""
        name = self.expect("IDENT").value
        while self.check("OP", "."):
            self.advance()
            name += "." + self.expect("IDENT").value
        return name

    def parse_import(self) -> ast.Import:
        """Parse `import a.b [as c]`."""
        kw = self.advance()
        module = self.parse_dotted()
        alias = self.expect("IDENT").value if self.match("KW", "as") else None
        self.expect("NEWLINE")
        return ast.Import(module, alias, line=kw.line, col=kw.col)

    def parse_from_import(self) -> ast.FromImport:
        """Parse `from a.b import x [as y], ...`."""
        kw = self.advance()
        module = self.parse_dotted()
        self.expect("KW", "import")
        names: list[tuple[str, str | None]] = []
        while True:
            name = self.expect("IDENT").value
            alias = self.expect("IDENT").value if self.match("KW", "as") else None
            names.append((name, alias))
            if not self.match("OP", ","):
                break
        self.expect("NEWLINE")
        return ast.FromImport(module, names, line=kw.line, col=kw.col)

    def parse_struct(self) -> ast.StructDef:
        """Parse a struct: its fields (`name: type`) and methods (`fn`).

        Raises:
            ZyloError: If the body contains anything else.
        """
        self.advance()
        name = self.expect("IDENT")
        self.expect("OP", ":")
        self.expect("NEWLINE")
        if not self.check("INDENT"):
            raise ZyloError("expected an indented block", self.cur.line, self.cur.col)
        self.advance()
        fields: list[ast.Field] = []
        methods: list[ast.FnDef] = []
        while not self.check("DEDENT"):
            tok = self.cur
            if tok.kind == "EOF":
                raise ZyloError("unclosed block", tok.line, tok.col)
            if self.check("KW", "fn"):
                methods.append(self.parse_fn(in_struct=True))
            elif tok.kind == "IDENT" and self.peek().kind == "OP" and self.peek().value == ":":
                self.advance()
                self.advance()
                typ = self.parse_type()
                self.expect("NEWLINE")
                fields.append(ast.Field(tok.value, typ, line=tok.line, col=tok.col))
            else:
                raise ZyloError("a struct body can only contain fields (`name: type`) and methods (`fn`)", tok.line, tok.col)
        self.expect("DEDENT")
        return ast.StructDef(name.value, fields, methods, line=name.line, col=name.col)

    def parse_param(self) -> ast.Param:
        """Parse one `name: type` parameter.

        Raises:
            ZyloError: If the type is missing, or the name is `self` outside a method.
        """
        name = self.expect("IDENT")
        if name.value == "self":
            raise ZyloError("`self` can only be the first parameter of a method, inside a struct", name.line, name.col)
        if not self.check("OP", ":"):
            raise ZyloError(f"parameter {name.value!r} needs a type (`{name.value}: int`)", name.line, name.col)
        self.advance()
        return ast.Param(name.value, self.parse_type(), line=name.line, col=name.col)

    # ---- tipos --------------------------------------------------------------------

    def parse_type(self) -> ast.TypeRef:
        """Parse a type: `int`, `list[T]`, `tuple[A, B]`, `dict[K, V]`, `set[T]`, `Result[T, E]`, `mod.Struct`, with `?` suffixes.

        Raises:
            ZyloError: On a nested optional (`T??`) or a malformed type.
        """
        tok = self.expect("IDENT")
        name = tok.value
        while self.check("OP", ".") and self.peek().kind == "IDENT":  # qualified: module.Struct
            self.advance()
            name += "." + self.advance().value
        args: list[ast.TypeRef] = []
        if name == "list":
            self.expect("OP", "[")
            args.append(self.parse_type())
            self.expect("OP", "]")
        elif name == "set":
            self.expect("OP", "[")
            args.append(self.parse_type())
            self.expect("OP", "]")
        elif name == "dict":
            self.expect("OP", "[")
            args.append(self.parse_type())
            self.expect("OP", ",")
            args.append(self.parse_type())
            self.expect("OP", "]")
        elif name == "tuple":
            self.expect("OP", "[")
            args.append(self.parse_type())
            while self.match("OP", ","):
                args.append(self.parse_type())
            self.expect("OP", "]")
            if len(args) < 2:
                raise ZyloError("a tuple type needs at least 2 element types", tok.line, tok.col)
        elif name == "Result":
            self.expect("OP", "[")
            args.append(self.parse_type())
            self.expect("OP", ",")
            args.append(self.parse_type())
            self.expect("OP", "]")
        t = ast.TypeRef(name, args, line=tok.line, col=tok.col)
        while self.check("OP", "?"):  # `T?` is Option; internally TypeRef("?", [T])
            q = self.advance()
            if t.name == "?":
                raise ZyloError("nested Option types (`T??`) are not allowed", q.line, q.col)
            t = ast.TypeRef("?", [t], line=t.line, col=t.col)
        return t

    # ---- expresiones ----------------------------------------------------------------

    def parse_expr(self, min_prec: int = 1) -> ast.Expr:
        """Parse an expression with precedence climbing.

        Args:
            min_prec: Only operators binding at least this tightly are consumed.

        Returns:
            The expression node.
        """
        left = self.parse_unary()
        while True:
            if self.check("KW", "is") and IS_PREC >= min_prec:
                left = self.parse_is(left)
                continue
            if self.check("KW", "not") and self.peek().kind == "KW" and self.peek().value == "in":
                if PRECEDENCE["not in"] < min_prec:
                    return left
                op = self.advance()
                self.advance()
                right = self.parse_expr(PRECEDENCE["not in"] + 1)
                left = ast.BinOp("not in", left, right, line=op.line, col=op.col)
                continue
            if not (self._is_binop() and PRECEDENCE[self.cur.value] >= min_prec):
                return left
            op = self.advance()
            right = self.parse_expr(PRECEDENCE[op.value] + 1)
            left = ast.BinOp(op.value, left, right, line=op.line, col=op.col)

    def parse_is(self, left: ast.Expr) -> ast.IsCheck:
        """Parse the right side of `x is some|none|ok|err`.

        Args:
            left: The already parsed left operand.

        Returns:
            The `IsCheck` node.
        """
        kw = self.advance()
        tok = self.cur
        if tok.value not in IS_KINDS or tok.kind not in ("IDENT", "KW"):
            raise ZyloError("`is` must be followed by some, none, ok or err", tok.line, tok.col)
        self.advance()
        return ast.IsCheck(left, tok.value, line=kw.line, col=kw.col)

    def _is_binop(self) -> bool:
        return self.cur.kind in ("OP", "KW") and self.cur.value in PRECEDENCE

    def parse_unary(self) -> ast.Expr:
        """Parse a prefix `-` or `not`, or fall through to a postfix expression."""
        tok = self.cur
        if tok.kind == "OP" and tok.value == "-":
            self.advance()
            return ast.UnaryOp("-", self.parse_unary(), line=tok.line, col=tok.col)
        if tok.kind == "KW" and tok.value == "not":
            self.advance()
            return ast.UnaryOp("not", self.parse_expr(NOT_PREC), line=tok.line, col=tok.col)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        """Parse a primary expression followed by any indexes, slices, attributes, method calls and `?`."""
        expr = self.parse_primary()
        while True:
            if self.check("OP", "["):
                lb = self.advance()
                lo = None if self.check("OP", ":") else self.parse_expr()
                if self.match("OP", ":"):
                    hi = None if self.check("OP", "]") else self.parse_expr()
                    self.expect("OP", "]")
                    expr = ast.Slice(expr, lo, hi, line=lb.line, col=lb.col)
                    continue
                self.expect("OP", "]")
                expr = ast.Index(expr, lo, line=lb.line, col=lb.col)
            elif self.check("OP", "?"):
                q = self.advance()
                expr = ast.Try(expr, line=q.line, col=q.col)
            elif self.check("OP", "."):
                self.advance()
                name = self.expect("IDENT")
                if self.check("OP", "("):
                    expr = ast.MethodCall(expr, name.value, self.parse_args(), line=name.line, col=name.col)
                else:
                    expr = ast.Attr(expr, name.value, line=name.line, col=name.col)
            else:
                return expr

    def parse_primary(self) -> ast.Expr:
        """Parse a literal, name, call, parenthesised expression or tuple, list, dict or set display.

        Raises:
            ZyloError: If the token cannot start an expression.
        """
        tok = self.advance()
        pos = {"line": tok.line, "col": tok.col}
        if tok.kind == "INT":
            return ast.Int(int(tok.value), **pos)
        if tok.kind == "FLOAT":
            return ast.Float(float(tok.value), **pos)
        if tok.kind == "STR":
            return ast.Str(tok.value, **pos)
        if tok.kind == "FSTR":
            return self.parse_fstring(tok)
        if tok.kind == "KW" and tok.value in ("true", "false"):
            return ast.Bool(tok.value == "true", **pos)
        if tok.kind == "KW" and tok.value == "none":
            return ast.NoneLit(**pos)
        if tok.kind == "IDENT":
            if self.check("OP", "("):
                return self.parse_call(tok)
            return ast.Name(tok.value, **pos)
        if tok.kind == "OP" and tok.value == "(":
            e = self.tuple_tail(self.parse_expr(), tok)
            self.expect("OP", ")")
            return e
        if tok.kind == "OP" and tok.value == "{":
            return self.parse_brace(tok)
        if tok.kind == "OP" and tok.value == "[":
            items: list[ast.Expr] = []
            if not self.check("OP", "]"):
                items.append(self.parse_expr())
                while self.match("OP", ","):
                    items.append(self.parse_expr())
            self.expect("OP", "]")
            return ast.ListLit(items, **pos)
        raise ZyloError(f"unexpected expression: {describe(tok)}", tok.line, tok.col)

    def parse_brace(self, tok: Token) -> ast.Expr:
        """`{}` (empty dict or set, typed by context), `{k: v, ...}` (dict) or `{a, b, ...}` (set)."""
        pos = {"line": tok.line, "col": tok.col}
        if self.match("OP", "}"):
            return ast.DictLit([], [], **pos)
        first = self.parse_expr()
        if self.match("OP", ":"):
            keys, values = [first], [self.parse_expr()]
            while self.match("OP", ","):
                if self.check("OP", "}"):
                    break
                keys.append(self.parse_expr())
                self.expect("OP", ":")
                values.append(self.parse_expr())
            self.expect("OP", "}")
            return ast.DictLit(keys, values, **pos)
        items = [first]
        while self.match("OP", ","):
            if self.check("OP", "}"):
                break
            items.append(self.parse_expr())
        self.expect("OP", "}")
        return ast.SetLit(items, **pos)

    def parse_fstring(self, tok: Token) -> ast.FString:
        """Split an f-string into literal text and `{expression:spec}` fields.

        Args:
            tok: The raw f-string token.

        Returns:
            The `FString` node.

        Raises:
            ZyloError: On an unterminated or empty field, or a stray `}`.
        """
        raw, parts, buf, i = tok.value, [], [], 0

        def flush() -> None:
            if buf:
                parts.append(unescape("".join(buf), tok.line, tok.col))
                buf.clear()

        def fail(msg: str, at: int) -> ZyloError:
            return ZyloError(msg, tok.line, tok.col + 2 + at)

        while i < len(raw):
            c = raw[i]
            if c == "{" and raw[i + 1:i + 2] == "{":
                buf.append("{")
                i += 2
            elif c == "}" and raw[i + 1:i + 2] == "}":
                buf.append("}")
                i += 2
            elif c == "}":
                raise fail("single '}' in f-string (write '}}' for a literal brace)", i)
            elif c == "{":
                j, depth, quote = i + 1, 0, None
                while j < len(raw):
                    ch = raw[j]
                    if quote:
                        quote = None if ch == quote else quote
                    elif ch in "\"'":
                        quote = ch
                    elif ch in "([{":
                        depth += 1
                    elif ch in ")]":
                        depth -= 1
                    elif ch == "}":
                        if depth == 0:
                            break
                        depth -= 1
                    j += 1
                else:
                    raise fail("unterminated '{' in f-string", i)
                inner = raw[i + 1:j]
                expr_text, spec = self.split_spec(inner)
                if not expr_text.strip():
                    raise fail("empty expression in f-string", i)
                flush()
                parts.append(ast.FormatPart(self.sub_expr(expr_text, tok, i + 1), spec, line=tok.line, col=tok.col + 2 + i))
                i = j + 1
            else:
                buf.append(c)
                i += 1
        flush()
        return ast.FString(parts, line=tok.line, col=tok.col)

    @staticmethod
    def split_spec(inner: str) -> tuple[str, str]:
        """Split `expr:spec` at the first top-level colon.

        Args:
            inner: The text between the braces of an f-string field.

        Returns:
            The expression text and the format specification (empty when there is none).
        """
        depth, quote = 0, None
        for k, ch in enumerate(inner):
            if quote:
                quote = None if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == ":" and depth == 0:
                return inner[:k], inner[k + 1:]
        return inner, ""

    def sub_expr(self, text: str, tok: Token, offset: int) -> ast.Expr:
        """Parse the expression inside `{...}`; its tokens are placed at the f-string's position."""
        stripped = text.strip()
        try:
            toks = tokenize(stripped)
        except ZyloError as e:
            raise ZyloError(e.msg, tok.line, tok.col + 2 + offset) from None
        base = tok.col + 2 + offset + (len(text) - len(text.lstrip()))
        toks = [dataclasses.replace(t, line=tok.line, col=base + max(t.col - 1, 0)) for t in toks]
        sub = Parser(toks)
        expr = sub.parse_expr()
        if not sub.check("NEWLINE"):
            raise ZyloError(f"unexpected {describe(sub.cur)} in f-string expression", tok.line, base)
        return expr

    def parse_args(self) -> list[ast.Expr]:
        """Parse a parenthesised, comma-separated argument list."""
        self.expect("OP", "(")
        args: list[ast.Expr] = []
        if not self.check("OP", ")"):
            args.append(self.parse_expr())
            while self.match("OP", ","):
                args.append(self.parse_expr())
        self.expect("OP", ")")
        return args

    def parse_call(self, name: Token) -> ast.Call:
        """Parse the arguments of a call to the function named by `name`."""
        return ast.Call(name.value, self.parse_args(), line=name.line, col=name.col)


def parse(src: str, file: str | None = None) -> ast.Program:
    """Parse a module. `file` (if given) is stamped on every node and on syntax errors."""
    try:
        prog = Parser(tokenize(src)).parse_program()
    except ZyloError as e:
        if e.file is None:
            e.file = file
        raise
    if file is not None:
        ast.stamp(prog, file)
    return prog
