"""Chequeo de tipos: inferencia local, tabla de símbolos y anotación del AST.

Reglas:
- Sin coerción implícita (`int + float` es error; usa `float(x)`).
- `x = v` declara si `x` no existe en el ámbito visible; si existe, reasigna (mismo tipo)
  y la variable queda marcada como mutable.
- Ámbito de bloque: una variable declarada dentro de un `if`/`while`/`for` no existe fuera.
- Las funciones solo ven sus parámetros, sus locales y otras funciones (no globales).
- `int / int` es división entera.
- Los structs son valores (como las listas): copiar o pasar a una función clona. Los métodos
  reciben `self` por referencia; el checker infiere si lo modifican (`self_mut`), también
  cuando lo hacen llamando a otro método modificador (punto fijo en `check`).
"""
from dataclasses import dataclass, field

from . import ast
from .errors import ZyloError
from .types import BOOL, FLOAT, INT, PRIMS, STR, UNIT, FnT, ListT, StructT, Type, VarInfo

BUILTINS = {"print", "range", "len", "int", "float", "str", "append", "exit"}
ARITH = {"+", "-", "*", "/", "%"}
EQ = {"==", "!="}
ORDER = {"<", ">", "<=", ">="}


def err(msg: str, node: ast.Node) -> ZyloError:
    return ZyloError(msg, node.line, node.col)


class Scopes:
    def __init__(self) -> None:
        self.stack: list[dict[str, VarInfo]] = [{}]

    def push(self) -> None:
        self.stack.append({})

    def pop(self) -> None:
        self.stack.pop()

    def lookup(self, name: str) -> VarInfo | None:
        for scope in reversed(self.stack):
            if name in scope:
                return scope[name]
        return None

    def declare(self, name: str, ty: Type) -> VarInfo:
        info = VarInfo(name, ty)
        self.stack[-1][name] = info
        return info


@dataclass
class StructInfo:
    name: str
    defn: ast.StructDef
    fields: dict[str, Type] = field(default_factory=dict)
    methods: dict[str, FnT] = field(default_factory=dict)


class Checker:
    def __init__(self, mutating: set[tuple[str, str]] | None = None) -> None:
        self.fns: dict[str, FnT] = {}
        self.structs: dict[str, StructInfo] = {}
        self.mutating: set[tuple[str, str]] = set(mutating or ())  # (struct, método) que modifican self
        self.scopes = Scopes()
        self.ret: Type | None = None  # tipo de retorno de la función actual (None = top level)
        self.owner: str | None = None  # struct del método actual
        self.loops = 0

    # ---- entrada ---------------------------------------------------------------------

    def check(self, prog: ast.Program) -> None:
        defs = [s for s in prog.body if isinstance(s, ast.StructDef)]
        for d in defs:
            self.register_struct(d)
        for d in defs:
            self.resolve_struct(d)
        self.check_cycles(defs)
        for s in prog.body:  # firmas de funciones, para permitir uso antes de la definición
            if isinstance(s, ast.FnDef):
                self.declare_fn(s)
        for d in defs:
            self.declare_methods(d)
        for s in prog.body:
            if isinstance(s, ast.FnDef):
                self.check_fn(s)
            elif isinstance(s, ast.StructDef):
                for m in s.methods:
                    self.check_fn(m, s.name)
            else:
                self.stmt(s)

    # ---- tipos escritos ------------------------------------------------------------

    def resolve(self, t: ast.TypeRef) -> Type:
        if t.name == "list":
            return ListT(self.resolve(t.args[0]))
        if t.name in PRIMS:
            return PRIMS[t.name]
        if t.name in self.structs:
            return StructT(t.name)
        raise err(f"tipo desconocido {t.name!r}", t)

    # ---- structs ----------------------------------------------------------------------

    def register_struct(self, d: ast.StructDef) -> None:
        if d.name in self.structs:
            raise err(f"struct {d.name!r} ya definido", d)
        if d.name in BUILTINS or d.name in PRIMS or d.name in ("list", "unit"):
            raise err(f"{d.name!r} es un nombre reservado", d)
        self.structs[d.name] = StructInfo(d.name, d)

    def resolve_struct(self, d: ast.StructDef) -> None:
        info = self.structs[d.name]
        if not d.fields:
            raise err(f"el struct {d.name!r} necesita al menos un campo", d)
        for f in d.fields:
            if f.name in info.fields:
                raise err(f"campo {f.name!r} repetido en {d.name!r}", f)
            f.ty = info.fields[f.name] = self.resolve(f.type)

    def check_cycles(self, defs: list[ast.StructDef]) -> None:
        def reaches(start: str, cur: str, seen: set[str]) -> bool:
            for t in self.structs[cur].fields.values():
                if isinstance(t, StructT):
                    if t.name == start:
                        return True
                    if t.name not in seen:
                        seen.add(t.name)
                        if reaches(start, t.name, seen):
                            return True
            return False

        for d in defs:
            if reaches(d.name, d.name, set()):
                raise err(f"el struct {d.name!r} se contiene a sí mismo (directa o indirectamente); usa una lista", d)

    def declare_methods(self, d: ast.StructDef) -> None:
        info = self.structs[d.name]
        for m in d.methods:
            if not m.has_self:
                raise err(f"el método {m.name!r} debe recibir `self` como primer parámetro", m)
            if m.name in info.fields:
                raise err(f"{m.name!r} es a la vez un campo y un método de {d.name!r}", m)
            if m.name in info.methods:
                raise err(f"método {m.name!r} ya definido en {d.name!r}", m)
            m.ty = info.methods[m.name] = self.signature(m)

    # ---- funciones ------------------------------------------------------------------

    def signature(self, f: ast.FnDef) -> FnT:
        seen: set[str] = set()
        for p in f.params:
            if p.name == "self":
                raise err("`self` solo puede ser el primer parámetro de un método", p)
            if p.name in seen:
                raise err(f"parámetro {p.name!r} repetido", p)
            seen.add(p.name)
        ret = self.resolve(f.ret) if f.ret else UNIT
        return FnT(tuple(self.resolve(p.type) for p in f.params), ret)

    def declare_fn(self, f: ast.FnDef) -> None:
        if f.name in BUILTINS:
            raise err(f"{f.name!r} es una función integrada", f)
        if f.name in self.structs:
            raise err(f"{f.name!r} ya es un struct", f)
        if f.name in self.fns:
            raise err(f"función {f.name!r} ya definida", f)
        f.ty = self.fns[f.name] = self.signature(f)

    def check_fn(self, f: ast.FnDef, owner: str | None = None) -> None:
        sig = f.ty
        top = self.scopes
        self.scopes, self.ret, self.loops, self.owner = Scopes(), sig.ret, 0, owner
        self_info = self.scopes.declare("self", StructT(owner)) if owner else None
        for p, pt in zip(f.params, sig.params):
            p.info = self.scopes.declare(p.name, pt)
        self.block(f.body)
        if sig.ret != UNIT and not always_returns(f.body):
            raise err(f"la función {f.name!r} puede terminar sin `return`", f)
        if owner:
            if self_info.mutable:
                self.mutating.add((owner, f.name))
            f.self_mut = (owner, f.name) in self.mutating
        self.scopes, self.ret, self.owner = top, None, None

    # ---- sentencias ------------------------------------------------------------------

    def block(self, body: list[ast.Stmt]) -> None:
        self.scopes.push()
        for s in body:
            self.stmt(s)
        self.scopes.pop()

    def stmt(self, s: ast.Stmt) -> None:
        match s:
            case ast.ExprStmt(e):
                self.expr(e)
            case ast.Assign():
                self.assign(s)
            case ast.If(cond, then, else_):
                self.expect_bool(cond, "la condición del `if`")
                self.block(then)
                self.block(else_)
            case ast.While(cond, body):
                self.expect_bool(cond, "la condición del `while`")
                self.loops += 1
                self.block(body)
                self.loops -= 1
            case ast.For():
                self.for_loop(s)
            case ast.Return(value):
                self.return_(s, value)
            case ast.Break() | ast.Continue():
                if not self.loops:
                    kw = "break" if isinstance(s, ast.Break) else "continue"
                    raise err(f"`{kw}` fuera de un bucle", s)
            case ast.FnDef():
                raise err("las funciones solo pueden definirse a nivel superior", s)
            case ast.StructDef():
                raise err("los structs solo pueden definirse a nivel superior", s)

    def expect_bool(self, e: ast.Expr, what: str) -> None:
        t = self.expr(e)
        if t != BOOL:
            raise err(f"{what} debe ser bool, no {t}", e)

    def return_(self, s: ast.Return, value: ast.Expr | None) -> None:
        if self.ret is None:
            raise err("`return` fuera de una función", s)
        if value is None:
            if self.ret != UNIT:
                raise err(f"`return` sin valor en una función que devuelve {self.ret}", s)
            return
        if self.ret == UNIT:
            raise err("la función no declara tipo de retorno (`-> T`) pero devuelve un valor", s)
        t = self.expr(value, self.ret)
        if t != self.ret:
            raise err(f"se devuelve {t} pero la función declara {self.ret}", value)

    def for_loop(self, s: ast.For) -> None:
        if s.var == "self":
            raise err("`self` no puede usarse como variable de bucle", s)
        it = s.iter
        if isinstance(it, ast.Call) and it.func == "range":
            if not 1 <= len(it.args) <= 2:
                raise err("range espera 1 o 2 argumentos", it)
            for a in it.args:
                t = self.expr(a)
                if t != INT:
                    raise err(f"range espera int, no {t}", a)
            it.ty = ListT(INT)
            var_t: Type = INT
        else:
            t = self.expr(it)
            if not isinstance(t, ListT):
                raise err(f"solo se puede iterar sobre range(...) o list, no {t}", it)
            var_t = t.elem
        self.scopes.push()
        s.info = self.scopes.declare(s.var, var_t)
        self.loops += 1
        for b in s.body:
            self.stmt(b)
        self.loops -= 1
        self.scopes.pop()

    def assign(self, s: ast.Assign) -> None:
        target = s.target
        if isinstance(target, ast.Index):
            base_t = self.expr(target.value)
            if not isinstance(base_t, ListT):
                raise err(f"no se puede asignar por índice a {base_t}", target)
            self.expect_int(target.index)
            vt = self.expr(s.value, base_t.elem)
            if vt != base_t.elem:
                raise err(f"se asigna {vt} a un elemento de {base_t}", s.value)
            target.ty = base_t.elem
            self.mark_mutable(target.value)
            return
        if isinstance(target, ast.Attr):
            ft = self.expr(target)
            vt = self.expr(s.value, ft)
            if vt != ft:
                raise err(f"el campo {target.name!r} es {ft} pero se asigna {vt}", s.value)
            self.mark_mutable(target)
            return
        if target.id == "self":
            raise err("`self` no se puede declarar ni reasignar" if self.owner is None
                      else "no se puede reasignar `self`; modifica sus campos (`self.x = ...`)", s)
        existing = self.scopes.lookup(target.id)
        if s.annotation:
            if existing:
                raise err(f"{target.id!r} ya está declarada", s)
            declared = self.resolve(s.annotation)
            vt = self.expr(s.value, declared)
            if vt != declared:
                raise err(f"{target.id!r} es {declared} pero se asigna {vt}", s.value)
            s.declares, s.var = True, self.scopes.declare(target.id, declared)
        elif existing:
            vt = self.expr(s.value, existing.ty)
            if vt != existing.ty:
                raise err(f"{target.id!r} es {existing.ty} pero se asigna {vt}", s.value)
            existing.mutable = True
            s.var = existing
        else:
            vt = self.expr(s.value)
            if vt == UNIT:
                raise err("la expresión no produce ningún valor", s.value)
            s.declares, s.var = True, self.scopes.declare(target.id, vt)
        target.ty = s.var.ty

    def mark_mutable(self, e: ast.Expr) -> None:
        """Marca como mutable la variable raíz de un lugar (`xs[i]`, `p.x`, `p.items[0]`...)."""
        while isinstance(e, (ast.Index, ast.Attr)):
            e = e.value
        if isinstance(e, ast.Name) and (info := self.scopes.lookup(e.id)):
            info.mutable = True

    def expect_int(self, e: ast.Expr) -> None:
        t = self.expr(e)
        if t != INT:
            raise err(f"se esperaba int, no {t}", e)

    # ---- expresiones -----------------------------------------------------------------

    def expr(self, e: ast.Expr, expected: Type | None = None) -> Type:
        e.ty = self._expr(e, expected)
        return e.ty

    def _expr(self, e: ast.Expr, expected: Type | None) -> Type:
        match e:
            case ast.Int():
                return INT
            case ast.Float():
                return FLOAT
            case ast.Str():
                return STR
            case ast.Bool():
                return BOOL
            case ast.Name(id):
                info = self.scopes.lookup(id)
                if info is None:
                    if id in self.structs:
                        raise err(f"{id!r} es un struct; constrúyelo con paréntesis: {id}(...)", e)
                    if id in self.fns or id in BUILTINS:
                        raise err(f"{id!r} es una función; llámala con paréntesis", e)
                    raise err(f"variable {id!r} no definida", e)
                return info.ty
            case ast.UnaryOp(op, x):
                return self.unary(e, op, x)
            case ast.BinOp(op, l, r):
                return self.binop(e, op, l, r)
            case ast.Call():
                return self.call(e)
            case ast.MethodCall():
                return self.method_call(e)
            case ast.Attr(v, name):
                return self.attr(e, v, name)
            case ast.ListLit(items):
                return self.list_lit(e, items, expected)
            case ast.Index(v, i):
                vt = self.expr(v)
                if not isinstance(vt, ListT):
                    raise err(f"no se puede indexar {vt}", e)
                self.expect_int(i)
                return vt.elem
        raise err(f"expresión no soportada: {type(e).__name__}", e)

    def unary(self, e: ast.UnaryOp, op: str, x: ast.Expr) -> Type:
        t = self.expr(x)
        if op == "not":
            if t != BOOL:
                raise err(f"`not` espera bool, no {t}", e)
            return BOOL
        if t not in (INT, FLOAT):
            raise err(f"`-` espera int o float, no {t}", e)
        return t

    def binop(self, e: ast.BinOp, op: str, l: ast.Expr, r: ast.Expr) -> Type:
        lt, rt = self.expr(l), self.expr(r)
        if op in ("and", "or"):
            if lt != BOOL or rt != BOOL:
                raise err(f"`{op}` espera bool y bool, no {lt} y {rt}", e)
            return BOOL
        if lt != rt:
            hint = " (usa float(x) o int(x) para convertir)" if {lt, rt} == {INT, FLOAT} else ""
            raise err(f"`{op}` entre {lt} y {rt}: los tipos deben coincidir{hint}", e)
        if op in ARITH:
            if lt in (INT, FLOAT) or (op == "+" and lt == STR):
                return lt
            raise err(f"`{op}` no está definido para {lt}", e)
        if op in ORDER:
            if lt in (INT, FLOAT, STR):
                return BOOL
            raise err(f"`{op}` no está definido para {lt}", e)
        if op in EQ:
            return BOOL
        raise err(f"operador desconocido {op!r}", e)

    def list_lit(self, e: ast.ListLit, items: list[ast.Expr], expected: Type | None) -> Type:
        if not items:
            if isinstance(expected, ListT):
                return expected
            raise err("no se puede inferir el tipo de una lista vacía; anótala: `xs: list[int] = []`", e)
        elem = expected.elem if isinstance(expected, ListT) else None
        first = self.expr(items[0], elem)
        for it in items[1:]:
            t = self.expr(it, first)
            if t != first:
                raise err(f"la lista mezcla {first} y {t}", it)
        return ListT(first)

    # ---- structs: campos y métodos -----------------------------------------------------

    def struct_of(self, t: Type, node: ast.Node, what: str) -> StructInfo:
        if not isinstance(t, StructT):
            raise err(f"{t} no tiene {what}", node)
        return self.structs[t.name]

    def attr(self, e: ast.Attr, v: ast.Expr, name: str) -> Type:
        t = self.expr(v)
        info = self.struct_of(t, e, "campos")
        ft = info.fields.get(name)
        if ft is None:
            have = ", ".join(info.fields)
            hint = f" (tiene: {have})" if info.fields else ""
            if name in info.methods:
                raise err(f"{name!r} es un método de {t}; llámalo con paréntesis: .{name}(...)", e)
            raise err(f"{t} no tiene el campo {name!r}{hint}", e)
        return ft

    def method_call(self, e: ast.MethodCall) -> Type:
        rt = self.expr(e.value)
        info = self.struct_of(rt, e, "métodos")
        sig = info.methods.get(e.name)
        if sig is None:
            if e.name in info.fields:
                raise err(f"{e.name!r} es un campo de {rt}, no un método", e)
            have = ", ".join(info.methods)
            hint = f" (tiene: {have})" if info.methods else ""
            raise err(f"{rt} no tiene el método {e.name!r}{hint}", e)
        self.check_args(e, f"{rt}.{e.name}", e.args, sig)
        if (rt.name, e.name) in self.mutating:
            self.mark_mutable(e.value)
        return sig.ret

    def ctor(self, e: ast.Call) -> Type:
        info = self.structs[e.func]
        types = list(info.fields.values())
        self.check_args(e, e.func, e.args, FnT(tuple(types), StructT(e.func)))
        e.ctor_fields = list(info.fields)
        return StructT(e.func)

    # ---- llamadas ------------------------------------------------------------------

    def check_args(self, e: ast.Node, name: str, args: list[ast.Expr], sig: FnT) -> None:
        if len(args) != len(sig.params):
            raise err(f"{name} espera {len(sig.params)} argumento(s), recibió {len(args)}", e)
        for i, (a, pt) in enumerate(zip(args, sig.params), start=1):
            t = self.expr(a, pt)
            if t != pt:
                raise err(f"argumento {i} de {name}: se esperaba {pt}, se pasó {t}", a)

    def call(self, e: ast.Call) -> Type:
        if e.func in self.structs:
            return self.ctor(e)
        if e.func in BUILTINS:
            return self.builtin(e)
        sig = self.fns.get(e.func)
        if sig is None:
            raise err(f"función {e.func!r} no definida", e)
        self.check_args(e, e.func, e.args, sig)
        return sig.ret

    def builtin(self, e: ast.Call) -> Type:
        name, args = e.func, e.args

        def arity(n: int) -> None:
            if len(args) != n:
                raise err(f"{name} espera {n} argumento(s), recibió {len(args)}", e)

        match name:
            case "print":
                arity(1)
                self.expr(args[0])
                return UNIT
            case "len":
                arity(1)
                t = self.expr(args[0])
                if t != STR and not isinstance(t, ListT):
                    raise err(f"len espera str o list, no {t}", args[0])
                return INT
            case "int" | "float":
                arity(1)
                t = self.expr(args[0])
                if t not in (INT, FLOAT, STR):
                    raise err(f"{name}() no convierte {t}", args[0])
                return INT if name == "int" else FLOAT
            case "str":
                arity(1)
                t = self.expr(args[0])
                if t not in (INT, FLOAT, BOOL, STR):
                    raise err(f"str() no convierte {t}", args[0])
                return STR
            case "append":
                arity(2)
                lt = self.expr(args[0])
                if not isinstance(lt, ListT):
                    raise err(f"append espera una list, no {lt}", args[0])
                vt = self.expr(args[1], lt.elem)
                if vt != lt.elem:
                    raise err(f"no se puede añadir {vt} a {lt}", args[1])
                self.mark_mutable(args[0])
                return UNIT
            case "exit":
                arity(1)
                self.expect_int(args[0])
                return UNIT
            case "range":
                raise err("range(...) solo puede usarse en un `for`", e)
        raise err(f"función integrada desconocida {name!r}", e)


def always_returns(body: list[ast.Stmt]) -> bool:
    if not body:
        return False
    last = body[-1]
    if isinstance(last, ast.Return):
        return True
    if isinstance(last, ast.If):
        return always_returns(last.then) and always_returns(last.else_)
    return False


def check(prog: ast.Program) -> ast.Program:
    """Comprueba el programa. Repite hasta que el conjunto de métodos que modifican `self` se
    estabiliza (un método que llama a otro modificador también lo es); converge en pocas vueltas."""
    mutating: set[tuple[str, str]] = set()
    while True:
        c = Checker(mutating)
        c.check(prog)
        if c.mutating == mutating:
            return prog
        mutating = c.mutating
