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
import re
from dataclasses import dataclass, field

from zylo.domain import ast
from zylo.domain.builtins import BUILTINS, SHADOWABLE  # noqa: F401
from zylo.domain.errors import ZyloError
from zylo.domain.types import (
    BOOL,
    FLOAT,
    INT,
    PRIMS,
    STR,
    UNIT,
    DictT,
    FnT,
    ListT,
    OptionT,
    ResultT,
    SetT,
    StructT,
    TupleT,
    Type,
    VarInfo,
    is_copy_type,
)

ARITH = {"+", "-", "*", "/", "%"}
EQ = {"==", "!="}
ORDER = {"<", ">", "<=", ">="}


def err(msg: str, node: ast.Node) -> ZyloError:
    """Build an error positioned at a node.

    Args:
        msg: The message.
        node: The node the problem is about; it supplies the line, column and file.

    Returns:
        The error, ready to be raised.
    """
    return ZyloError(msg, node.line, node.col, node.file)


class _Poisoned(Exception):
    """Raised when a name's declaration already failed, to avoid reporting the same mistake again and again."""


class Scopes:
    """A stack of lexical scopes mapping names to the variables visible in them."""
    def __init__(self) -> None:
        """Create a stack holding one empty (outermost) scope."""
        self.stack: list[dict[str, VarInfo]] = [{}]

    def push(self) -> None:
        """Open a new innermost scope."""
        self.stack.append({})

    def pop(self) -> None:
        """Close the innermost scope."""
        self.stack.pop()

    def lookup(self, name: str) -> VarInfo | None:
        """Find a visible variable.

        Args:
            name: The variable's name.

        Returns:
            The innermost visible variable with that name, or None.
        """
        for scope in reversed(self.stack):
            if name in scope:
                return scope[name]
        return None

    def declare(self, name: str, ty: Type) -> VarInfo:
        """Declare a variable in the innermost scope.

        Args:
            name: The variable's name.
            ty: Its type.

        Returns:
            The new variable's record.
        """
        info = VarInfo(name, ty)
        self.stack[-1][name] = info
        return info


@dataclass
class StructInfo:
    """What the checker knows about one struct.

    Attributes:
        name: The struct's name.
        defn: Its definition node.
        fields: Field types by name, in declaration order.
        methods: Method signatures by name.
    """
    name: str
    defn: ast.StructDef
    fields: dict[str, Type] = field(default_factory=dict)
    methods: dict[str, FnT] = field(default_factory=dict)


class Checker:
    """Type checker for one program.

    Resolves types, infers the types of expressions, tracks scopes, decides which variables are mutable and which
    parameters can be borrowed, narrows `Option`/`Result` values after `is` checks, and annotates the tree for the code
    generator. Use `check()` rather than this class directly: it reruns the checker until the set of methods that modify
    `self` stops changing.

    Example:
        >>> from zylo.syntax.parser import parse
        >>> prog = parse("x = 1 + 2")
    >>> Checker().check(prog)
    """
    def __init__(self, mutating: set[tuple[str, str]] | None = None) -> None:
        """Create a checker.

        Args:
            mutating: `(struct, method)` pairs already known to modify `self`, from a previous run.
        """
        self.fns: dict[str, FnT] = {}
        self.structs: dict[str, StructInfo] = {}
        self.mutating: set[tuple[str, str]] = set(mutating or ())  # (struct, method) pairs that modify self
        self.scopes = Scopes()
        self.ret: Type | None = None  # return type of the current function (None = top level)
        self.owner: str | None = None  # struct of the current method
        self.loops = 0
        self.errors: list[ZyloError] = []  # every mistake found; the checker goes on after one
        self.poisoned: set[str] = set()  # names whose declaration failed: later uses are not reported again

    # ---- entry point ---------------------------------------------------------------------

    def check(self, prog: ast.Program) -> None:
        """Check a whole program, annotating it in place.

        Args:
            prog: The program to check.

        Raises:
            ZyloError: Never: mistakes are collected in `errors` (see `check_all`).
        """
        try:
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
        except ZyloError as e:  # a broken declaration makes the rest meaningless: report it and stop
            self.errors.append(e)
            return
        for s in prog.body:
            try:
                if isinstance(s, ast.ExternFn):
                    self.check_extern(s)
                elif isinstance(s, ast.FnDef):
                    self.check_fn(s)
                elif isinstance(s, ast.StructDef):
                    for m in s.methods:
                        try:
                            self.check_fn(m, s.name)
                        except ZyloError as e:
                            self.errors.append(e)
                else:
                    self.stmt(s)
            except ZyloError as e:
                self.errors.append(e)

    # ---- written types ------------------------------------------------------------

    def resolve(self, t: ast.TypeRef) -> Type:
        """Turn a written type into a real one.

        Args:
            t: The type as the user wrote it.

        Returns:
            The resolved type.

        Raises:
            ZyloError: If it names an unknown type or uses an invalid dict key.
        """
        if t.name == "list":
            return ListT(self.resolve(t.args[0]))
        if t.name == "?":
            return OptionT(self.resolve(t.args[0]))
        if t.name == "Result":
            return ResultT(self.resolve(t.args[0]), self.resolve(t.args[1]))
        if t.name == "tuple":
            return TupleT(tuple(self.resolve(a) for a in t.args))
        if t.name in ("dict", "set"):
            args = [self.resolve(a) for a in t.args]
            if not key_ok(args[0]):
                raise err(f"{'dict keys' if t.name == 'dict' else 'set elements'} must be int, str, bool or tuples of those, not {args[0]}", t)
            return DictT(*args) if t.name == "dict" else SetT(args[0])
        if t.name in PRIMS:
            return PRIMS[t.name]
        if t.name in self.structs:
            return StructT(t.name)
        raise err(f"unknown type {t.name!r}", t)

    # ---- structs ----------------------------------------------------------------------

    def register_struct(self, d: ast.StructDef) -> None:
        """Record a struct's name, rejecting duplicates and reserved names."""
        if d.name in self.structs:
            raise err(f"struct {d.name!r} is already defined", d)
        if d.name in BUILTINS or d.name in PRIMS or d.name in ("list", "unit", "Result"):
            raise err(f"{d.name!r} is a reserved name", d)
        self.structs[d.name] = StructInfo(d.name, d)

    def resolve_struct(self, d: ast.StructDef) -> None:
        """Resolve the field types of a struct, rejecting duplicate fields and empty structs."""
        info = self.structs[d.name]
        if not d.fields:
            raise err(f"struct {d.name!r} needs at least one field", d)
        for f in d.fields:
            if f.name in info.fields:
                raise err(f"duplicate field {f.name!r} in {d.name!r}", f)
            f.ty = info.fields[f.name] = self.resolve(f.type)

    def check_cycles(self, defs: list[ast.StructDef]) -> None:
        """Reject structs that contain themselves directly or through other structs (lists are fine)."""
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
        """Record the signatures of a struct's methods, rejecting duplicates and clashes with fields."""
        info = self.structs[d.name]
        for m in d.methods:
            if not m.has_self:
                raise err(f"method {m.name!r} must take `self` as its first parameter", m)
            if m.name in info.fields:
                raise err(f"{m.name!r} is both a field and a method of {d.name!r}", m)
            if m.name in info.methods:
                raise err(f"method {m.name!r} is already defined in {d.name!r}", m)
            m.ty = info.methods[m.name] = self.signature(m)

    # ---- functions ------------------------------------------------------------------

    def signature(self, f: ast.FnDef) -> FnT:
        """Compute a function's type from its declaration.

        Args:
            f: The function or method.

        Returns:
            Its parameter and return types.

        Raises:
            ZyloError: On a duplicate parameter name.
        """
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
        """Record a top-level function (or `extern fn`) so it can be called before it is defined."""
        if f.name in BUILTINS - SHADOWABLE:
            raise err(f"{f.name!r} is a built-in function", f)
        if f.name in self.structs:
            raise err(f"{f.name!r} is already a struct", f)
        if f.name in self.fns:
            raise err(f"function {f.name!r} is already defined", f)
        f.ty = self.fns[f.name] = self.signature(f)

    def check_extern(self, f: ast.ExternFn) -> None:
        """Validate an `extern fn`: it needs a template, and `{n}` placeholders must name real parameters."""
        if not f.template.strip():
            raise err(f"extern fn {f.name!r} needs a Rust path or template after `=`", f)
        for n in re.findall(r"\{(\d+)\}", f.template):
            if int(n) >= len(f.params):
                raise err(f"the template of {f.name!r} uses {{{n}}} but the function has {len(f.params)} parameter(s)", f)

    def check_fn(self, f: ast.FnDef, owner: str | None = None) -> None:
        """Check a function or method body.

        Also decides which parameters are never modified (and so can be borrowed) and whether a method modifies `self`.

        Args:
            f: The function.
            owner: The struct's name when `f` is a method.

        Raises:
            ZyloError: On a type error in the body, or when a function with a return type can finish without returning.
        """
        sig = f.ty
        top, saved_poison = self.scopes, self.poisoned
        self.scopes, self.ret, self.loops, self.owner, self.poisoned = Scopes(), sig.ret, 0, owner, set()
        try:
            self_info = self.scopes.declare("self", StructT(owner)) if owner else None
            for p, pt in zip(f.params, sig.params):
                p.info = self.scopes.declare(p.name, pt)
            before = len(self.errors)
            self.block(f.body)
            # a `return` that failed to check is not a missing return: do not pile a second error on the first
            if len(self.errors) == before and sig.ret != UNIT and not always_returns(f.body):
                raise err(f"function {f.name!r} can finish without a `return`", f)
            if owner:
                if self_info.mutable:
                    self.mutating.add((owner, f.name))
                f.self_mut = (owner, f.name) in self.mutating
            # A parameter that is never modified is lent instead of cloned. A method that modifies `self` takes its
            # arguments by value: they may be parts of `self`, which cannot be lent while `self` is borrowed mutably.
            for p in f.params:
                p.info.by_ref = not p.info.mutable and not is_copy_type(p.info.ty) and not f.self_mut
        finally:
            self.scopes, self.ret, self.owner, self.poisoned = top, None, None, saved_poison

    # ---- statements ------------------------------------------------------------------

    def block(self, body: list[ast.Stmt], bind: tuple[str | None, Type | None] | None = None) -> VarInfo | None:
        """Check a block in its own scope.

        Args:
            body: The statements.
            bind: `(name, type)` to rebind a variable with a narrowed type for the whole block (see `is` narrowing).

        Returns:
            The new variable's record when `bind` was used, otherwise None.
        """
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
        """Work out what an `is` check tells about a variable.

        Args:
            cond: The condition of an `if`.

        Returns:
            For `x is K` on a plain variable, `(name, type of x in the then-branch, type in the else-branch)`, where a
            type of None means no information; None for any other condition.
        """
        if not (isinstance(cond, ast.IsCheck) and isinstance(cond.value, ast.Name)):
            return None
        t, k = cond.value.ty, cond.kind
        if isinstance(t, OptionT):
            then_t, else_t = (t.inner, None) if k == "some" else (None, t.inner)
        else:
            then_t, else_t = (t.ok, t.err) if k == "ok" else (t.err, t.ok)
        return cond.value.id, then_t, else_t

    def if_stmt(self, s: ast.If) -> None:
        """Check an `if`, narrowing `Option`/`Result` variables tested with `is` inside its branches."""
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
        """Check one statement; a mistake is recorded in `errors` and checking goes on with the next statement.

        Args:
            s: The statement.
        """
        try:
            self._stmt(s)
        except _Poisoned:
            self.poison(s)  # it only repeats an earlier mistake
        except ZyloError as e:
            self.errors.append(e)
            self.poison(s)

    def poison(self, s: ast.Stmt) -> None:
        """Mark the variables a failed statement should have declared, so later uses are not reported too."""
        names: list[str] = []
        if isinstance(s, ast.Assign) and isinstance(s.target, ast.Name):
            names = [s.target.id]
        elif isinstance(s, ast.Unpack):
            names = [t.id for t in s.targets]
        self.poisoned.update(n for n in names if self.scopes.lookup(n) is None)

    def _stmt(self, s: ast.Stmt) -> None:
        match s:
            case ast.ExprStmt(e):
                self.expr(e)
            case ast.Assign():
                self.assign(s)
            case ast.Unpack():
                self.unpack(s)
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
            case ast.Import() | ast.FromImport():
                raise err("imports can only appear at the top level", s)

    def expect_bool(self, e: ast.Expr, what: str) -> None:
        """Require an expression to be a bool.

        Args:
            e: The expression.
            what: What it is, for the error message (such as "the `if` condition").
        """
        t = self.expr(e)
        if t != BOOL:
            raise err(f"{what} must be bool, not {t}", e)

    def return_(self, s: ast.Return, value: ast.Expr | None) -> None:
        """Check a `return` against the enclosing function's declared return type."""
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

    def unpack(self, s: ast.Unpack) -> None:
        """Check a tuple unpacking assignment, declaring or reassigning each target."""
        t = self.expr(s.value)
        n = len(s.targets)
        if not isinstance(t, TupleT) or len(t.elems) != n:
            have = f"{len(t.elems)} values" if isinstance(t, TupleT) else str(t)
            raise err(f"cannot unpack {have} into {n} variables", s)
        seen: set[str] = set()
        s.declares, s.vars = [], []
        for target, et in zip(s.targets, t.elems):
            name = target.id
            if name == "self":
                raise err("`self` cannot be declared or reassigned", target)
            if name in seen:
                raise err(f"{name!r} appears twice on the left of the assignment", target)
            seen.add(name)
            existing = self.scopes.lookup(name)
            if existing is None:
                s.declares.append(True)
                s.vars.append(self.scopes.declare(name, et))
            else:
                if existing.narrowed:
                    raise err(f"{name!r} is narrowed to {existing.ty} by an `is` check here and cannot be reassigned", target)
                if existing.ty != et:
                    raise err(f"{name!r} is {existing.ty} but {et} is assigned", target)
                existing.mutable = True
                s.declares.append(False)
                s.vars.append(existing)
            target.ty = et

    def for_loop(self, s: ast.For) -> None:
        """Check a `for` loop and declare its loop variable(s) from the iterated type."""
        if "self" in (s.targets or [s.var]):
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
            if isinstance(t, ListT):
                var_t = t.elem
            elif isinstance(t, SetT):
                var_t = t.elem
            elif isinstance(t, DictT):
                var_t = t.key
            elif t == STR:
                var_t = STR
            else:
                raise err(f"can only iterate over range(...), a list, str, set or dict, not {t}", it)
        self.scopes.push()
        if s.targets:
            if not isinstance(var_t, TupleT) or len(var_t.elems) != len(s.targets):
                raise err(f"cannot unpack {var_t} into {len(s.targets)} variables", s)
            if len(set(s.targets)) != len(s.targets):
                raise err("duplicate loop variable", s)
            s.infos = [self.scopes.declare(n, et) for n, et in zip(s.targets, var_t.elems)]
        else:
            s.info = self.scopes.declare(s.var, var_t)
        self.loops += 1
        for b in s.body:
            self.stmt(b)
        self.loops -= 1
        self.scopes.pop()

    def assign(self, s: ast.Assign) -> None:
        """Check an assignment to a name, an index, a dict key or a field, deciding declare versus reassign."""
        target = s.target
        if isinstance(target, ast.Index):
            base_t = self.expr(target.value)
            if isinstance(base_t, TupleT):
                raise err("tuples are immutable: build a new tuple instead", target)
            if isinstance(base_t, DictT):
                kt = self.expr(target.index, base_t.key)
                if kt != base_t.key:
                    raise err(f"this dict has {base_t.key} keys, not {kt}", target.index)
                vt = self.expr(s.value, base_t.val)
                if vt != base_t.val:
                    raise err(f"assigning {vt} to a value of {base_t}", s.value)
                target.ty = base_t.val
                self.mark_mutable(target.value)
                return
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
        """Require an expression to be an int.

        Args:
            e: The expression.
        """
        t = self.expr(e)
        if t != INT:
            raise err(f"expected int, not {t}", e)

    # ---- expressions -----------------------------------------------------------------

    def expr(self, e: ast.Expr, expected: Type | None = None) -> Type:
        """Infer the type of an expression and record it on the node.

        Args:
            e: The expression.
            expected: The type the context wants, used for literals that cannot be typed alone (`[]`, `none`, `ok(...)`).

        Returns:
            The expression's type.
        """
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
                    if id in self.poisoned:
                        raise _Poisoned
                    raise err(f"undefined variable {id!r}", e)
                e.var = info
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
            case ast.FString(parts):
                for p in parts:
                    if isinstance(p, ast.FormatPart):
                        self.format_part(p)
                return STR
            case ast.MethodCall():
                return self.method_call(e)
            case ast.Attr(v, name):
                return self.attr(e, v, name)
            case ast.ListLit(items):
                return self.list_lit(e, items, expected)
            case ast.DictLit(keys, values):
                return self.dict_lit(e, keys, values, expected)
            case ast.SetLit(items):
                et = expected.elem if isinstance(expected, SetT) else None
                first = self.expr(items[0], et)
                for it in items[1:]:
                    t = self.expr(it, first)
                    if t != first:
                        raise err(f"the set mixes {first} and {t}", it)
                if not key_ok(first):
                    raise err(f"set elements must be int, str, bool or tuples of those, not {first}", e)
                return SetT(first)
            case ast.Slice(v, lo, hi):
                vt = self.expr(v)
                if vt != STR and not isinstance(vt, ListT):
                    raise err(f"cannot slice {vt}", e)
                for b in (lo, hi):
                    if b is not None:
                        self.expect_int(b)
                return vt
            case ast.TupleLit(items):
                exp = expected.elems if isinstance(expected, TupleT) and len(expected.elems) == len(items) else [None] * len(items)
                return TupleT(tuple(self.expr(it, x) for it, x in zip(items, exp)))
            case ast.Index(v, i):
                vt = self.expr(v)
                if isinstance(vt, TupleT):
                    if not isinstance(i, ast.Int) or not 0 <= i.value < len(vt.elems):
                        raise err(f"a tuple of {len(vt.elems)} is indexed with a literal 0..{len(vt.elems) - 1}", e)
                    i.ty = INT
                    return vt.elems[i.value]
                if isinstance(vt, DictT):
                    kt = self.expr(i, vt.key)
                    if kt != vt.key:
                        raise err(f"this dict has {vt.key} keys, not {kt}", i)
                    return vt.val
                if vt == STR:
                    self.expect_int(i)
                    return STR
                if not isinstance(vt, ListT):
                    raise err(f"cannot index {vt}", e)
                self.expect_int(i)
                return vt.elem
        raise err(f"unsupported expression: {type(e).__name__}", e)

    def unary(self, e: ast.UnaryOp, op: str, x: ast.Expr) -> Type:
        """Type a prefix `-` or `not`."""
        t = self.expr(x)
        if op == "not":
            if t != BOOL:
                raise err(f"`not` expects bool, not {t}", e)
            return BOOL
        if t not in (INT, FLOAT):
            raise err(f"`-` expects int or float, not {t}", e)
        return t

    def binop(self, e: ast.BinOp, op: str, left: ast.Expr, right: ast.Expr) -> Type:
        """Type a binary operation, including `in`, `and`/`or` and comparisons.

        Raises:
            ZyloError: If the operand types do not fit the operator (there is no implicit int/float coercion).
        """
        if op in ("in", "not in"):
            rt = self.expr(right)
            want = rt.elem if isinstance(rt, (ListT, SetT)) else rt.key if isinstance(rt, DictT) else STR if rt == STR else None
            if want is None:
                raise err(f"`{op}` needs a list, str, set or dict on the right, not {rt}", e)
            lt = self.expr(left, want)
            if lt != want:
                raise err(f"`{op}` on {rt} needs a {want} on the left, not {lt}", e)
            return BOOL
        if isinstance(left, ast.NoneLit):  # `none == x`: the other side gives the type
            rt = self.expr(right)
            lt = self.expr(left, rt)
        else:
            lt = self.expr(left)
            rt = self.expr(right, lt)
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

    def dict_lit(self, e: ast.DictLit, keys: list[ast.Expr], values: list[ast.Expr], expected: Type | None) -> Type:
        """Type a dict literal, using the expected type for the empty `{}`."""
        if not keys:
            if isinstance(expected, (DictT, SetT)):
                return expected  # `{}` is an empty set when the context says so
            raise err("cannot infer the type of `{}`; annotate it: `d: dict[str, int] = {}`", e)
        kexp = expected.key if isinstance(expected, DictT) else None
        vexp = expected.val if isinstance(expected, DictT) else None
        kt, vt = self.expr(keys[0], kexp), self.expr(values[0], vexp)
        for k, v in zip(keys[1:], values[1:]):
            if self.expr(k, kt) != kt:
                raise err(f"the dict mixes {kt} and {k.ty} keys", k)
            if self.expr(v, vt) != vt:
                raise err(f"the dict mixes {vt} and {v.ty} values", v)
        if not key_ok(kt):
            raise err(f"dict keys must be int, str, bool or tuples of those, not {kt}", e)
        return DictT(kt, vt)

    def list_lit(self, e: ast.ListLit, items: list[ast.Expr], expected: Type | None) -> Type:
        """Type a list literal, using the expected type for the empty `[]`."""
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
        """Look up the struct a value belongs to.

        Args:
            t: The value's type.
            node: Where to report an error.
            what: "fields" or "methods", for the error message.

        Returns:
            The struct's description.

        Raises:
            ZyloError: If `t` is not a struct.
        """
        if not isinstance(t, StructT):
            raise err(f"{t} has no {what}", node)
        return self.structs[t.name]

    def attr(self, e: ast.Attr, v: ast.Expr, name: str) -> Type:
        """Type a field access `value.name`."""
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

    SPEC_RE = re.compile(r"^(?:[<>^])?0?\d*(?:\.\d+)?([fdsxXb]?)$")

    def format_part(self, p: ast.FormatPart) -> None:
        """Check one `{expression:spec}` field of an f-string: a printable value and a valid specification."""
        t = self.expr(p.value)
        if t == UNIT:
            raise err("the expression produces no value", p.value)
        m = self.SPEC_RE.match(p.spec)
        if not m:
            raise err(f"unsupported format specification {p.spec!r} (use e.g. `:.2f`, `:>8`, `:05d`)", p)
        kind = m.group(1)
        want = {"f": FLOAT, "d": INT, "x": INT, "X": INT, "b": INT, "s": STR}.get(kind)
        if want is not None and t != want:
            raise err(f"format type `{kind}` needs {want}, not {t}", p)

    def type_method(self, e: ast.MethodCall, rt: Type) -> Type:
        """Methods of the built-in str / list / dict / set types."""
        n, args = e.name, e.args

        def arity(lo: int, hi: int | None = None) -> None:
            hi = lo if hi is None else hi
            if not lo <= len(args) <= hi:
                want = str(lo) if lo == hi else f"{lo} or {hi}"
                raise err(f"{rt}.{n} takes {want} argument(s), got {len(args)}", e)

        def arg(i: int, want: Type) -> None:
            t = self.expr(args[i], want)
            if t != want:
                raise err(f"argument {i + 1} of {n}: expected {want}, got {t}", args[i])

        def mutates() -> None:
            self.mark_mutable(e.value)

        if rt == STR:
            if n in ("strip", "upper", "lower"):
                arity(0)
                return STR
            if n == "split":
                arity(0, 1)
                if args:
                    arg(0, STR)
                return ListT(STR)
            if n == "join":
                arity(1)
                arg(0, ListT(STR))
                return STR
            if n == "replace":
                arity(2)
                arg(0, STR)
                arg(1, STR)
                return STR
            if n in ("startswith", "endswith"):
                arity(1)
                arg(0, STR)
                return BOOL
            if n == "find":
                arity(1)
                arg(0, STR)
                return OptionT(INT)
            if n == "count":
                arity(1)
                arg(0, STR)
                return INT
            if n in ("lines", "chars"):
                arity(0)
                return ListT(STR)
            if n == "repeat":
                arity(1)
                arg(0, INT)
                return STR
            have = "strip, upper, lower, split, join, replace, startswith, endswith, find, count, lines, chars, repeat"
        elif isinstance(rt, ListT):
            el = rt.elem
            if n == "append":
                arity(1)
                arg(0, el)
                mutates()
                return UNIT
            if n == "pop":
                arity(0)
                if isinstance(el, OptionT):
                    raise err("pop() on a list of Option values is not supported", e)
                mutates()
                return OptionT(el)
            if n == "insert":
                arity(2)
                arg(0, INT)
                arg(1, el)
                mutates()
                return UNIT
            if n == "remove":
                arity(1)
                arg(0, el)
                mutates()
                return BOOL
            if n in ("clear", "reverse"):
                arity(0)
                mutates()
                return UNIT
            if n == "sort":
                arity(0)
                if not sortable(el):
                    raise err(f"cannot sort a list of {el}", e)
                mutates()
                return UNIT
            if n == "extend":
                arity(1)
                arg(0, rt)
                mutates()
                return UNIT
            if n == "index":
                arity(1)
                arg(0, el)
                return OptionT(INT)
            if n == "count":
                arity(1)
                arg(0, el)
                return INT
            have = "append, pop, insert, remove, clear, reverse, sort, extend, index, count"
        elif isinstance(rt, DictT):
            if n == "get":
                arity(1, 2)
                if isinstance(rt.val, OptionT):
                    raise err("get() on a dict of Option values is not supported", e)
                arg(0, rt.key)
                if len(args) == 2:
                    arg(1, rt.val)
                    return rt.val
                return OptionT(rt.val)
            if n == "keys":
                arity(0)
                return ListT(rt.key)
            if n == "values":
                arity(0)
                return ListT(rt.val)
            if n == "items":
                arity(0)
                return ListT(TupleT((rt.key, rt.val)))
            if n == "pop":
                arity(1)
                if isinstance(rt.val, OptionT):
                    raise err("pop() on a dict of Option values is not supported", e)
                arg(0, rt.key)
                mutates()
                return OptionT(rt.val)
            if n == "clear":
                arity(0)
                mutates()
                return UNIT
            have = "get, keys, values, items, pop, clear"
        else:  # SetT
            if n == "add":
                arity(1)
                arg(0, rt.elem)
                mutates()
                return UNIT
            if n == "remove":
                arity(1)
                arg(0, rt.elem)
                mutates()
                return BOOL
            if n in ("union", "intersection", "difference"):
                arity(1)
                arg(0, rt)
                return rt
            if n == "clear":
                arity(0)
                mutates()
                return UNIT
            have = "add, remove, union, intersection, difference, clear"
        raise err(f"{rt} has no method {n!r} (available: {have})", e)

    def try_op(self, e: ast.Try, v: ast.Expr) -> Type:
        """Type the postfix `?` operator.

        Raises:
            ZyloError: Outside a function, on a value that is not an Option or Result, or when the enclosing function does not
                return a compatible Option or Result.
        """
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
        """Type `unwrap()` and `unwrap_or(default)` on an Option or Result."""
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
        """Type a method call on a struct, a string, a list, a dict, a set, an Option or a Result."""
        rt = self.expr(e.value)
        if isinstance(rt, (OptionT, ResultT)):
            return self.builtin_method(e, rt)
        if rt == STR or isinstance(rt, (ListT, DictT, SetT)):
            return self.type_method(e, rt)
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
        """Type a struct constructor call; arguments are positional and cover every field."""
        info = self.structs[e.func]
        types = list(info.fields.values())
        self.check_args(e, e.func, e.args, FnT(tuple(types), StructT(e.func)))
        e.ctor_fields = list(info.fields)
        return StructT(e.func)

    # ---- calls ------------------------------------------------------------------

    def check_args(self, e: ast.Node, name: str, args: list[ast.Expr], sig: FnT) -> None:
        """Check call arguments against a signature.

        Args:
            e: The call, for error positions.
            name: The callee's name, for messages.
            args: The argument expressions.
            sig: The callee's type.

        Raises:
            ZyloError: On a wrong number of arguments or a mismatched type.
        """
        if len(args) != len(sig.params):
            raise err(f"{name} takes {len(sig.params)} argument(s), got {len(args)}", e)
        for i, (a, pt) in enumerate(zip(args, sig.params), start=1):
            t = self.expr(a, pt)
            if t != pt:
                raise err(f"argument {i} of {name}: expected {pt}, got {t}", a)

    def call(self, e: ast.Call, expected: Type | None = None) -> Type:
        """Type a call: a struct constructor, a built-in, or a user function."""
        if e.func in self.structs:
            return self.ctor(e)
        if e.func in BUILTINS and not (e.func in SHADOWABLE and e.func in self.fns):
            return self.builtin(e, expected)
        sig = self.fns.get(e.func)
        if sig is None:
            raise err(f"undefined function {e.func!r}", e)
        self.check_args(e, e.func, e.args, sig)
        return sig.ret

    def builtin(self, e: ast.Call, expected: Type | None = None) -> Type:
        """Type a call of a built-in function such as `print`, `len`, `sum` or `read_file`."""
        name, args = e.func, e.args

        def arity(n: int) -> None:
            if len(args) != n:
                raise err(f"{name} takes {n} argument(s), got {len(args)}", e)

        match name:
            case "print":  # any number of values, separated by spaces
                for a in args:
                    if self.expr(a) == UNIT:
                        raise err("the expression produces no value", a)
                return UNIT
            case "len":
                arity(1)
                t = self.expr(args[0])
                if t != STR and not isinstance(t, (ListT, DictT, SetT)):
                    raise err(f"len expects str, list, dict or set, not {t}", args[0])
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
            case "sum":
                arity(1)
                t = self.expr(args[0])
                if not (isinstance(t, ListT) and t.elem in (INT, FLOAT)):
                    raise err(f"sum expects a list of int or float, not {t}", args[0])
                return t.elem
            case "min" | "max":
                if len(args) not in (1, 2):
                    raise err(f"{name} takes 1 or 2 argument(s), got {len(args)}", e)
                t = self.expr(args[0])
                if len(args) == 1:
                    if not (isinstance(t, ListT) and sortable(t.elem)):
                        raise err(f"{name} of one argument expects a list of comparable values, not {t}", args[0])
                    return t.elem
                u = self.expr(args[1], t)
                if t != u or t not in (INT, FLOAT, STR):
                    raise err(f"{name} compares two int, float or str values of the same type, not {t} and {u}", e)
                return t
            case "abs":
                arity(1)
                t = self.expr(args[0])
                if t not in (INT, FLOAT):
                    raise err(f"abs expects int or float, not {t}", args[0])
                return t
            case "sorted" | "reversed":
                arity(1)
                t = self.expr(args[0])
                if not isinstance(t, ListT) or (name == "sorted" and not sortable(t.elem)):
                    raise err(f"{name} expects a list{' of comparable values' if name == 'sorted' else ''}, not {t}", args[0])
                return t
            case "enumerate":
                arity(1)
                t = self.expr(args[0])
                if not isinstance(t, ListT):
                    raise err(f"enumerate expects a list, not {t}", args[0])
                return ListT(TupleT((INT, t.elem)))
            case "zip":
                arity(2)
                a, b = self.expr(args[0]), self.expr(args[1])
                if not (isinstance(a, ListT) and isinstance(b, ListT)):
                    raise err(f"zip expects two lists, not {a} and {b}", e)
                return ListT(TupleT((a.elem, b.elem)))
            case "sqrt" | "pow" | "round" | "floor" | "ceil":
                arity(2 if name == "pow" else 1)
                for a in args:
                    t = self.expr(a, FLOAT)
                    if t != FLOAT:
                        raise err(f"{name} expects float, not {t} (use float(x))", a)
                return FLOAT if name in ("sqrt", "pow") else INT
            case "argv" | "read_stdin" | "read_line" | "time":
                arity(0)
                return {"argv": ListT(STR), "read_stdin": STR, "read_line": OptionT(STR), "time": FLOAT}[name]
            case "input":
                if len(args) > 1:
                    raise err(f"input takes 0 or 1 argument(s), got {len(args)}", e)
                if args and self.expr(args[0], STR) != STR:
                    raise err(f"input expects a str prompt, not {args[0].ty}", args[0])
                return STR
            case "read_file" | "file_exists" | "list_dir" | "env":
                arity(1)
                if self.expr(args[0], STR) != STR:
                    raise err(f"{name} expects a str, not {args[0].ty}", args[0])
                return {"read_file": ResultT(STR, STR), "file_exists": BOOL, "list_dir": ResultT(ListT(STR), STR),
                        "env": OptionT(STR)}[name]
            case "write_file" | "append_file":
                arity(2)
                for a in args:
                    if self.expr(a, STR) != STR:
                        raise err(f"{name} expects (path: str, text: str), not {a.ty}", a)
                return ResultT(INT, STR)
            case "eprint":
                for a in args:
                    if self.expr(a) == UNIT:
                        raise err("the expression produces no value", a)
                return UNIT
            case "sleep":
                arity(1)
                if self.expr(args[0], FLOAT) != FLOAT:
                    raise err(f"sleep expects float seconds, not {args[0].ty} (use float(x))", args[0])
                return UNIT
            case "range":
                raise err("range(...) can only be used in a `for`", e)
        raise err(f"unknown built-in function {name!r}", e)


def key_ok(t: Type) -> bool:
    """Types usable as dict keys / set elements (ordered and hashable)."""
    return t in (INT, STR, BOOL) or (isinstance(t, TupleT) and all(key_ok(x) for x in t.elems))


def sortable(t: Type) -> bool:
    """Whether a list of this type can be sorted.

    Args:
        t: The element type.

    Returns:
        True for int, float, str, bool and tuples of the orderable ones.
    """
    return t == FLOAT or key_ok(t)


def always_returns(body: list[ast.Stmt]) -> bool:
    """Whether a block always ends by returning.

    Args:
        body: The statements.

    Returns:
        True if every path through `body` ends in a `return`.
    """
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


def check_all(prog: ast.Program) -> list[ZyloError]:
    """Check a program and report every mistake in it, not only the first.

    Repeats the check until the set of methods that modify `self` stops changing (a method that calls a modifying
    method is one too); this converges in a few rounds. Each statement is checked on its own, so a mistake in one
    does not hide those in the next; a variable whose declaration failed is not reported again where it is used.

    Args:
        prog: The program, annotated in place.

    Returns:
        The mistakes, in source order; empty when the program is correct.
    """
    mutating: set[tuple[str, str]] = set()
    while True:
        c = Checker(mutating)
        c.check(prog)
        if c.mutating == mutating:
            return sorted(c.errors, key=lambda e: (e.file or "", e.line, e.col))
        mutating = c.mutating


def check(prog: ast.Program) -> ast.Program:
    """Check a program, failing on the first mistake.

    Args:
        prog: The program, annotated in place.

    Returns:
        The same program.

    Raises:
        ZyloError: On the first mistake found (use `check_all` to get every one).
    """
    errors = check_all(prog)
    if errors:
        raise errors[0]
    return prog
