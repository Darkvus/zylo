from . import ast
from .errors import LumoError
from .lexer import Token, tokenize

# precedencia: mayor = se une antes. `not` es unario y se resuelve en parse_unary.
PRECEDENCE = {
    "or": 1,
    "and": 2,
    "==": 3, "!=": 3,
    "<": 4, ">": 4, "<=": 4, ">=": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
}
NOT_PREC = 3  # `not a == b` es `not (a == b)`
AUG_OPS = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%"}
TYPE_NAMES = {"int", "float", "bool", "str", "list"}
KIND_NAMES = {
    "NEWLINE": "fin de línea", "EOF": "fin del archivo", "INDENT": "indentación",
    "DEDENT": "fin de bloque", "IDENT": "un nombre", "INT": "un entero",
    "FLOAT": "un decimal", "STR": "una cadena",
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
            raise LumoError(f"se esperaba {want}, encontrado {describe(tok)}", tok.line, tok.col)
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
            raise LumoError("se esperaba un bloque indentado", self.cur.line, self.cur.col)
        self.advance()
        body = []
        while not self.check("DEDENT"):
            if self.check("EOF"):
                raise LumoError("bloque sin cerrar", self.cur.line, self.cur.col)
            body.append(self.parse_stmt())
        self.expect("DEDENT")
        return body

    # ---- sentencias ---------------------------------------------------------------

    def parse_stmt(self) -> ast.Stmt:
        tok = self.cur
        if tok.kind == "INDENT":
            raise LumoError("indentación inesperada", tok.line, tok.col)
        if tok.kind == "KW":
            match tok.value:
                case "fn":
                    return self.parse_fn()
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
                    raise LumoError(f"{tok.value!r} sin 'if' previo", tok.line, tok.col)
        return self.parse_simple()

    def parse_simple(self) -> ast.Stmt:
        """Expresión, asignación (`=`, `x: T =`) o asignación compuesta (`+=`)."""
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
        if not isinstance(expr, (ast.Name, ast.Index)):
            raise LumoError("no se puede asignar a esta expresión", start.line, start.col)

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

    def parse_fn(self) -> ast.FnDef:
        kw = self.advance()
        name = self.expect("IDENT").value
        self.expect("OP", "(")
        params: list[ast.Param] = []
        if not self.check("OP", ")"):
            params.append(self.parse_param())
            while self.match("OP", ","):
                params.append(self.parse_param())
        self.expect("OP", ")")
        ret = self.parse_type() if self.match("OP", "->") else None
        body = self.parse_block()
        return ast.FnDef(name, params, ret, body, line=kw.line, col=kw.col)

    def parse_param(self) -> ast.Param:
        name = self.expect("IDENT")
        if not self.check("OP", ":"):
            raise LumoError(f"el parámetro {name.value!r} necesita tipo (`{name.value}: int`)", name.line, name.col)
        self.advance()
        return ast.Param(name.value, self.parse_type(), line=name.line, col=name.col)

    # ---- tipos --------------------------------------------------------------------

    def parse_type(self) -> ast.TypeRef:
        tok = self.expect("IDENT")
        if tok.value not in TYPE_NAMES:
            raise LumoError(f"tipo desconocido {tok.value!r}", tok.line, tok.col)
        args: list[ast.TypeRef] = []
        if tok.value == "list":
            self.expect("OP", "[")
            args.append(self.parse_type())
            self.expect("OP", "]")
        return ast.TypeRef(tok.value, args, line=tok.line, col=tok.col)

    # ---- expresiones ----------------------------------------------------------------

    def parse_expr(self, min_prec: int = 1) -> ast.Expr:
        left = self.parse_unary()
        while self._is_binop() and PRECEDENCE[self.cur.value] >= min_prec:
            op = self.advance()
            right = self.parse_expr(PRECEDENCE[op.value] + 1)
            left = ast.BinOp(op.value, left, right, line=op.line, col=op.col)
        return left

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
        while self.check("OP", "["):
            lb = self.advance()
            idx = self.parse_expr()
            self.expect("OP", "]")
            expr = ast.Index(expr, idx, line=lb.line, col=lb.col)
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
        raise LumoError(f"expresión inesperada: {describe(tok)}", tok.line, tok.col)

    def parse_call(self, name: Token) -> ast.Call:
        self.expect("OP", "(")
        args: list[ast.Expr] = []
        if not self.check("OP", ")"):
            args.append(self.parse_expr())
            while self.match("OP", ","):
                args.append(self.parse_expr())
        self.expect("OP", ")")
        return ast.Call(name.value, args, line=name.line, col=name.col)


def parse(src: str) -> ast.Program:
    return Parser(tokenize(src)).parse_program()
