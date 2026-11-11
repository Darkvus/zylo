from . import ast
from .errors import ZyloError
from .lexer import Token, tokenize

# precedence: higher binds tighter. `not` is unary and handled in parse_unary.
PRECEDENCE = {
    "or": 1,
    "and": 2,
    "==": 3, "!=": 3,
    "<": 4, ">": 4, "<=": 4, ">=": 4,
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
    return repr(tok.value) if tok.value else KIND_NAMES.get(tok.kind, tok.kind)


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.pos = 0

    # ---- utilidades -------------------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def peek(self, offset: int = 1) -> Token:
        return self.toks[min(self.pos + offset, len(self.toks) - 1)]

    def advance(self) -> Token:
        tok = self.cur
        self.pos += 1
        return tok

    def check(self, kind: str, value: str | None = None) -> bool:
        tok = self.cur
        return tok.kind == kind and (value is None or tok.value == value)

    def match(self, kind: str, value: str | None = None) -> Token | None:
        return self.advance() if self.check(kind, value) else None

    def expect(self, kind: str, value: str | None = None) -> Token:
        tok = self.cur
        if tok.kind != kind or (value is not None and tok.value != value):
            want = repr(value) if value else KIND_NAMES.get(kind, kind)
            raise ZyloError(f"expected {want}, found {describe(tok)}", tok.line, tok.col)
        return self.advance()

    # ---- programa y bloques -------------------------------------------------------

    def parse_program(self) -> ast.Program:
        body = []
        while not self.check("EOF"):
            body.append(self.parse_stmt())
        return ast.Program(body, line=1, col=1)

    def parse_block(self) -> list[ast.Stmt]:
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
        tok = self.cur
        if tok.kind == "INDENT":
            raise ZyloError("unexpected indentation", tok.line, tok.col)
        if tok.kind == "KW":
            match tok.value:
                case "fn":
                    return self.parse_fn()
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

    def parse_simple(self) -> ast.Stmt:
        """Expression, assignment (`=`, `x: T =`) or compound assignment (`+=`)."""
        start = self.cur
        expr = self.parse_expr()
        if isinstance(expr, ast.Name) and self.check("OP", ":"):
            self.advance()
            ann = self.parse_type()
            self.expect("OP", "=")
            value = self.parse_expr()
            self.expect("NEWLINE")
            return ast.Assign(expr, ann, value, line=start.line, col=start.col)
        if self.check("OP", "="):
            self._check_target(expr, start)
            self.advance()
            value = self.parse_expr()
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
        kw = self.advance()
        cond = self.parse_expr()
        return ast.While(cond, self.parse_block(), line=kw.line, col=kw.col)

    def parse_for(self) -> ast.For:
        kw = self.advance()
        var = self.expect("IDENT").value
        self.expect("KW", "in")
        it = self.parse_expr()
        return ast.For(var, it, self.parse_block(), line=kw.line, col=kw.col)

    def parse_return(self) -> ast.Return:
        kw = self.advance()
        value = None if self.check("NEWLINE") else self.parse_expr()
        self.expect("NEWLINE")
        return ast.Return(value, line=kw.line, col=kw.col)

    def parse_fn(self, in_struct: bool = False) -> ast.FnDef:
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
        body = self.parse_block()
        return ast.FnDef(name, params, ret, body, has_self, line=kw.line, col=kw.col)

    def _is_self_param(self) -> bool:
        nxt = self.peek()
        return self.check("IDENT", "self") and not (nxt.kind == "OP" and nxt.value == ":")

    def parse_dotted(self) -> str:
        name = self.expect("IDENT").value
        while self.check("OP", "."):
            self.advance()
            name += "." + self.expect("IDENT").value
        return name

    def parse_import(self) -> ast.Import:
        kw = self.advance()
        module = self.parse_dotted()
        alias = self.expect("IDENT").value if self.match("KW", "as") else None
        self.expect("NEWLINE")
        return ast.Import(module, alias, line=kw.line, col=kw.col)

    def parse_from_import(self) -> ast.FromImport:
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
        kw = self.advance()
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
        name = self.expect("IDENT")
        if name.value == "self":
            raise ZyloError("`self` can only be the first parameter of a method, inside a struct", name.line, name.col)
        if not self.check("OP", ":"):
            raise ZyloError(f"parameter {name.value!r} needs a type (`{name.value}: int`)", name.line, name.col)
        self.advance()
        return ast.Param(name.value, self.parse_type(), line=name.line, col=name.col)

    # ---- tipos --------------------------------------------------------------------

    def parse_type(self) -> ast.TypeRef:
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
        left = self.parse_unary()
        while True:
            if self.check("KW", "is") and IS_PREC >= min_prec:
                left = self.parse_is(left)
                continue
            if not (self._is_binop() and PRECEDENCE[self.cur.value] >= min_prec):
                return left
            op = self.advance()
            right = self.parse_expr(PRECEDENCE[op.value] + 1)
            left = ast.BinOp(op.value, left, right, line=op.line, col=op.col)

    def parse_is(self, left: ast.Expr) -> ast.IsCheck:
        kw = self.advance()
        tok = self.cur
        if tok.value not in IS_KINDS or tok.kind not in ("IDENT", "KW"):
            raise ZyloError("`is` must be followed by some, none, ok or err", tok.line, tok.col)
        self.advance()
        return ast.IsCheck(left, tok.value, line=kw.line, col=kw.col)

    def _is_binop(self) -> bool:
        return self.cur.kind in ("OP", "KW") and self.cur.value in PRECEDENCE

    def parse_unary(self) -> ast.Expr:
        tok = self.cur
        if tok.kind == "OP" and tok.value == "-":
            self.advance()
            return ast.UnaryOp("-", self.parse_unary(), line=tok.line, col=tok.col)
        if tok.kind == "KW" and tok.value == "not":
            self.advance()
            return ast.UnaryOp("not", self.parse_expr(NOT_PREC), line=tok.line, col=tok.col)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        expr = self.parse_primary()
        while True:
            if self.check("OP", "["):
                lb = self.advance()
                idx = self.parse_expr()
                self.expect("OP", "]")
                expr = ast.Index(expr, idx, line=lb.line, col=lb.col)
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
        tok = self.advance()
        pos = {"line": tok.line, "col": tok.col}
        if tok.kind == "INT":
            return ast.Int(int(tok.value), **pos)
        if tok.kind == "FLOAT":
            return ast.Float(float(tok.value), **pos)
        if tok.kind == "STR":
            return ast.Str(tok.value, **pos)
        if tok.kind == "KW" and tok.value in ("true", "false"):
            return ast.Bool(tok.value == "true", **pos)
        if tok.kind == "KW" and tok.value == "none":
            return ast.NoneLit(**pos)
        if tok.kind == "IDENT":
            if self.check("OP", "("):
                return self.parse_call(tok)
            return ast.Name(tok.value, **pos)
        if tok.kind == "OP" and tok.value == "(":
            e = self.parse_expr()
            self.expect("OP", ")")
            return e
        if tok.kind == "OP" and tok.value == "[":
            items: list[ast.Expr] = []
            if not self.check("OP", "]"):
                items.append(self.parse_expr())
                while self.match("OP", ","):
                    items.append(self.parse_expr())
            self.expect("OP", "]")
            return ast.ListLit(items, **pos)
        raise ZyloError(f"unexpected expression: {describe(tok)}", tok.line, tok.col)

    def parse_args(self) -> list[ast.Expr]:
        self.expect("OP", "(")
        args: list[ast.Expr] = []
        if not self.check("OP", ")"):
            args.append(self.parse_expr())
            while self.match("OP", ","):
                args.append(self.parse_expr())
        self.expect("OP", ")")
        return args

    def parse_call(self, name: Token) -> ast.Call:
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
