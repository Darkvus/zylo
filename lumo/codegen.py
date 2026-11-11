from . import ast
from .errors import LumoError


def emit_expr(e: ast.Expr) -> str:
    match e:
        case ast.Int(v):
            return f"{v}i64"
        case ast.Float(v):
            return f"{v!r}f64"
        case ast.Str(v):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        case ast.Bool(v):
            return "true" if v else "false"
        case ast.Name(i):
            return i
        case ast.UnaryOp(op, x):
            return f"({op}{emit_expr(x)})"
        case ast.BinOp(op, l, r):
            return f"({emit_expr(l)} {op} {emit_expr(r)})"
        case ast.Call("print", args):
            if len(args) != 1:
                raise LumoError("print espera 1 argumento")
            return f'println!("{{}}", {emit_expr(args[0])})'
        case ast.Call(func, _):
            raise LumoError(f"función desconocida {func!r} (aún sin soporte de fn)")
    raise LumoError(f"nodo no soportado: {e!r}")


def generate(prog: ast.Program) -> str:
    lines = ["fn main() {"]
    for s in prog.body:
        if not isinstance(s, ast.ExprStmt):
            raise LumoError(f"{type(s).__name__} aún sin soporte en codegen (fase 4)", s.line, s.col)
        lines.append(f"    {emit_expr(s.expr)};")
    lines.append("}")
    return "\n".join(lines) + "\n"
