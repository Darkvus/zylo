"""Codegen: AST tipado (ya pasado por el checker) -> Rust.

Modelo de memoria: todo se comporta como valor. `int`/`float`/`bool` son `Copy`; `str`
(`String`) y `list` (`Vec<T>`) se clonan al leerlos como valor (`x.clone()`). Las lecturas
que no necesitan propiedad (len, print, índices, comparaciones, `for` sobre range) toman
referencia y no clonan.
"""
from . import ast
from .errors import LumoError
from .types import BOOL, FLOAT, INT, STR, UNIT, ListT, Type

PRELUDE = "#![allow(unused, unused_parens, while_true, unused_mut)]\n"

RUST_KEYWORDS = {
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum",
    "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod", "move",
    "mut", "pub", "ref", "return", "self", "Self", "static", "struct", "super", "trait", "true",
    "type", "unsafe", "use", "where", "while", "abstract", "become", "box", "do", "final",
    "macro", "override", "priv", "try", "typeof", "unsized", "virtual", "yield", "main",
}


def ident(name: str) -> str:
    return name + "_" if name in RUST_KEYWORDS else name


def rust_type(t: Type) -> str:
    if t == INT:
        return "i64"
    if t == FLOAT:
        return "f64"
    if t == BOOL:
        return "bool"
    if t == STR:
        return "String"
    if t == UNIT:
        return "()"
    if isinstance(t, ListT):
        return f"Vec<{rust_type(t.elem)}>"
    raise LumoError(f"tipo sin equivalente en Rust: {t}")


def is_copy(t: Type) -> bool:
    return t in (INT, FLOAT, BOOL)


def str_lit(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Gen:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.depth = 0

    def line(self, text: str) -> None:
        self.out.append("    " * self.depth + text)

    # ---- programa ----------------------------------------------------------------------

    def program(self, prog: ast.Program) -> str:
        self.out.append(PRELUDE)
        fns = [s for s in prog.body if isinstance(s, ast.FnDef)]
        rest = [s for s in prog.body if not isinstance(s, ast.FnDef)]
        for f in fns:
            self.fn(f)
            self.out.append("")
        self.line("fn main() {")
        self.block(rest)
        self.line("}")
        return "\n".join(self.out) + "\n"

    def fn(self, f: ast.FnDef) -> None:
        params = ", ".join(
            f"{'mut ' if p.info and p.info.mutable else ''}{ident(p.name)}: {rust_type(p.info.ty)}"
            for p in f.params
        )
        ret = "" if f.ty.ret == UNIT else f" -> {rust_type(f.ty.ret)}"
        self.line(f"fn {ident(f.name)}({params}){ret} {{")
        self.block(f.body)
        self.line("}")

    def block(self, body: list[ast.Stmt]) -> None:
        self.depth += 1
        for s in body:
            self.stmt(s)
        self.depth -= 1

    # ---- sentencias --------------------------------------------------------------------

    def stmt(self, s: ast.Stmt) -> None:
        match s:
            case ast.ExprStmt(e):
                self.line(f"{emit_ref(e)};")
            case ast.Assign():
                self.assign(s)
            case ast.If():
                self.if_(s, "if")
            case ast.While(cond, body):
                self.line(f"while {emit_ref(cond)} {{")
                self.block(body)
                self.line("}")
            case ast.For():
                self.for_(s)
            case ast.Return(value):
                self.line("return;" if value is None else f"return {emit(value)};")
            case ast.Break():
                self.line("break;")
            case ast.Continue():
                self.line("continue;")
            case _:
                raise LumoError(f"{type(s).__name__} no soportado en codegen", s.line, s.col)

    def assign(self, s: ast.Assign) -> None:
        if isinstance(s.target, ast.Index):
            self.line(f"{emit_ref(s.target)} = {emit(s.value)};")
        elif s.declares:
            mut = "mut " if s.var.mutable else ""
            self.line(f"let {mut}{ident(s.target.id)}: {rust_type(s.var.ty)} = {emit(s.value)};")
        else:
            self.line(f"{ident(s.target.id)} = {emit(s.value)};")

    def if_(self, s: ast.If, kw: str) -> None:
        self.line(f"{kw} {emit_ref(s.cond)} {{")
        self.block(s.then)
        self._else_tail(s)

    def _else_tail(self, s: ast.If) -> None:
        if len(s.else_) == 1 and isinstance(s.else_[0], ast.If):
            nested = s.else_[0]
            self.line(f"}} else if {emit_ref(nested.cond)} {{")
            self.block(nested.then)
            self._else_tail(nested)
            return
        if s.else_:
            self.line("} else {")
            self.block(s.else_)
        self.line("}")

    def for_(self, s: ast.For) -> None:
        mut = "mut " if s.info and s.info.mutable else ""
        it = s.iter
        if isinstance(it, ast.Call) and it.func == "range":
            lo, hi = ("0i64", emit_ref(it.args[0])) if len(it.args) == 1 else (emit_ref(it.args[0]), emit_ref(it.args[1]))
            src = f"({lo})..({hi})"
        else:
            src = emit(it)  # propiedad: clona la lista una vez, como Python permite mutarla dentro
        self.line(f"for {mut}{ident(s.var)} in {src} {{")
        self.block(s.body)
        self.line("}")


# ---- expresiones ----------------------------------------------------------------------------

def emit(e: ast.Expr) -> str:
    """Expresión como valor con propiedad (clona lo que no es Copy al leerlo de una variable)."""
    code = emit_ref(e)
    if isinstance(e, (ast.Name, ast.Index)) and not is_copy(e.ty):
        return f"{code}.clone()"
    return code


def emit_ref(e: ast.Expr) -> str:
    """Expresión sin clonar: sirve para leer sin consumir (o como lugar asignable)."""
    match e:
        case ast.Int(v):
            return f"{v}i64"
        case ast.Float(v):
            return f"{v!r}f64"
        case ast.Str(v):
            return f"String::from({str_lit(v)})"
        case ast.Bool(v):
            return "true" if v else "false"
        case ast.Name(i):
            return ident(i)
        case ast.Index(v, i):
            return f"{emit_ref(v)}[({emit_ref(i)}) as usize]"
        case ast.ListLit(items):
            if not items:
                return "Vec::new()"
            return f"vec![{', '.join(emit(i) for i in items)}]"
        case ast.UnaryOp("not", x):
            return f"(!{emit_ref(x)})"
        case ast.UnaryOp(op, x):
            return f"({op}{emit_ref(x)})"
        case ast.BinOp(op, l, r):
            return binop(e, op, l, r)
        case ast.Call():
            return call(e)
    raise LumoError(f"nodo no soportado: {type(e).__name__}", e.line, e.col)


def binop(e: ast.BinOp, op: str, l: ast.Expr, r: ast.Expr) -> str:
    a, b = emit_ref(l), emit_ref(r)
    if op == "+" and l.ty == STR:
        return f'format!("{{}}{{}}", {a}, {b})'
    rust_op = {"and": "&&", "or": "||"}.get(op, op)
    return f"({a} {rust_op} {b})"


def call(e: ast.Call) -> str:
    args = e.args
    match e.func:
        case "print":
            t = args[0].ty
            fmt = "{:?}" if t == FLOAT or isinstance(t, ListT) else "{}"
            return f'println!("{fmt}", {emit_ref(args[0])})'
        case "len":
            x = emit_ref(args[0])
            return f"({x}.chars().count() as i64)" if args[0].ty == STR else f"({x}.len() as i64)"
        case "append":
            return f"{emit_ref(args[0])}.push({emit(args[1])})"
        case "int":
            x, t = emit_ref(args[0]), args[0].ty
            if t == INT:
                return x
            if t == FLOAT:
                return f"({x} as i64)"
            return f'({x}.trim().parse::<i64>().expect("int(): cadena no numérica"))'
        case "float":
            x, t = emit_ref(args[0]), args[0].ty
            if t == FLOAT:
                return x
            if t == INT:
                return f"({x} as f64)"
            return f'({x}.trim().parse::<f64>().expect("float(): cadena no numérica"))'
        case "str":
            x, t = emit_ref(args[0]), args[0].ty
            if t == STR:
                return f"{x}.clone()"
            if t == FLOAT:
                return f'format!("{{:?}}", {x})'
            return f"{x}.to_string()"
        case "range":
            raise LumoError("range(...) solo puede usarse en un `for`", e.line, e.col)
        case name:
            return f"{ident(name)}({', '.join(emit(a) for a in args)})"


def generate(prog: ast.Program) -> str:
    return Gen().program(prog)
