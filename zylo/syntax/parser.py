"""Parser: tokens to a syntax tree.

A hand-written recursive-descent parser. Statements are dispatched on their leading keyword; expressions use
precedence climbing. It knows the grammar only: whether names and types exist is the checker's concern.
"""

import dataclasses

from zylo.domain import ast
from zylo.domain.builtins import TYPE_ARG_BUILTINS
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


BUILTIN_TYPE_NAMES = {"int", "float", "bool", "str", "list", "set", "dict", "tuple", "Cell", "Result", "fn"}


def typey(t: ast.TypeRef) -> bool:
    """Whether a type could not be mistaken for a variable: a built-in type name or a capitalised name.

    Args:
        t: A parsed type.

    Returns:
        True for `int`, `list[str]`, `Shape`, `mod.Shape`, `T?`..., False for a lowercase user name such as `point`.
    """
    if t.name in BUILTIN_TYPE_NAMES or t.name == "?":
        ok = True
    else:
        last = t.name.replace("\u00b7", ".").rsplit(".", 1)[-1]
        ok = last[:1].isupper()
    return ok and all(typey(a) for a in t.args) and (t.ret is None or typey(t.ret))


class Parser:
    """Recursive-descent parser over a token list.

    Use `parse()` rather than this class directly.
    """
    def __init__(self, tokens: list[Token], collect: bool = False):
        """Create a parser.

        Args:
            tokens: The token list produced by `tokenize`.
            collect: Recover from a syntax error, remember it in `errors` and go on with the next statement, instead
                of raising on the first one.
        """
        self.toks = tokens
        self.pos = 0
        self.depth = 0  # how many INDENTs are open: needed to find the end of a bad statement
        self.errors: list[ZyloError] = []
        self.collect = collect
        # names this file declares with type parameters: `name[...]` after them is always a list of type arguments
        self.generics = {tokens[i + 1].value for i in range(len(tokens) - 2)
                         if tokens[i].kind == "KW" and tokens[i].value in ("fn", "struct", "enum")
                         and tokens[i + 1].kind == "IDENT" and tokens[i + 2].kind == "OP" and tokens[i + 2].value == "["}

    # ---- helpers -------------------------------------------------------------

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
        if tok.kind == "INDENT":
            self.depth += 1
        elif tok.kind == "DEDENT":
            self.depth -= 1
        return tok

    def recover(self, depth: int) -> None:
        """Skip to the end of the statement that failed, so parsing can continue.

        A statement ends at the NEWLINE that returns to `depth`; if that line opens an indented block (the failed
        statement was a block header) the whole block is skipped too.

        Args:
            depth: The indentation depth of the statement that failed.
        """
        start = self.pos
        prev = self.toks[self.pos - 1] if self.pos else None
        if prev is not None and prev.kind == "NEWLINE" and self.depth == depth and not self.check("INDENT"):
            return  # the error was found after the end of the line had already been consumed
        while not self.check("EOF"):
            if self.depth == depth and self.check("DEDENT") and self.pos > start:
                return  # the enclosing block ends here; leave its DEDENT for the caller
            tok = self.advance()
            if tok.kind == "NEWLINE" and self.depth == depth and not self.check("INDENT"):
                return
            if tok.kind == "DEDENT" and self.depth == depth:
                return
        if self.pos == start and not self.check("EOF"):
            self.advance()

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

    # ---- program and blocks -------------------------------------------------------

    def parse_program(self) -> ast.Program:
        """Parse a whole module.

        Returns:
            The program: every top-level statement in order.
        """
        body = []
        while not self.check("EOF"):
            self.parse_one(body, 0)
        return ast.Program(body, line=1, col=1)

    def parse_one(self, body: list[ast.Stmt], depth: int) -> None:
        """Parse one statement into `body`; in collecting mode a syntax error is recorded and skipped.

        Args:
            body: The statement list being built.
            depth: The indentation depth the statement is at.
        """
        try:
            body.append(self.parse_stmt())
        except ZyloError as e:
            if not self.collect:
                raise
            self.errors.append(e)
            self.recover(depth)

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
        depth = self.depth
        while not self.check("DEDENT"):
            if self.check("EOF"):
                raise ZyloError("unclosed block", self.cur.line, self.cur.col)
            self.parse_one(body, depth)
        self.expect("DEDENT")
        return body

    # ---- statements ---------------------------------------------------------------

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
                case "fn" if self.peek().kind != "IDENT":
                    return self.parse_simple()  # an anonymous function used as an expression
                case "fn" if self.depth > 0:
                    return self.parse_local_fn()
                case "fn":
                    return self.parse_fn()
                case "extern":
                    self.advance()
                    if not self.check("KW", "fn"):
                        raise ZyloError("expected 'fn' after 'extern'", self.cur.line, self.cur.col)
                    return self.parse_fn(extern=True)
                case "struct":
                    return self.parse_struct()
                case "enum":
                    return self.parse_enum()
                case "match":
                    return self.parse_match()
                case "import":
                    return self.parse_import()
                case "from":
                    return self.parse_from_import()
                case "pub":
                    return self.parse_pub()
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
            in_struct: True inside a struct or enum body, where a leading `self` parameter is allowed.
            extern: True when the `extern` keyword was already consumed: the body is a Rust string after `=`.

        Returns:
            The function node (an `ExternFn` when `extern` is True).
        """
        kw = self.advance()
        name_tok = self.expect("IDENT")
        name = name_tok.value
        tparams = self.parse_tparams()
        if tparams and in_struct:
            raise ZyloError("a method cannot have its own type parameters; declare them on the struct or enum",
                            tparams[0].line, tparams[0].col)
        if tparams and extern:
            raise ZyloError("an `extern fn` cannot be generic", tparams[0].line, tparams[0].col)
        self.expect("OP", "(")
        params: list[ast.Param] = []
        has_self = False
        if not self.check("OP", ")"):
            if in_struct and self._is_self_param():
                self.advance()
                has_self = True
                if self.match("OP", ",") and not self.check("OP", ")"):
                    params.append(self.parse_param())
            else:
                params.append(self.parse_param())
            while self.match("OP", ",") and not self.check("OP", ")"):  # a trailing comma is allowed
                params.append(self.parse_param())
        self.expect("OP", ")")
        ret = self.parse_type() if self.match("OP", "->") else None
        if extern:
            self.expect("OP", "=")
            template = self.expect("STR").value
            self.expect("NEWLINE")
            return ast.ExternFn(name, params, ret, [], template=template, line=kw.line, col=kw.col)
        body = self.parse_block()
        return ast.FnDef(name, params, ret, body, has_self, tparams=tparams, line=kw.line, col=kw.col)

    def parse_tparams(self) -> list[ast.TypeParam]:
        """Parse the optional type parameters after a definition's name: `[T]`, `[K, V]`, `[T: Ord + Show]`.

        Raises:
            ZyloError: On an empty list, a repeated name or a malformed bound.
        """
        if not self.check("OP", "["):
            return []
        open_tok = self.advance()
        found: list[ast.TypeParam] = []
        while True:
            name = self.expect("IDENT")
            if any(p.name == name.value for p in found):
                raise ZyloError(f"duplicate type parameter {name.value!r}", name.line, name.col)
            bounds: list[str] = []
            if self.match("OP", ":"):
                bounds.append(self.expect("IDENT").value)
                while self.match("OP", "+"):
                    bounds.append(self.expect("IDENT").value)
            found.append(ast.TypeParam(name.value, bounds, line=name.line, col=name.col))
            if not self.match("OP", ","):
                break
        if not found:
            raise ZyloError("a type parameter list cannot be empty", open_tok.line, open_tok.col)
        self.expect("OP", "]")
        return found

    def parse_local_fn(self) -> ast.Assign:
        """Parse a `fn name(...)` inside a block: a variable holding a function that may use the variables around it.

        Returns:
            An assignment of a block-bodied `Lambda` to `name`.
        """
        f = self.parse_fn()
        for p in f.params:
            if p.default is not None:
                raise ZyloError(f"a local function cannot have default values ({p.name!r}); write them on a top-level `fn`",
                                p.line, p.col)
        lam = ast.Lambda(f.params, f.ret, None, f.body, f.name, line=f.line, col=f.col)
        return ast.Assign(ast.Name(f.name, line=f.line, col=f.col), None, lam, line=f.line, col=f.col)

    def parse_lambda(self, kw: Token) -> ast.Lambda:
        """Parse the rest of `fn(params) [-> T] => expression`, after the `fn` keyword.

        Args:
            kw: The `fn` token.

        Returns:
            The lambda; a parameter written without a type has `type` None.

        Raises:
            ZyloError: On a malformed parameter list, or a missing `=>` (a block body needs a name: `fn name(...):`).
        """
        self.expect("OP", "(")
        params: list[ast.Param] = []
        while not self.check("OP", ")"):
            name = self.expect("IDENT")
            if name.value == "self":
                raise ZyloError("`self` can only be the first parameter of a method, inside a struct", name.line, name.col)
            typ = self.parse_type() if self.match("OP", ":") else None
            params.append(ast.Param(name.value, typ, line=name.line, col=name.col))
            if not self.match("OP", ","):
                break
        self.expect("OP", ")")
        ret = self.parse_type() if self.match("OP", "->") else None
        if not self.check("OP", "=>"):
            hint = " (for several statements, define a named `fn` inside a function)" if self.check("OP", ":") else ""
            raise ZyloError(f"expected '=>' before the body of an anonymous function, found {describe(self.cur)}{hint}",
                            self.cur.line, self.cur.col)
        self.advance()
        return ast.Lambda(params, ret, self.parse_expr(), line=kw.line, col=kw.col)

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

    def parse_pub(self) -> ast.FromImport:
        """Parse `pub from a.b import x [as y], ...`: an import that is also part of this module's public surface."""
        kw = self.advance()
        if not self.check("KW", "from"):
            raise ZyloError("expected 'from' after 'pub' (only imports can be re-exported: `pub from a import x`)",
                            self.cur.line, self.cur.col)
        node = self.parse_from_import()
        node.pub = True
        node.line, node.col = kw.line, kw.col
        return node

    def parse_from_import(self) -> ast.FromImport:
        """Parse `from a.b import x [as y], ...`; the names may be wrapped in parentheses (one per line)."""
        kw = self.advance()
        module = self.parse_dotted()
        self.expect("KW", "import")
        paren = self.match("OP", "(") is not None
        names: list[tuple[str, str | None]] = []
        while True:
            name = self.expect("IDENT").value
            alias = self.expect("IDENT").value if self.match("KW", "as") else None
            names.append((name, alias))
            if not self.match("OP", ",") or (paren and self.check("OP", ")")):
                break
        if paren:
            self.expect("OP", ")")
        self.expect("NEWLINE")
        return ast.FromImport(module, names, line=kw.line, col=kw.col)

    def parse_struct(self) -> ast.StructDef:
        """Parse a struct: its fields (`name: type`) and methods (`fn`).

        Raises:
            ZyloError: If the body contains anything else.
        """
        self.advance()
        name = self.expect("IDENT")
        tparams = self.parse_tparams()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        if not self.check("INDENT"):
            raise ZyloError("expected an indented block", self.cur.line, self.cur.col)
        self.advance()
        fields: list[ast.Field] = []
        methods: list[ast.FnDef] = []
        doc: tuple[str, int] | None = None
        while not self.check("DEDENT"):
            tok = self.cur
            if tok.kind == "EOF":
                raise ZyloError("unclosed block", tok.line, tok.col)
            if tok.kind == "STR":
                target = fields[-1] if fields and not methods else None
                if target is not None and target.doc is None:
                    target.doc, target.doc_line = self.parse_doc()
                elif not fields and not methods and doc is None:
                    doc = self.parse_doc()
                else:
                    raise ZyloError("a description goes first in the struct, or right after a field", tok.line, tok.col)
            elif self.check("KW", "fn"):
                methods.append(self.parse_fn(in_struct=True))
            elif tok.kind == "IDENT" and self.peek().kind == "OP" and self.peek().value == ":":
                self.advance()
                self.advance()
                typ = self.parse_type()
                default = self.parse_expr() if self.match("OP", "=") else None
                self.expect("NEWLINE")
                fields.append(ast.Field(tok.value, typ, default, line=tok.line, col=tok.col))
            else:
                raise ZyloError("a struct body can only contain fields (`name: type`) and methods (`fn`)", tok.line, tok.col)
        self.expect("DEDENT")
        d = ast.StructDef(name.value, fields, methods, tparams=tparams, line=name.line, col=name.col)
        if doc:
            d.doc, d.doc_line = doc
        return d

    def parse_doc(self) -> tuple[str, int]:
        """Parse a description: a string literal alone on its line, in a struct or enum body.

        Returns:
            The text and its line.
        """
        tok = self.advance()
        self.expect("NEWLINE")
        return tok.value, tok.line

    def parse_enum(self) -> ast.EnumDef:
        """Parse an enum: one variant per line, `Name` or `Name(type, ...)`, and methods (`fn`).

        Raises:
            ZyloError: If the body is missing, empty, or contains anything but variants.
        """
        self.advance()
        name = self.expect("IDENT")
        tparams = self.parse_tparams()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        if not self.check("INDENT"):
            raise ZyloError("expected an indented block", self.cur.line, self.cur.col)
        self.advance()
        variants: list[ast.Variant] = []
        methods: list[ast.FnDef] = []
        doc: tuple[str, int] | None = None
        while not self.check("DEDENT"):
            tok = self.cur
            if tok.kind == "EOF":
                raise ZyloError("unclosed block", tok.line, tok.col)
            if tok.kind == "STR":
                target = variants[-1] if variants and not methods else None
                if target is not None and target.doc is None:
                    target.doc, target.doc_line = self.parse_doc()
                elif not variants and not methods and doc is None:
                    doc = self.parse_doc()
                else:
                    raise ZyloError("a description goes first in the enum, or right after a variant", tok.line, tok.col)
                continue
            if self.check("KW", "fn"):
                methods.append(self.parse_fn(in_struct=True))
                continue
            if tok.kind != "IDENT":
                raise ZyloError("an enum body can only contain variants (`Name` or `Name(type, ...)`) and methods (`fn`)", tok.line, tok.col)
            self.advance()
            fields: list[ast.TypeRef] = []
            if self.match("OP", "("):
                fields.append(self.parse_type())
                while self.match("OP", ","):
                    fields.append(self.parse_type())
                self.expect("OP", ")")
            self.expect("NEWLINE")
            variants.append(ast.Variant(tok.value, fields, line=tok.line, col=tok.col))
        self.expect("DEDENT")
        d = ast.EnumDef(name.value, variants, methods=methods, tparams=tparams, line=name.line, col=name.col)
        if doc:
            d.doc, d.doc_line = doc
        return d

    def parse_match(self) -> ast.Match:
        """Parse `match value:` and its arms.

        Raises:
            ZyloError: If the body is missing, empty, or an arm is malformed.
        """
        kw = self.advance()
        value = self.parse_expr()
        self.expect("OP", ":")
        self.expect("NEWLINE")
        if not self.check("INDENT"):
            raise ZyloError("expected an indented block", self.cur.line, self.cur.col)
        self.advance()
        arms: list[ast.MatchArm] = []
        while not self.check("DEDENT"):
            if self.check("EOF"):
                raise ZyloError("unclosed block", self.cur.line, self.cur.col)
            arms.append(self.parse_arm())
        self.expect("DEDENT")
        if not arms:
            raise ZyloError("a `match` needs at least one arm", kw.line, kw.col)
        return ast.Match(value, arms, line=kw.line, col=kw.col)

    def parse_arm(self) -> ast.MatchArm:
        """Parse one arm and its block.

        An arm is a pattern, optionally followed by more alternatives (`1 | 2`) and a guard (`if cond`), then a block.
        The patterns may be wrapped in parentheses, which lets a long or-pattern span several lines.

        Raises:
            ZyloError: If the pattern is malformed.
        """
        paren = self.match("OP", "(") is not None  # `(A | B | C)`: the form `zylo fmt` uses for long or-patterns
        arm = self.parse_pattern()
        while self.match("OP", "|"):
            arm.alts.append(self.parse_pattern())
        if paren:
            self.expect("OP", ")")
        if self.match("KW", "if"):
            arm.guard = self.parse_expr()
        arm.body = self.parse_block()
        return arm

    def parse_pattern(self) -> ast.MatchArm:
        """Parse one pattern, as an arm with an empty body.

        The pattern is `_`, `Enum.Variant`, `Enum.Variant(a, b)`, a literal (`1`, `-1`, `"a"`, `true`), `none`, or
        `some(x)` / `ok(v)` / `err(e)`.

        Raises:
            ZyloError: If the pattern is malformed.
        """
        first = self.cur
        pos = {"line": first.line, "col": first.col}
        if first.kind == "KW" and first.value == "none":
            self.advance()
            return ast.MatchArm(None, "none", [], [], **pos)
        if first.kind in ("INT", "FLOAT", "STR") or (first.kind == "OP" and first.value == "-") \
                or (first.kind == "KW" and first.value in ("true", "false")):
            lit = self.parse_unary()
            if not isinstance(lit, (ast.Int, ast.Float, ast.Str, ast.Bool, ast.UnaryOp)):
                raise ZyloError("a `match` pattern must be a literal", first.line, first.col)
            return ast.MatchArm(None, "literal", [], [], literal=lit, **pos)
        self.expect("IDENT")
        if first.value == "_":
            return ast.MatchArm(None, "_", [], [], **pos)
        parts = [first.value]
        targs: list[ast.TypeRef] = []
        while self.match("OP", "."):
            parts.append(self.expect("IDENT").value)
            if self.check("OP", "[") and not targs:
                targs = self.parse_type_args()  # `Opt[int].Some(x)`: the enum with its type arguments
        if not targs and self.check("OP", "[") and len(parts) == 1:
            targs = self.parse_type_args()
            self.expect("OP", ".")
            parts.append(self.expect("IDENT").value)
        if len(parts) < 2 and first.value not in ("some", "ok", "err"):
            raise ZyloError(f"write the enum before the variant: `Enum.{first.value}`", first.line, first.col)
        binds: list[str] = []
        if self.match("OP", "("):
            binds.append(self.expect("IDENT").value)
            while self.match("OP", ","):
                binds.append(self.expect("IDENT").value)
            self.expect("OP", ")")
        if len(parts) == 1:  # some(x) / ok(v) / err(e)
            if len(binds) != 1:
                raise ZyloError(f"`{first.value}(...)` binds exactly one name: write `{first.value}(x)`", first.line, first.col)
            return ast.MatchArm(None, first.value, binds, [], **pos)
        typ = ast.TypeRef(".".join(parts[:-1]), targs, line=first.line, col=first.col)
        return ast.MatchArm(typ, parts[-1], binds, [], **pos)

    def parse_param(self) -> ast.Param:
        """Parse one `name: type` or `name: type = default` parameter.

        Raises:
            ZyloError: If the type is missing, or the name is `self` outside a method.
        """
        name = self.expect("IDENT")
        if name.value == "self":
            raise ZyloError("`self` can only be the first parameter of a method, inside a struct or enum", name.line, name.col)
        if not self.check("OP", ":"):
            raise ZyloError(f"parameter {name.value!r} needs a type (`{name.value}: int`)", name.line, name.col)
        self.advance()
        typ = self.parse_type()
        default = self.parse_expr() if self.match("OP", "=") else None
        return ast.Param(name.value, typ, default=default, line=name.line, col=name.col)

    # ---- types --------------------------------------------------------------------

    def parse_type(self) -> ast.TypeRef:
        """Parse a type: `int`, `list[T]`, `tuple[A, B]`, `dict[K, V]`, `set[T]`, `Result[T, E]`, `mod.Struct`, with `?` suffixes.

        Raises:
            ZyloError: On a nested optional (`T??`) or a malformed type.
        """
        if self.check("OP", "("):  # a parenthesised type: `(fn(int) -> int)?`
            self.advance()
            t = self.parse_type()
            self.expect("OP", ")")
            return self._optional_suffix(t)
        if self.check("KW", "fn"):
            return self._optional_suffix(self.parse_fn_type())
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
        elif name in ("set", "Cell"):
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
        elif name not in BUILTIN_TYPE_NAMES and self.check("OP", "["):  # a generic struct or enum: `Box[int]`
            args = self.parse_type_args()
        return self._optional_suffix(ast.TypeRef(name, args, line=tok.line, col=tok.col))

    def parse_type_args(self) -> list[ast.TypeRef]:
        """Parse `[A, B, ...]`: the type arguments of a generic struct, enum or function."""
        open_tok = self.expect("OP", "[")
        args = [self.parse_type()]
        while self.match("OP", ","):
            args.append(self.parse_type())
        self.expect("OP", "]")
        if not args:
            raise ZyloError("a type argument list cannot be empty", open_tok.line, open_tok.col)
        return args

    def try_type_args(self, name: str, follow: tuple[str, ...]) -> list[ast.TypeRef] | None:
        """Read explicit type arguments after a name in an expression, if what follows really is one.

        `first[int](xs)` and `Opt[int].Some(1)` look like an index; they are type arguments when the brackets hold
        types, are followed by one of `follow`, and either name a generic declared in this file or hold only types that
        cannot be a variable (built-in type names, or capitalised names).

        Args:
            name: The name before the `[`.
            follow: Operators allowed right after the closing bracket.

        Returns:
            The type arguments (the parser is then after the `]`), or None, with the parser where it was.
        """
        start = self.pos
        try:
            args = self.parse_type_args()
        except ZyloError:
            self.pos = start
            return None
        nxt = self.cur
        if nxt.kind != "OP" or nxt.value not in follow or not (name in self.generics or all(typey(a) for a in args)):
            self.pos = start
            return None
        return args

    def parse_fn_type(self) -> ast.TypeRef:
        """Parse a function type `fn(A, B) -> R`; without `-> R` it returns nothing."""
        kw = self.advance()
        self.expect("OP", "(")
        params: list[ast.TypeRef] = []
        while not self.check("OP", ")"):
            params.append(self.parse_type())
            if not self.match("OP", ","):
                break
        self.expect("OP", ")")
        ret = self.parse_type() if self.match("OP", "->") else None
        return ast.TypeRef("fn", params, ret, line=kw.line, col=kw.col)

    def _optional_suffix(self, t: ast.TypeRef) -> ast.TypeRef:
        """Apply any `?` after a type: `T?` is Option, internally `TypeRef("?", [T])`."""
        while self.check("OP", "?"):
            q = self.advance()
            if t.name == "?":
                raise ZyloError("nested Option types (`T??`) are not allowed", q.line, q.col)
            t = ast.TypeRef("?", [t], line=t.line, col=t.col)
        return t

    # ---- expressions ----------------------------------------------------------------

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
            elif self.check("OP", "("):  # calling the value of an expression: `table[k](x)`, `make()(x)`
                lp = self.cur
                args, named = self.parse_args()
                if named:
                    raise ZyloError("function values take positional arguments only", named[0].line, named[0].col)
                expr = ast.CallValue(expr, args, line=lp.line, col=lp.col)
            elif self.check("OP", "."):
                self.advance()
                name = self.expect("IDENT")
                targs = self.try_type_args(name.value, ("(", ".")) if self.check("OP", "[") else None
                if self.check("OP", "("):
                    args, named = self.parse_args()
                    expr = ast.MethodCall(expr, name.value, args, named=named, targs=targs or [], line=name.line, col=name.col)
                else:
                    expr = ast.Attr(expr, name.value, targs=targs or [], line=name.line, col=name.col)
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
        if tok.kind == "KW" and tok.value == "fn":
            return self.parse_lambda(tok)
        if tok.kind == "IDENT":
            targs = self.try_type_args(tok.value, ("(", ".")) if self.check("OP", "[") else None
            if self.check("OP", "("):
                call = self.parse_call(tok)
                call.targs = targs or []
                return call
            return ast.Name(tok.value, targs=targs or [], **pos)
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
                    if self.check("OP", "]"):
                        break
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

    def parse_args(self) -> tuple[list[ast.Expr], list[ast.NamedArg]]:
        """Parse a parenthesised, comma-separated argument list: positional arguments, then `name=value` ones.

        Returns:
            The positional arguments and the named ones.

        Raises:
            ZyloError: If a positional argument follows a named one.
        """
        self.expect("OP", "(")
        args: list[ast.Expr] = []
        named: list[ast.NamedArg] = []
        while not self.check("OP", ")"):
            tok = self.cur
            if tok.kind == "IDENT" and self.peek().kind == "OP" and self.peek().value == "=":
                self.advance()
                self.advance()
                named.append(ast.NamedArg(tok.value, self.parse_expr(), line=tok.line, col=tok.col))
            else:
                if named:
                    raise ZyloError("positional arguments must come before named arguments", tok.line, tok.col)
                args.append(self.parse_expr())
            if not self.match("OP", ","):
                break
        self.expect("OP", ")")
        return args, named

    def parse_call(self, name: Token) -> ast.Call:
        """Parse the arguments of a call to the function named by `name`."""
        if name.value in TYPE_ARG_BUILTINS:  # `from_json(list[int], j)`: the first argument is a type
            return self.parse_type_call(name)
        args, named = self.parse_args()
        return ast.Call(name.value, args, named=named, line=name.line, col=name.col)

    def parse_type_call(self, name: Token) -> ast.Call:
        """Parse the arguments of `from_json`, `json_schema` or `json_schema_defs`: a type, then ordinary arguments."""
        self.expect("OP", "(")
        args: list[ast.Expr] = [self.parse_type()]  # type: ignore[list-item]
        while self.match("OP", ","):
            if self.check("OP", ")"):
                break
            args.append(self.parse_expr())
        self.expect("OP", ")")
        return ast.Call(name.value, args, line=name.line, col=name.col)


def parse_collect(src: str, file: str | None = None) -> tuple[ast.Program, list[ZyloError]]:
    """Parse a module and report every syntax error, not only the first.

    After an error the parser skips the bad statement (and its block) and carries on, so one run reports the mistakes
    of several statements. The returned program holds only the statements that parsed.

    Args:
        src: The module's source text.
        file: Its file name, stamped on nodes and errors.

    Returns:
        The (possibly partial) program and the errors found, in source order. A lexical error (an unterminated string,
        a tab) stops the run, so it is the only error reported.
    """
    try:
        tokens = tokenize(src)
    except ZyloError as e:
        e.file = e.file or file
        return ast.Program([], line=1, col=1), [e]
    parser = Parser(tokens, collect=True)
    try:
        prog = parser.parse_program()
    except ZyloError as e:  # an error the recovery could not absorb, such as a block that never closes
        parser.errors.append(e)
        prog = ast.Program([], line=1, col=1)
    for e in parser.errors:
        e.file = e.file or file
    if file is not None:
        ast.stamp(prog, file)
    return prog, parser.errors


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
