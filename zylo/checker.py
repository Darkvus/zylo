"""Type checker: local inference, symbol table and AST annotation.

Reglas:
- No implicit coercion (`int + float` is an error; use `float(x)`).
- `x = v` declares `x` if it is not visible in scope; otherwise it reassigns (same type)
  y la variable queda marcada como mutable.
- Ámbito de bloque: una variable declarada dentro de un `if`/`while`/`for` no existe fuera.
- Functions only see their parameters, their locals and other functions (no globals).
- `int / int` is integer division.
- Structs are values (like lists): copying or passing them to a function clones. Methods
  receive `self` by reference; the checker infers whether they modify it (`self_mut`), also
  when they do so by calling another modifying method (fixed point in `check`).
"""
from dataclasses import dataclass, field

from . import ast
from .errors import ZyloError
from .types import BOOL, FLOAT, INT, PRIMS, STR, UNIT, FnT, ListT, OptionT, ResultT, StructT, Type, VarInfo

BUILTINS = {"print", "range", "len", "int", "float", "str", "append", "exit",
            "some", "ok", "err", "parse_int", "parse_float"}
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
        self.mutating: set[tuple[str, str]] = set(mutating or ())  # (struct, method) pairs that modify self
        self.scopes = Scopes()
        self.ret: Type | None = None  # return type of the current function (None = top level)
        self.owner: str | None = None  # struct of the current method
        self.loops = 0

    # ---- entrada ---------------------------------------------------------------------

    def check(self, prog: ast.Program) -> None:
        defs = [s for s in prog.body if isinstance(s, ast.StructDef)]
        for d in defs:
            self.register_struct(d)
        for d in defs:
            self.resolve_struct(d)
        self.check_cycles(defs)
        for s in prog.body:  # function signatures, so they can be used before their definition
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
        if t.name == "?":
            return OptionT(self.resolve(t.args[0]))
        if t.name == "Result":
            return ResultT(self.resolve(t.args[0]), self.resolve(t.args[1]))
        if t.name in PRIMS:
            return PRIMS[t.name]
        if t.name in self.structs:
            return StructT(t.name)
        raise err(f"unknown type {t.name!r}", t)

    # ---- structs ----------------------------------------------------------------------

    def register_struct(self, d: ast.StructDef) -> None:
        if d.name in self.structs:
            raise err(f"struct {d.name!r} is already defined", d)
        if d.name in BUILTINS or d.name in PRIMS or d.name in ("list", "unit", "Result"):
            raise err(f"{d.name!r} is a reserved name", d)
        self.structs[d.name] = StructInfo(d.name, d)

    def resolve_struct(self, d: ast.StructDef) -> None:
        info = self.structs[d.name]
        if not d.fields:
            raise err(f"struct {d.name!r} needs at least one field", d)
        for f in d.fields:
            if f.name in info.fields:
                raise err(f"duplicate field {f.name!r} in {d.name!r}", f)
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
                raise err(f"struct {d.name!r} contains itself (directly or indirectly); use a list", d)

    def declare_methods(self, d: ast.StructDef) -> None:
        info = self.structs[d.name]
        for m in d.methods:
            if not m.has_self:
                raise err(f"method {m.name!r} must take `self` as its first parameter", m)
            if m.name in info.fields:
                raise err(f"{m.name!r} is both a field and a method of {d.name!r}", m)
            if m.name in info.methods:
                raise err(f"method {m.name!r} is already defined in {d.name!r}", m)
            m.ty = info.methods[m.name] = self.signature(m)

    # ---- funciones ------------------------------------------------------------------

    def signature(self, f: ast.FnDef) -> FnT:
        seen: set[str] = set()
        for p in f.params:
            if p.name == "self":
                raise err("`self` can only be the first parameter of a method", p)
            if p.name in seen:
                raise err(f"duplicate parameter {p.name!r}", p)
            seen.add(p.name)
        ret = self.resolve(f.ret) if f.ret else UNIT
        return FnT(tuple(self.resolve(p.type) for p in f.params), ret)

    def declare_fn(self, f: ast.FnDef) -> None:
        if f.name in BUILTINS:
            raise err(f"{f.name!r} is a built-in function", f)
        if f.name in self.structs:
            raise err(f"{f.name!r} is already a struct", f)
        if f.name in self.fns:
            raise err(f"function {f.name!r} is already defined", f)
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
            raise err(f"function {f.name!r} can finish without a `return`", f)
        if owner:
            if self_info.mutable:
                self.mutating.add((owner, f.name))
            f.self_mut = (owner, f.name) in self.mutating
        self.scopes, self.ret, self.owner = top, None, None

    # ---- sentencias ------------------------------------------------------------------

    def block(self, body: list[ast.Stmt], bind: tuple[str | None, Type | None] | None = None) -> VarInfo | None:
        """Check a block in its own scope. `bind` = (name, type) rebinds a variable with a narrowed
        type for the whole block (see `is` narrowing); returns that new VarInfo."""
        self.scopes.push()
        info = None
        if bind and bind[1] is not None:
            info = self.scopes.declare(bind[0], bind[1])
            info.narrowed = True
        for s in body:
            self.stmt(s)
        self.scopes.pop()
        return info

    def narrowing(self, cond: ast.Expr) -> tuple[str, Type | None, Type | None] | None:
        """For `x is K` on a plain variable: (name, type of x inside the then-branch, type inside
        the else-branch); None means "no information"."""
        if not (isinstance(cond, ast.IsCheck) and isinstance(cond.value, ast.Name)):
            return None
        t, k = cond.value.ty, cond.kind
        if isinstance(t, OptionT):
            then_t, else_t = (t.inner, None) if k == "some" else (None, t.inner)
        else:
            then_t, else_t = (t.ok, t.err) if k == "ok" else (t.err, t.ok)
        return cond.value.id, then_t, else_t

    def if_stmt(self, s: ast.If) -> None:
        self.expect_bool(s.cond, "the `if` condition")
        nar = self.narrowing(s.cond)
        name, then_t, else_t = nar if nar else (None, None, None)
        s.then_bind = self.block(s.then, (name, then_t))
        s.else_bind = self.block(s.else_, (name, else_t))
        if nar and not s.else_ and else_t is not None and exits(s.then):
            # early exit: `if x is none: return ...` narrows x for the rest of the enclosing block
            info = self.scopes.declare(name, else_t)
            info.narrowed = True
            s.after_bind = info

    def stmt(self, s: ast.Stmt) -> None:
        match s:
            case ast.ExprStmt(e):
                self.expr(e)
            case ast.Assign():
                self.assign(s)
            case ast.If():
                self.if_stmt(s)
            case ast.While(cond, body):
                self.expect_bool(cond, "the `while` condition")
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
                    raise err(f"`{kw}` outside a loop", s)
            case ast.FnDef():
                raise err("functions can only be defined at the top level", s)
            case ast.StructDef():
                raise err("structs can only be defined at the top level", s)

    def expect_bool(self, e: ast.Expr, what: str) -> None:
        t = self.expr(e)
        if t != BOOL:
            raise err(f"{what} must be bool, not {t}", e)

    def return_(self, s: ast.Return, value: ast.Expr | None) -> None:
        if self.ret is None:
            raise err("`return` outside a function", s)
        if value is None:
            if self.ret != UNIT:
                raise err(f"`return` without a value in a function that returns {self.ret}", s)
            return
        if self.ret == UNIT:
            raise err("the function declares no return type (`-> T`) but returns a value", s)
        t = self.expr(value, self.ret)
        if t != self.ret:
            raise err(f"returning {t} but the function declares {self.ret}", value)

    def for_loop(self, s: ast.For) -> None:
        if s.var == "self":
            raise err("`self` cannot be used as a loop variable", s)
        it = s.iter
        if isinstance(it, ast.Call) and it.func == "range":
            if not 1 <= len(it.args) <= 2:
                raise err("range takes 1 or 2 arguments", it)
            for a in it.args:
                t = self.expr(a)
                if t != INT:
                    raise err(f"range expects int, not {t}", a)
            it.ty = ListT(INT)
            var_t: Type = INT
        else:
            t = self.expr(it)
            if not isinstance(t, ListT):
                raise err(f"can only iterate over range(...) or a list, not {t}", it)
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
                raise err(f"cannot assign by index to {base_t}", target)
            self.expect_int(target.index)
            vt = self.expr(s.value, base_t.elem)
            if vt != base_t.elem:
                raise err(f"assigning {vt} to an element of {base_t}", s.value)
            target.ty = base_t.elem
            self.mark_mutable(target.value)
            return
        if isinstance(target, ast.Attr):
            ft = self.expr(target)
            vt = self.expr(s.value, ft)
            if vt != ft:
                raise err(f"field {target.name!r} is {ft} but {vt} is assigned", s.value)
            self.mark_mutable(target)
            return
        if target.id == "self":
            raise err("`self` cannot be declared or reassigned" if self.owner is None
                      else "cannot reassign `self`; modify its fields (`self.x = ...`)", s)
        existing = self.scopes.lookup(target.id)
        if s.annotation:
            if existing:
                raise err(f"{target.id!r} is already declared", s)
            declared = self.resolve(s.annotation)
            vt = self.expr(s.value, declared)
            if vt != declared:
                raise err(f"{target.id!r} is {declared} but {vt} is assigned", s.value)
            s.declares, s.var = True, self.scopes.declare(target.id, declared)
        elif existing:
            if existing.narrowed:
                raise err(f"{target.id!r} is narrowed to {existing.ty} by an `is` check here and cannot be reassigned", s)
            vt = self.expr(s.value, existing.ty)
            if vt != existing.ty:
                raise err(f"{target.id!r} is {existing.ty} but {vt} is assigned", s.value)
            existing.mutable = True
            s.var = existing
        else:
            vt = self.expr(s.value)
            if vt == UNIT:
                raise err("the expression produces no value", s.value)
            s.declares, s.var = True, self.scopes.declare(target.id, vt)
        target.ty = s.var.ty

    def mark_mutable(self, e: ast.Expr) -> None:
        """Mark the root variable of a place (`xs[i]`, `p.x`, `p.items[0]`...) as mutable."""
        while isinstance(e, (ast.Index, ast.Attr)):
            e = e.value
        if isinstance(e, ast.Name) and (info := self.scopes.lookup(e.id)):
            info.mutable = True

    def expect_int(self, e: ast.Expr) -> None:
        t = self.expr(e)
        if t != INT:
            raise err(f"expected int, not {t}", e)

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
                        raise err(f"{id!r} is a struct; build it with parentheses: {id}(...)", e)
                    if id in self.fns or id in BUILTINS:
                        raise err(f"{id!r} is a function; call it with parentheses", e)
                    raise err(f"undefined variable {id!r}", e)
                return info.ty
            case ast.UnaryOp(op, x):
                return self.unary(e, op, x)
            case ast.BinOp(op, l, r):
                return self.binop(e, op, l, r)
            case ast.Call():
                return self.call(e, expected)
            case ast.NoneLit():
                if isinstance(expected, OptionT):
                    return expected
                raise err("cannot infer the type of `none`; annotate it, e.g. `x: int? = none`", e)
            case ast.IsCheck(v, kind):
                t = self.expr(v)
                allowed = {"some", "none"} if isinstance(t, OptionT) else {"ok", "err"} if isinstance(t, ResultT) else None
                if allowed is None:
                    raise err(f"`is {kind}` needs an Option or a Result, not {t}", e)
                if kind not in allowed:
                    raise err(f"`is {kind}` cannot be applied to {t}; use `is {'` or `is '.join(sorted(allowed))}`", e)
                return BOOL
            case ast.Try(v):
                return self.try_op(e, v)
            case ast.MethodCall():
                return self.method_call(e)
            case ast.Attr(v, name):
                return self.attr(e, v, name)
            case ast.ListLit(items):
                return self.list_lit(e, items, expected)
            case ast.Index(v, i):
                vt = self.expr(v)
                if not isinstance(vt, ListT):
                    raise err(f"cannot index {vt}", e)
                self.expect_int(i)
                return vt.elem
        raise err(f"unsupported expression: {type(e).__name__}", e)

    def unary(self, e: ast.UnaryOp, op: str, x: ast.Expr) -> Type:
        t = self.expr(x)
        if op == "not":
            if t != BOOL:
                raise err(f"`not` expects bool, not {t}", e)
            return BOOL
        if t not in (INT, FLOAT):
            raise err(f"`-` expects int or float, not {t}", e)
        return t

    def binop(self, e: ast.BinOp, op: str, l: ast.Expr, r: ast.Expr) -> Type:
        if isinstance(l, ast.NoneLit):  # `none == x`: the other side gives the type
            rt = self.expr(r)
            lt = self.expr(l, rt)
        else:
            lt = self.expr(l)
            rt = self.expr(r, lt)
        if op in ("and", "or"):
            if lt != BOOL or rt != BOOL:
                raise err(f"`{op}` expects bool and bool, not {lt} and {rt}", e)
            return BOOL
        if lt != rt:
            hint = " (use float(x) or int(x) to convert)" if {lt, rt} == {INT, FLOAT} else ""
            raise err(f"`{op}` between {lt} and {rt}: types must match{hint}", e)
        if op in ARITH:
            if lt in (INT, FLOAT) or (op == "+" and lt == STR):
                return lt
            raise err(f"`{op}` is not defined for {lt}", e)
        if op in ORDER:
            if lt in (INT, FLOAT, STR):
                return BOOL
            raise err(f"`{op}` is not defined for {lt}", e)
        if op in EQ:
            return BOOL
        raise err(f"unknown operator {op!r}", e)

    def list_lit(self, e: ast.ListLit, items: list[ast.Expr], expected: Type | None) -> Type:
        if not items:
            if isinstance(expected, ListT):
                return expected
            raise err("cannot infer the type of an empty list; annotate it: `xs: list[int] = []`", e)
        elem = expected.elem if isinstance(expected, ListT) else None
        first = self.expr(items[0], elem)
        for it in items[1:]:
            t = self.expr(it, first)
            if t != first:
                raise err(f"the list mixes {first} and {t}", it)
        return ListT(first)

    # ---- structs: fields and methods -----------------------------------------------------

    def struct_of(self, t: Type, node: ast.Node, what: str) -> StructInfo:
        if not isinstance(t, StructT):
            raise err(f"{t} has no {what}", node)
        return self.structs[t.name]

    def attr(self, e: ast.Attr, v: ast.Expr, name: str) -> Type:
        t = self.expr(v)
        info = self.struct_of(t, e, "fields")
        ft = info.fields.get(name)
        if ft is None:
            have = ", ".join(info.fields)
            hint = f" (has: {have})" if info.fields else ""
            if name in info.methods:
                raise err(f"{name!r} is a method of {t}; call it with parentheses: .{name}(...)", e)
            raise err(f"{t} has no field {name!r}{hint}", e)
        return ft

    def try_op(self, e: ast.Try, v: ast.Expr) -> Type:
        t = self.expr(v)
        if self.ret is None:
            raise err("`?` can only be used inside a function", e)
        if isinstance(t, OptionT):
            if not isinstance(self.ret, OptionT):
                raise err(f"`?` on {t} requires the function to return an Option, but it returns {self.ret}", e)
            return t.inner
        if isinstance(t, ResultT):
            if not isinstance(self.ret, ResultT) or self.ret.err != t.err:
                raise err(f"`?` on {t} requires the function to return Result[..., {t.err}], but it returns {self.ret}", e)
            return t.ok
        raise err(f"`?` can only be applied to an Option or a Result, not {t}", e)

    def builtin_method(self, e: ast.MethodCall, rt: Type) -> Type:
        inner = rt.inner if isinstance(rt, OptionT) else rt.ok
        if e.name == "unwrap":
            if e.args:
                raise err("unwrap takes no arguments", e)
            return inner
        if e.name == "unwrap_or":
            if len(e.args) != 1:
                raise err("unwrap_or takes exactly 1 argument", e)
            t = self.expr(e.args[0], inner)
            if t != inner:
                raise err(f"unwrap_or on {rt} expects {inner}, got {t}", e.args[0])
            return inner
        raise err(f"{rt} has no method {e.name!r} (available: unwrap, unwrap_or)", e)

    def method_call(self, e: ast.MethodCall) -> Type:
        rt = self.expr(e.value)
        if isinstance(rt, (OptionT, ResultT)):
            return self.builtin_method(e, rt)
        info = self.struct_of(rt, e, "methods")
        sig = info.methods.get(e.name)
        if sig is None:
            if e.name in info.fields:
                raise err(f"{e.name!r} is a field of {rt}, not a method", e)
            have = ", ".join(info.methods)
            hint = f" (has: {have})" if info.methods else ""
            raise err(f"{rt} has no method {e.name!r}{hint}", e)
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
            raise err(f"{name} takes {len(sig.params)} argument(s), got {len(args)}", e)
        for i, (a, pt) in enumerate(zip(args, sig.params), start=1):
            t = self.expr(a, pt)
            if t != pt:
                raise err(f"argument {i} of {name}: expected {pt}, got {t}", a)

    def call(self, e: ast.Call, expected: Type | None = None) -> Type:
        if e.func in self.structs:
            return self.ctor(e)
        if e.func in BUILTINS:
            return self.builtin(e, expected)
        sig = self.fns.get(e.func)
        if sig is None:
            raise err(f"undefined function {e.func!r}", e)
        self.check_args(e, e.func, e.args, sig)
        return sig.ret

    def builtin(self, e: ast.Call, expected: Type | None = None) -> Type:
        name, args = e.func, e.args

        def arity(n: int) -> None:
            if len(args) != n:
                raise err(f"{name} takes {n} argument(s), got {len(args)}", e)

        match name:
            case "print":
                arity(1)
                self.expr(args[0])
                return UNIT
            case "len":
                arity(1)
                t = self.expr(args[0])
                if t != STR and not isinstance(t, ListT):
                    raise err(f"len expects str or list, not {t}", args[0])
                return INT
            case "int" | "float":
                arity(1)
                t = self.expr(args[0])
                if t not in (INT, FLOAT, STR):
                    raise err(f"{name}() cannot convert {t}", args[0])
                return INT if name == "int" else FLOAT
            case "str":
                arity(1)
                t = self.expr(args[0])
                if t not in (INT, FLOAT, BOOL, STR):
                    raise err(f"str() cannot convert {t}", args[0])
                return STR
            case "append":
                arity(2)
                lt = self.expr(args[0])
                if not isinstance(lt, ListT):
                    raise err(f"append expects a list, not {lt}", args[0])
                vt = self.expr(args[1], lt.elem)
                if vt != lt.elem:
                    raise err(f"cannot append {vt} to {lt}", args[1])
                self.mark_mutable(args[0])
                return UNIT
            case "exit":
                arity(1)
                self.expect_int(args[0])
                return UNIT
            case "some":
                arity(1)
                t = self.expr(args[0], expected.inner if isinstance(expected, OptionT) else None)
                if isinstance(t, OptionT):
                    raise err("nested Option types are not allowed", args[0])
                return OptionT(t)
            case "ok" | "err":
                arity(1)
                if not isinstance(expected, ResultT):
                    raise err(f"cannot infer the type of `{name}(...)`; annotate it, e.g. `r: Result[int, str] = {name}(...)`", e)
                want = expected.ok if name == "ok" else expected.err
                t = self.expr(args[0], want)
                if t != want:
                    raise err(f"{name}(...) expects {want}, got {t}", args[0])
                return expected
            case "parse_int" | "parse_float":
                arity(1)
                t = self.expr(args[0])
                if t != STR:
                    raise err(f"{name} expects str, not {t}", args[0])
                return OptionT(INT if name == "parse_int" else FLOAT)
            case "range":
                raise err("range(...) can only be used in a `for`", e)
        raise err(f"unknown built-in function {name!r}", e)


def always_returns(body: list[ast.Stmt]) -> bool:
    if not body:
        return False
    last = body[-1]
    if isinstance(last, ast.Return):
        return True
    if isinstance(last, ast.If):
        return always_returns(last.then) and always_returns(last.else_)
    return False


def exits(body: list[ast.Stmt]) -> bool:
    """True if control never falls out of the end of `body` (return/break/continue/exit)."""
    if not body:
        return False
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Break, ast.Continue)):
        return True
    if isinstance(last, ast.ExprStmt) and isinstance(last.expr, ast.Call) and last.expr.func == "exit":
        return True
    if isinstance(last, ast.If):
        return exits(last.then) and exits(last.else_)
    return False


def check(prog: ast.Program) -> ast.Program:
    """Check the program. Repeats until the set of methods that modify `self`
    stabilizes (a method that calls a modifying method is one too); converges in a few rounds."""
    mutating: set[tuple[str, str]] = set()
    while True:
        c = Checker(mutating)
        c.check(prog)
        if c.mutating == mutating:
            return prog
        mutating = c.mutating
