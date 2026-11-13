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
import copy
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
    CellT,
    DictT,
    EnumT,
    FnT,
    ListT,
    OptionT,
    ParamT,
    ResultT,
    SetT,
    StructT,
    TupleT,
    Type,
    VarInfo,
    is_copy_type,
)
from zylo.semantics.derive import JSON_T, Deriver
from zylo.semantics.exhaustive import Pat, Space, lower
from zylo.semantics.generics import (
    Generic,
    GenericMixin,
    allows,
    has_param,
    int_literal,
    is_num,
    is_ord,
    key_ok,
    sortable,
)
from zylo.syntax.formatter import pattern_text

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
class ClosureFrame:
    """A closure being checked: where its scopes start and which outer variables it has captured so far.

    Attributes:
        base: Index in the scope stack of the closure's parameter scope; scopes below it belong to the enclosing code.
        node: The closure, whose `captures` list is filled in.
        by_outer: The inner copy of each captured outer variable, keyed by the outer variable.
    """
    base: int
    node: "ast.Lambda"
    by_outer: dict[int, VarInfo] = field(default_factory=dict)


@dataclass
class StructInfo:
    """What the checker knows about one struct.

    Attributes:
        name: The struct's name.
        defn: Its definition node.
        fields: Field types by name, in declaration order.
        methods: Method signatures by name.
        gmethods: The generic methods (`fn map[U](self, ...)`), which are templates: their copies are in `methods`.
    """
    name: str
    defn: ast.StructDef
    fields: dict[str, Type] = field(default_factory=dict)
    methods: dict[str, FnT] = field(default_factory=dict)
    gmethods: dict[str, ast.FnDef] = field(default_factory=dict)


@dataclass
class EnumInfo:
    """What the checker knows about one enum.

    Attributes:
        name: The enum's name.
        defn: Its definition node.
        variants: The types of each variant's data, by variant name, in declaration order.
        methods: Method signatures by name.
        gmethods: The generic methods, which are templates: their copies are in `methods`.
    """
    name: str
    defn: ast.EnumDef
    variants: dict[str, list[Type]] = field(default_factory=dict)
    methods: dict[str, FnT] = field(default_factory=dict)
    gmethods: dict[str, ast.FnDef] = field(default_factory=dict)


class Checker(GenericMixin):
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
        self.externs: set[str] = set()  # extern fns: they cannot be used as values
        self.field_meta: dict[int, tuple[VarInfo, str]] = {}  # narrowed field path -> (root variable, unwrap method)
        self.closures: list[ClosureFrame] = []  # the closures being checked, outermost first
        self.structs: dict[str, StructInfo] = {}
        self.enums: dict[str, EnumInfo] = {}
        self.mutating: set[tuple[str, str]] = set(mutating or ())  # (struct, method) pairs that modify self
        self.scopes = Scopes()
        self.ret: Type | None = None  # return type of the current function (None = top level)
        self.owner: str | None = None  # struct of the current method
        self.loops = 0
        self.errors: list[ZyloError] = []  # every mistake found; the checker goes on after one
        self.poisoned: set[str] = set()  # names whose declaration failed: later uses are not reported again
        # parameters (or struct fields) with their defaults, for calls: by function name, `Type.method` or struct name
        self.specs: dict[str, list] = {}
        self.deriver = Deriver(self.structs, self.enums, lambda name: name in self.fns)  # typed JSON helpers
        self.program: ast.Program | None = None  # the program being checked: generated functions are added to it
        # generics (see semantics/generics.py)
        self.generics: dict[str, Generic] = {}  # templates by name
        self.itypes: dict[str, Type] = {}  # instance name -> its type, which remembers the type arguments
        self.insts: dict[tuple, str] = {}  # (generic, type arguments) -> instance name
        self.idefs: dict[str, ast.StructDef | ast.EnumDef] = {}  # instance name -> its definition
        self.taken: dict[str, tuple] = {}  # instance names in use, to keep them unique
        self.deferred: list | None = []  # instances made before enums are resolved: finished later (None once they can be)
        self.abstract: list[tuple[Generic, ast.Node, str]] = []  # templates to check once, for every type
        self.ninst = 0  # instances made so far, for the expansion limit
        self.pending: list[tuple[ast.FnDef, str]] = []  # copies of generic methods still to check, with their owner
        self.mbroken: dict[tuple[str, str], bool] = {}  # (struct or template, generic method) -> its template has a mistake
        self.fn_insts: dict[tuple, str] = {}  # (generic function, type arguments) -> instance name
        self.minsts: list[tuple[str, str, tuple]] = []  # (struct or template, generic method, type arguments) in use
        self.in_copy = False  # checking the copy of a generic definition (its integer literals may stand for a float `T`)

    # ---- entry point ---------------------------------------------------------------------

    def check(self, prog: ast.Program) -> None:
        """Check a whole program, annotating it in place.

        Args:
            prog: The program to check.

        Raises:
            ZyloError: Never: mistakes are collected in `errors` (see `check_all`).
        """
        self.program = prog
        prog.body[:] = [s for s in prog.body if not getattr(s, "instance", False)]  # copies of generics are made again
        for s in prog.body:
            if isinstance(s, (ast.StructDef, ast.EnumDef)):
                s.methods[:] = [m for m in s.methods if not m.instance]  # and so are those of generic methods
        try:
            gens = [s for s in prog.body if getattr(s, "tparams", None)]
            defs = [s for s in prog.body if isinstance(s, ast.StructDef) and not s.tparams]
            enums = [s for s in prog.body if isinstance(s, ast.EnumDef) and not s.tparams]
            for g in gens:
                self.register_generic(g)
            for d in defs:
                self.register_struct(d)
            for en in enums:
                self.register_enum(en)
            for d in defs:
                self.resolve_struct(d)
            for en in enums:
                self.resolve_enum(en)
            for d in defs:  # field defaults may name enum variants, so they wait until enums are resolved
                self.check_defaults(d.fields, "field", [f.ty for f in d.fields])
            late, self.deferred = self.deferred, None
            for d in late:
                self.finish_instance(d)
            for g in self.generics.values():
                if g.kind != "fn":
                    self.declare_generic(g)
            self.check_cycles(defs, enums)
            for s in prog.body:  # function signatures, so they can be used before their definition
                if isinstance(s, ast.FnDef) and not s.instance and not s.tparams:
                    self.declare_fn(s)
            for d in [*defs, *enums]:
                self.declare_methods(d)
                for m in d.methods:
                    if m.tparams:  # a generic method of an ordinary struct: checked once, for every type
                        self.abstract.append((None, m, d.name))
            for g in self.generics.values():
                if g.kind == "fn":
                    self.declare_generic(g)
        except ZyloError as e:  # a broken declaration makes the rest meaningless: report it and stop
            self.errors.append(e)
            return
        self.check_abstract()
        done = 0
        while done < len(prog.body) or self.pending:  # checking can add copies of generics: check those too
            if self.pending:
                m, owner = self.pending.pop(0)
                try:
                    self.check_fn(m, owner)
                except ZyloError as e:
                    self.errors.append(e)
                continue
            s = prog.body[done]
            done += 1
            if getattr(s, "tparams", None) or (getattr(s, "origin", None) and self.generics[s.origin].broken):
                continue  # a template (checked once above, emitted never), or a copy of one that has a mistake
            try:
                if isinstance(s, ast.ExternFn):
                    self.check_extern(s)
                elif isinstance(s, ast.FnDef):
                    self.check_fn(s)
                elif isinstance(s, (ast.StructDef, ast.EnumDef)):  # enum variants were checked with the declarations
                    for m in s.methods:
                        if m.tparams or m.instance:  # a template, or a copy of one (checked from the queue)
                            continue
                        try:
                            self.check_fn(m, s.name)
                        except ZyloError as e:
                            self.errors.append(e)
                else:
                    self.stmt(s)
            except ZyloError as e:
                self.errors.append(e)
        self.note_instances()

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
        if t.fixed is not None:  # a type parameter in a copy of a generic definition
            return t.fixed
        if t.name == "list":
            return ListT(self.resolve(t.args[0]))
        if t.name == "Cell":
            inner = self.resolve(t.args[0])
            if inner == UNIT:
                raise err("a Cell cannot hold nothing", t)
            return CellT(inner)
        if t.name == "?":
            return OptionT(self.resolve(t.args[0]))
        if t.name == "fn":
            return FnT(tuple(self.resolve(a) for a in t.args), self.resolve(t.ret) if t.ret else UNIT)
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
        if t.name in self.generics:
            return self.resolve_generic(t)
        if t.args and (t.name in self.structs or t.name in self.enums):
            raise err(f"{t.name!r} is not generic, it takes no type arguments", t)
        if t.name in self.structs:
            return StructT(t.name)
        if t.name in self.enums:
            return EnumT(t.name)
        raise err(f"unknown type {t.name!r}", t)

    # ---- structs ----------------------------------------------------------------------

    def register_struct(self, d: ast.StructDef) -> None:
        """Record a struct's name, rejecting duplicates and reserved names."""
        if d.name in self.structs or d.name in self.enums:
            raise err(f"struct {d.name!r} is already defined" if d.name in self.structs else f"{d.name!r} is already an enum", d)
        if d.name in self.generics:
            raise err(f"{d.name!r} is already defined", d)
        if d.name in BUILTINS or d.name in PRIMS or d.name in ("list", "unit", "Result", "Cell"):
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
        self.specs[d.name] = d.fields

    def check_cycles(self, defs: list[ast.StructDef], enums: list[ast.EnumDef]) -> None:
        """Reject structs and enums that contain themselves directly or through others (lists are fine)."""
        def inside(t: Type) -> list[str]:  # the structs and enums stored by value in `t`
            if isinstance(t, (StructT, EnumT)):
                return [t.name]
            if isinstance(t, OptionT):
                return inside(t.inner)
            if isinstance(t, ResultT):
                return inside(t.ok) + inside(t.err)
            if isinstance(t, TupleT):
                return [n for x in t.elems for n in inside(x)]
            return []

        def parts(name: str) -> list[str]:
            if name in self.structs:
                return [n for t in self.structs[name].fields.values() for n in inside(t)]
            return [n for ts in self.enums[name].variants.values() for t in ts for n in inside(t)]

        def reaches(start: str, cur: str, seen: set[str]) -> bool:
            for n in parts(cur):
                if n == start:
                    return True
                if n not in seen:
                    seen.add(n)
                    if reaches(start, n, seen):
                        return True
            return False

        for d in [*defs, *enums]:
            if reaches(d.name, d.name, set()):
                kind = "struct" if isinstance(d, ast.StructDef) else "enum"
                raise err(f"{kind} {d.name!r} contains itself (directly or indirectly); use a list", d)

    # ---- enums -----------------------------------------------------------------------

    def register_enum(self, d: ast.EnumDef) -> None:
        """Record an enum's name, rejecting duplicates and reserved names."""
        if d.name in self.structs or d.name in self.enums:
            raise err(f"enum {d.name!r} is already defined" if d.name in self.enums else f"{d.name!r} is already a struct", d)
        if d.name in self.generics:
            raise err(f"{d.name!r} is already defined", d)
        if d.name in BUILTINS or d.name in PRIMS or d.name in ("list", "unit", "Result", "Cell"):
            raise err(f"{d.name!r} is a reserved name", d)
        self.enums[d.name] = EnumInfo(d.name, d)

    def resolve_enum(self, d: ast.EnumDef) -> None:
        """Resolve the data types of an enum's variants, rejecting empty enums and duplicate variants."""
        info = self.enums[d.name]
        if not d.variants:
            raise err(f"enum {d.name!r} needs at least one variant", d)
        for v in d.variants:
            if v.name in info.variants:
                raise err(f"duplicate variant {v.name!r} in {d.name!r}", v)
            v.tys = info.variants[v.name] = [self.resolve(t) for t in v.fields]

    def variant_of(self, e: ast.Expr) -> EnumInfo | None:
        """The enum a `Name` refers to when it is used as `Enum.Variant`, or None when it is a variable or not an enum."""
        if isinstance(e, ast.Name) and e.id in self.enums and self.scopes.lookup(e.id) is None:
            return self.enums[e.id]
        return None

    def variant_ctor(self, e: ast.Attr | ast.MethodCall, info: EnumInfo, name: str, args: list[ast.Expr] | None) -> Type:
        """Type `Enum.Variant` (no data) or `Enum.Variant(args)`."""
        tys = info.variants.get(name)
        if tys is None:
            raise err(f"enum {info.name!r} has no variant {name!r} (has: {', '.join(info.variants)})", e)
        if args is None and tys:
            raise err(f"variant {info.name}.{name} carries data; write {info.name}.{name}({', '.join(str(t) for t in tys)})", e)
        if args is not None and not tys:
            raise err(f"variant {info.name}.{name} carries no data; write {info.name}.{name} without parentheses", e)
        if args is not None:
            self.check_args(e, f"{info.name}.{name}", args, FnT(tuple(tys), self.named(info.name)))
        e.variant = (info.name, name)
        return self.named(info.name)

    def match_stmt(self, s: ast.Match) -> None:
        """Check a `match` on an enum, `int`, `str`, `bool`, `Option`, `Result` or tuple.

        Every pattern must fit the value's type and bind its data. Reachability and exhaustiveness are decided by
        `semantics/exhaustive.py`: an arm that could never run is an error, and so is a `match` that leaves some value
        unmatched (guarded arms never count).
        """
        t = self.expr(s.value)
        if isinstance(t, EnumT):
            s.enum = t.name
        elif t == FLOAT:
            raise err("`match` cannot be used on a float: floats are not matched exactly; use `if` and comparisons", s.value)
        elif not isinstance(t, (OptionT, ResultT, TupleT)) and t not in (INT, STR, BOOL):
            raise err(f"`match` needs an enum, int, str, bool, Option, Result or tuple value, not {t}", s.value)
        space = Space(lambda ty: self.enums[ty.name].variants)
        rows: list[list[Pat]] = []
        before = len(self.errors)
        for arm in s.arms:
            try:
                self.check_arm(arm, t, space, rows)
            except ZyloError as ex:
                self.errors.append(ex)
        if len(self.errors) > before:  # a broken arm would only add misleading coverage errors
            return
        uncovered = space.missing(rows, [t])
        if uncovered is None:
            return
        shown = [w[0] for w in uncovered]
        ranged = any(n.kind == "range" for arm in s.arms for n in self.pattern_nodes(arm.pattern))
        if t in (INT, STR) and not ranged:
            raise err(f"`match` on {t} needs a final `_` arm (without a guard) for every other value", s)
        names = ", ".join(shown[:5]) + (", ..." if len(shown) > 5 else "")
        raise err(f"`match` on {t} does not cover {names}; add arms or a final `_` arm; an arm with a guard does not count", s)

    @staticmethod
    def pattern_nodes(p: ast.Pattern):
        """The pattern and every pattern inside it."""
        yield p
        for sub in p.subs:
            yield from Checker.pattern_nodes(sub)

    def check_arm(self, arm: ast.MatchArm, t: Type, space: Space, rows: list[list[Pat]]) -> None:
        """Check one arm: its pattern against the matched value's type, its reachability, then guard and body.

        The arm's patterns are added to `rows` (what the next arms are compared with) unless it has a guard, because
        the guard may reject the value.
        """
        binds: dict[str, Type] = {}
        self.check_pattern(arm.pattern, t, binds, top=True)
        alts = arm.pattern.subs if arm.pattern.kind == "or" else [arm.pattern]
        local: list[list[Pat]] = []
        dead: ZyloError | None = None
        for alt in alts:
            row = [lower(alt)]
            if dead is None and not space.useful(rows + local, row, [t]):
                noun = "alternative" if len(alts) > 1 else "arm"
                if row in rows + local:
                    dead = err(f"`{pattern_text(alt)}` is matched twice: an earlier pattern is the same", alt)
                else:
                    dead = err(f"this {noun} can never run: the patterns before it already match every value it matches", alt)
            local.append(row)
        arm.binds = list(binds)
        self.arm_body(arm, list(binds.values()))
        if dead is not None:
            raise dead
        if arm.guard is None:
            rows.extend(local)

    def check_pattern(self, p: ast.Pattern, t: Type, binds: dict[str, Type], top: bool = False) -> None:
        """Check a pattern against the type of the value it matches, collecting the names it binds.

        Args:
            p: The pattern; each node gets the type it matches in `ty`.
            t: The type of the value.
            binds: The names bound so far, with their types, in order; this pattern adds its own.
            top: Whether this is the whole pattern of an arm (it only changes the wording of some errors).
        """
        p.ty = t
        match p.kind:
            case "wild":
                pass
            case "bind":
                if p.name in binds:
                    raise err("a pattern cannot bind the same name twice", p)
                if isinstance(t, EnumT) and p.name in self.enums[t.name].variants:
                    raise err(f"`{p.name}` is a variant of {t}: write `{t}.{p.name}` to match it, or choose another name", p)
                binds[p.name] = t
            case "lit":
                self.literal_pattern(p, t, top)
            case "range":
                self.range_pattern(p, t)
            case "or":
                first: dict[str, Type] | None = None
                for alt in p.subs:
                    if alt.kind == "wild":
                        raise err("`_` cannot be part of an or-pattern: it already matches every value", alt)
                    own: dict[str, Type] = {}
                    self.check_pattern(alt, t, own, top)
                    if first is None:
                        first = own
                    elif own != first:
                        raise err("every alternative of an or-pattern must bind the same names with the same types", alt)
                for name, ty in (first or {}).items():
                    if name in binds:
                        raise err("a pattern cannot bind the same name twice", p)
                    binds[name] = ty
            case "tuple":
                if not isinstance(t, TupleT):
                    raise err(f"a tuple pattern cannot match {t}", p)
                if len(p.subs) != len(t.elems):
                    raise err(f"this tuple pattern has {len(p.subs)} elements, but the value has {len(t.elems)}", p)
                for sub, ty in zip(p.subs, t.elems):
                    self.check_pattern(sub, ty, binds)
            case "enum":
                self.enum_pattern(p, t, binds)
            case _:  # some / none / ok / err
                if p.kind in ("some", "none"):
                    if not isinstance(t, OptionT):
                        raise err(f"`{p.kind}` can only match an Option, not {t}", p)
                    inner = [t.inner] if p.kind == "some" else []
                else:
                    if not isinstance(t, ResultT):
                        raise err(f"`{p.kind}` can only match a Result, not {t}", p)
                    inner = [t.ok if p.kind == "ok" else t.err]
                for sub, ty in zip(p.subs, inner):
                    self.check_pattern(sub, ty, binds)

    def enum_pattern(self, p: ast.Pattern, t: Type, binds: dict[str, Type]) -> None:
        """Check `Enum.Variant(p, ...)` against the matched type."""
        if not isinstance(t, EnumT):
            raise err(f"`{p.type.name}.{p.name}` is a variant pattern, but the value is {t}", p)
        info = self.enums[t.name]
        if p.type.name in self.generics and not p.type.args:  # `Opt.Some(x)`: the type arguments come from the value
            if t.base != p.type.name:
                raise err(f"this `match` is on {t}, not {p.type.name.rsplit(chr(183), 1)[-1]}", p)
            named = t
        else:
            named = self.resolve(p.type)
        if named != t:
            raise err(f"this `match` is on {t}, not {named}", p)
        tys = info.variants.get(p.name)
        if tys is None:
            raise err(f"enum {t} has no variant {p.name!r} (has: {', '.join(info.variants)})", p)
        if len(p.subs) != len(tys):
            want = f"{len(tys)} pattern(s) in parentheses" if tys else "no parentheses"
            raise err(f"{t}.{p.name} carries {len(tys)} value(s): write {want}", p)
        p.enum = t.name
        for sub, ty in zip(p.subs, tys):
            self.check_pattern(sub, ty, binds)

    def literal_pattern(self, p: ast.Pattern, t: Type, top: bool) -> None:
        """Check a literal pattern against the matched type."""
        lit = p.literal
        if isinstance(lit, ast.Float) or (isinstance(lit, ast.UnaryOp) and isinstance(lit.operand, ast.Float)):
            raise err("floats cannot be matched; use `if` and comparisons", p)
        if isinstance(lit, ast.UnaryOp) and not (lit.op == "-" and isinstance(lit.operand, ast.Int)):
            raise err("a `match` pattern must be a literal such as `3`, `-1`, `\"a\"` or `true`", p)
        lt = self.expr(lit)
        if lt != t:
            where = "this `match` is on" if top else "the value here is"
            raise err(f"{where} {t}, but the pattern is {lt}", p)

    def range_pattern(self, p: ast.Pattern, t: Type) -> None:
        """Check an integer range pattern: an `int` value, integer literal ends, and at least one value in it."""
        for end in (p.lo, p.hi):
            if end is None:
                continue
            if isinstance(end, ast.UnaryOp) and not (end.op == "-" and isinstance(end.operand, ast.Int)) \
                    or not isinstance(end, (ast.Int, ast.UnaryOp)):
                raise err("the ends of a range pattern must be integer literals such as `1` or `-5`", p)
            self.expr(end)
        if t != INT:
            raise err(f"a range pattern matches an int, but the value is {t}", p)
        lo, hi = ast.range_bounds(p)
        if lo > hi:
            raise err("this range matches no value: its start is not below its end", p)

    def arm_body(self, arm: ast.MatchArm, tys: list[Type]) -> None:
        """Check an arm's body with the names of its pattern (`arm.binds`) bound to `tys`."""
        self.scopes.push()
        try:
            arm.vars = [self.scopes.declare(b, ty) for b, ty in zip(arm.binds, tys)]
            if arm.guard is not None:
                self.expect_bool(arm.guard, "a `match` guard")
            for st in arm.body:
                self.stmt(st)
        finally:
            self.scopes.pop()

    def declare_methods(self, d: ast.StructDef | ast.EnumDef) -> None:
        """Record the signatures of the methods of a struct or enum, rejecting duplicates and clashes."""
        info = self.structs[d.name] if isinstance(d, ast.StructDef) else self.enums[d.name]
        clash = info.fields if isinstance(d, ast.StructDef) else info.variants
        what = "field" if isinstance(d, ast.StructDef) else "variant"
        for m in d.methods:
            if not m.has_self:
                raise err(f"method {m.name!r} must take `self` as its first parameter", m)
            if m.name in clash:
                raise err(f"{m.name!r} is both a {what} and a method of {d.name!r}", m)
            if m.name in info.methods or m.name in info.gmethods:
                raise err(f"method {m.name!r} is already defined in {d.name!r}", m)
            if m.tparams:
                self.declare_generic_method(d, info, m)
                continue
            m.ty = info.methods[m.name] = self.signature(m)
            self.specs[f"{d.name}.{m.name}"] = m.params

    def is_constant(self, e: ast.Expr) -> bool:
        """Whether an expression may be a default value.

        Allowed: a literal, `none`, an empty list or dict, an enum variant (with constant data), or a tuple of those.
        """
        if isinstance(e, (ast.Int, ast.Float, ast.Str, ast.Bool, ast.NoneLit)):
            return True
        if isinstance(e, ast.UnaryOp):
            return e.op == "-" and isinstance(e.operand, (ast.Int, ast.Float))
        if isinstance(e, ast.ListLit):
            return not e.items
        if isinstance(e, ast.DictLit):
            return not e.keys
        if isinstance(e, ast.TupleLit):
            return all(self.is_constant(x) for x in e.items)
        if isinstance(e, ast.Attr):
            return self.variant_of(e.value) is not None
        if isinstance(e, ast.MethodCall):
            return self.variant_of(e.value) is not None and not e.named and all(self.is_constant(a) for a in e.args)
        return False

    def check_defaults(self, items: list, what: str, types: list[Type]) -> None:
        """Check the default values of parameters or struct fields.

        A default must be a constant of the right type, and an item without a default cannot follow one with a default.

        Args:
            items: The parameters or fields (anything with `name` and `default`).
            what: "parameter" or "field", for messages.
            types: The resolved type of each item.

        Raises:
            ZyloError: On a non-constant default, a mismatched type or a required item after an optional one.
        """
        previous: str | None = None
        for item, ty in zip(items, types):
            if item.default is None:
                if previous is not None:
                    raise err(f"{what} {item.name!r} has no default but follows {previous!r}, which has one", item)
                continue
            previous = previous or item.name
            if not self.is_constant(item.default):
                raise err("a default value must be a constant: a number, string, true/false, none, an empty list or dict, "
                          "or a tuple of those", item.default)
            t = self.expr(item.default, ty)
            if t != ty:
                raise err(f"the default of {what} {item.name!r} is {t} but it is declared {ty}", item.default)

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
        params = tuple(self.resolve(p.type) for p in f.params)
        self.check_defaults(f.params, "parameter", list(params))
        return FnT(params, ret)

    def declare_fn(self, f: ast.FnDef) -> None:
        """Record a top-level function (or `extern fn`) so it can be called before it is defined."""
        if f.name in BUILTINS - SHADOWABLE:
            raise err(f"{f.name!r} is a built-in function", f)
        if f.name in self.structs:
            raise err(f"{f.name!r} is already a struct", f)
        if f.name in self.fns or f.name in self.generics:
            raise err(f"function {f.name!r} is already defined", f)
        f.ty = self.fns[f.name] = self.signature(f)
        self.specs[f.name] = f.params
        if isinstance(f, ast.ExternFn):
            self.externs.add(f.name)

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
            owner: The struct's or enum's name when `f` is a method.

        Raises:
            ZyloError: On a type error in the body, or when a function with a return type can finish without returning.
        """
        sig = f.ty
        top, saved_poison = self.scopes, self.poisoned
        self.scopes, self.ret, self.loops, self.owner, self.poisoned = Scopes(), sig.ret, 0, owner, set()
        self.closures = []
        saved_copy = self.in_copy
        owner_t = self.itypes.get(owner) if owner else None
        self.in_copy = f.instance or (owner_t is not None and not has_param(owner_t))
        try:
            self_info = self.scopes.declare("self", self.named(owner)) if owner else None
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
            self.lost_mutation(f.params, f)
            for p in f.params:
                p.info.by_ref = not p.info.mutable and not is_copy_type(p.info.ty) and not f.self_mut
        finally:
            self.scopes, self.ret, self.owner, self.poisoned = top, None, None, saved_poison
            self.in_copy = saved_copy

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
        """Work out what an `is` check tells about a variable or a field path.

        Args:
            cond: The condition of an `if`.

        Returns:
            For `x is K` on a plain variable or a field path (`p.name`, `self.a.b`), `(name or dotted path, type of the
            value in the then-branch, type in the else-branch)`, where a type of None means no information; None for any
            other condition.
        """
        if not isinstance(cond, ast.IsCheck):
            return None
        key = field_path(cond.value)
        if key is None or (isinstance(cond.value, ast.Attr) and cond.value.variant):
            return None
        t, k = cond.value.ty, cond.kind
        if isinstance(t, OptionT):
            then_t, else_t = (t.inner, None) if k == "some" else (None, t.inner)
        else:
            then_t, else_t = (t.ok, t.err) if k == "ok" else (t.err, t.ok)
        return key, then_t, else_t

    def narrow_field(self, cond: ast.IsCheck, key: str, ty: Type | None, ok_side: bool) -> None:
        """Narrow a field path such as `p.name` in the current scope (see `narrowing`).

        The narrowing is recorded as a pseudo variable named by the dotted path, so it ends with the scope. Reads of that
        path then have the unwrapped type; any write to the path, to a prefix of it or a mutating call on a prefix is an
        error while it is in force.

        Args:
            cond: The `is` check.
            key: The dotted path.
            ty: The narrowed type, or None for no narrowing.
            ok_side: True when this scope is the side where `cond.kind` holds.
        """
        if ty is None:
            return
        root = self.scopes.lookup(key.split(".")[0])
        if root is None:
            return
        want_ok = isinstance(cond.value.ty, OptionT) or (cond.kind == "ok") == ok_side
        extract = "unwrap" if want_ok else "unwrap_err"
        info = self.scopes.declare(key, ty)
        info.narrowed = True
        self.field_meta[id(info)] = (root, extract)

    def field_conflict(self, place: ast.Expr) -> None:
        """Reject a write to a place that overlaps a narrowed field path.

        Args:
            place: The place being assigned or mutated (`p`, `p.name`, `p.items[0]`...).

        Raises:
            ZyloError: If the place is, is inside, or contains a narrowed field path.
        """
        path, e = None, place
        while path is None and isinstance(e, (ast.Attr, ast.Index, ast.Name)):
            path = field_path(e)
            e = e.value if isinstance(e, (ast.Attr, ast.Index)) else None
        if path is None:
            return
        paths = [path]
        keys = [k for scope in self.scopes.stack for k in scope if "." in k]
        for key in keys:
            if any(key == p or key.startswith(p + ".") or p.startswith(key + ".") for p in paths):
                raise err(f"{key!r} is narrowed by an `is` check here and cannot be reassigned or modified", place)

    def if_stmt(self, s: ast.If) -> None:
        """Check an `if`, narrowing `Option`/`Result` variables and fields tested with `is` inside its branches."""
        self.expect_bool(s.cond, "the `if` condition")
        nar = self.narrowing(s.cond)
        name, then_t, else_t = nar if nar else (None, None, None)
        if nar and "." in name:
            for body, t, side in ((s.then, then_t, True), (s.else_, else_t, False)):
                self.scopes.push()
                self.narrow_field(s.cond, name, t, side)
                for st in body:
                    self.stmt(st)
                self.scopes.pop()
            if not s.else_ and else_t is not None and exits(s.then):
                # early exit: `if p.x is none: return ...` narrows the field for the rest of the enclosing block
                self.narrow_field(s.cond, name, else_t, False)
            return
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
            case ast.Pass():
                pass
            case ast.Break() | ast.Continue():
                if not self.loops:
                    kw = "break" if isinstance(s, ast.Break) else "continue"
                    raise err(f"`{kw}` outside a loop", s)
            case ast.FnDef():
                raise err("functions can only be defined at the top level", s)
            case ast.StructDef():
                raise err("structs can only be defined at the top level", s)
            case ast.EnumDef():
                raise err("enums can only be defined at the top level", s)
            case ast.Match():
                self.match_stmt(s)
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
            existing = self.lookup_var(name, target)
            if existing is not None and existing.captured:
                raise self.captured_error(name, target)
            if existing is None:
                s.declares.append(True)
                s.vars.append(self.scopes.declare(name, et))
            else:
                self.field_conflict(target)
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
            self.field_conflict(target)
            ft = self.expr(target)
            vt = self.expr(s.value, ft)
            if vt != ft:
                raise err(f"field {target.name!r} is {ft} but {vt} is assigned", s.value)
            self.mark_mutable(target)
            return
        if target.id == "self":
            raise err("`self` cannot be declared or reassigned" if self.owner is None
                      else "enum values are immutable: return a new value instead of reassigning `self`" if self.owner in self.enums
                      else "cannot reassign `self`; modify its fields (`self.x = ...`)", s)
        existing = self.lookup_var(target.id, target)
        if existing is not None and existing.captured:
            raise self.captured_error(target.id, target)
        if s.annotation:
            if existing:
                raise err(f"{target.id!r} is already declared", s)
            declared = self.resolve(s.annotation)
            vt = self.expr(s.value, declared)
            if vt != declared:
                raise err(f"{target.id!r} is {declared} but {vt} is assigned", s.value)
            s.declares, s.var = True, self.scopes.declare(target.id, declared)
        elif existing:
            self.field_conflict(target)
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
        self.field_conflict(e)
        while isinstance(e, (ast.Index, ast.Attr)):
            e = e.value
        if isinstance(e, ast.Name) and (info := self.lookup_var(e.id, e)):
            if info.captured:
                raise self.captured_error(e.id, e)
            info.mutable = True
            info.place_mut = True
            info.mut_roots.add(id(e))

    def lost_mutation(self, params: list[ast.Param], node: ast.Node) -> None:
        """Reject a parameter whose in-place changes nothing can observe.

        Arguments are passed by value, so a function that modifies a part of a parameter (`a.names[k] = v`,
        `append(xs, 1)`, `p.bump()`) only changes its own copy. That is fine when the function then uses the copy (reads
        it, returns it, passes it on); when it never does, the change is silently lost, which is almost certainly a
        mistake.

        Args:
            params: The parameters of the function or closure just checked.
            node: The function, for the location of the error when a parameter has none.

        Raises:
            ZyloError: On a modified parameter that the function never uses afterwards.
        """
        for p in params:
            info = p.info
            if info is not None and info.place_mut and not is_copy_type(info.ty) and info.uses <= info.mut_roots:
                raise err(f"parameter {p.name!r} is modified but the changes are not visible to the caller: arguments are "
                          f"passed by value; return it (`-> {info.ty}`), make the function a method of its struct, or share "
                          f"the state with a `Cell`", p if p.line else node)

    def captured_error(self, name: str, node: ast.Node) -> ZyloError:
        """The error for modifying a variable that a closure captured."""
        return err(f"cannot modify {name!r} inside a function that captured it: closures capture a copy of the variables "
                   f"they use, so the change would be lost; share state with a `Cell` (`c = cell(0)`, then `c.set(...)`) or return the new value", node)

    def lookup_var(self, name: str, node: ast.Node) -> VarInfo | None:
        """Find a visible variable, turning it into a captured copy when it lives outside the closures being checked.

        Args:
            name: The variable's name.
            node: Where it is used, for errors.

        Returns:
            The variable as seen from the current code: the variable itself, or the read-only copy a closure holds of it.

        Raises:
            ZyloError: If a closure tries to capture `self`.
        """
        found, info = -1, None
        for i in range(len(self.scopes.stack) - 1, -1, -1):
            if name in self.scopes.stack[i]:
                found, info = i, self.scopes.stack[i][name]
                break
        if info is None:
            return None
        info.uses.add(id(node))
        for frame in self.closures:  # outermost first, so a nested closure copies the outer closure's copy
            if frame.base > found:
                if name == "self":
                    raise err("a function cannot capture `self`; copy what it needs into a local first (`size = self.size`)", node)
                inner = frame.by_outer.get(id(info))
                if inner is None:
                    inner = VarInfo(info.name, info.ty, narrowed=info.narrowed, captured=True)
                    frame.by_outer[id(info)] = inner
                    frame.node.captures.append((info, inner))
                    self.scopes.stack[frame.base][name] = inner
                info, found = inner, frame.base
        return info

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
                if isinstance(expected, ParamT) and allows(expected, "Num"):
                    return expected  # `a + 1` in a generic: the literal is a `T`
                if expected == FLOAT and self.in_copy:
                    return FLOAT  # ... and in the copy for `float` it is written `1.0`
                return INT
            case ast.Float():
                return FLOAT
            case ast.Str():
                return STR
            case ast.Bool():
                return BOOL
            case ast.Name(id):
                info = self.lookup_var(id, e)
                if info is None:
                    if id in self.structs:
                        raise err(f"{id!r} is a struct; build it with parentheses: {id}(...)", e)
                    if id in self.enums:
                        raise err(f"{id!r} is an enum; name one of its variants: {id}.Variant", e)
                    if id in self.generics and self.generics[id].kind == "fn":
                        return self.fn_value(e, expected)
                    if e.targs:
                        raise err(f"{id!r} is not a generic function, so it takes no type arguments", e)
                    if id in self.generics:
                        raise err(f"{id!r} is generic; write {id}[...] (a type) or {id}.Variant(...) (an enum)", e)
                    if id in self.fns and id in self.externs:
                        raise err(f"{id!r} is an `extern fn` and cannot be used as a value; wrap it: fn(...) => {id}(...)", e)
                    if id in self.fns:
                        return self.fns[id]  # a top-level function used as a value
                    if id in BUILTINS:
                        raise err(f"{id!r} is a built-in function; call it, or wrap it: fn(x) => {id}(x)", e)
                    if id in self.poisoned:
                        raise _Poisoned
                    raise err(f"undefined variable {id!r}", e)
                if e.targs:
                    raise err(f"{id!r} is a variable, so it takes no type arguments", e)
                e.var = info
                return info.ty
            case ast.UnaryOp(op, x):
                return self.unary(e, op, x, expected)
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
                return self.method_call(e, expected)
            case ast.Attr(v, name):
                return self.attr(e, v, name, expected)
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
            case ast.Lambda():
                return self.lambda_expr(e, expected)
            case ast.CallValue(callee, args):
                ft = self.expr(callee)
                if not isinstance(ft, FnT):
                    raise err(f"cannot call a value of type {ft}", e)
                self.check_args(e, "the function", args, ft)
                return ft.ret
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

    def unary(self, e: ast.UnaryOp, op: str, x: ast.Expr, expected: Type | None = None) -> Type:
        """Type a prefix `-` or `not`."""
        t = self.expr(x, expected if op == "-" else None)
        if op == "not":
            if t != BOOL:
                raise err(f"`not` expects bool, not {t}", e)
            return BOOL
        if not is_num(t):
            raise err(f"`-` expects int or float, not {t}" + needs("Num", t), e)
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
            if self.contains_fn(lt):
                raise err(f"cannot use `{op}` with {lt}: functions cannot be compared", e)
            return BOOL
        if isinstance(left, ast.NoneLit):  # `none == x`: the other side gives the type
            rt = self.expr(right)
            lt = self.expr(left, rt)
        elif int_literal(left) and not int_literal(right):  # `1 + a`: the other side gives the literal its type
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
            if isinstance(lt, ParamT) or isinstance(rt, ParamT):
                hint = " (a value of a type parameter can only be combined with values of that same type)"
                lit = right if isinstance(lt, ParamT) else left
                if int_literal(lit):
                    hint = " (an integer literal can only be combined with a type parameter bounded by `Num`: `T: Num`)"
                elif isinstance(lit, ast.Float):
                    hint = " (a float literal is a float but the type parameter may be int; compute in float: float(a) * 1.5)"
            raise err(f"`{op}` between {lt} and {rt}: types must match{hint}", e)
        if op in ARITH:
            if is_num(lt) or (op == "+" and lt == STR):
                return lt
            raise err(f"`{op}` is not defined for {lt}" + needs("Num", lt), e)
        if op in ORDER:
            if is_ord(lt):
                return BOOL
            raise err(f"`{op}` is not defined for {lt}" + needs("Ord", lt), e)
        if op in EQ:
            if self.contains_fn(lt):
                raise err(f"cannot compare {lt} with `{op}`: " + ("a type parameter needs the `Eq` bound" if isinstance(lt, ParamT)
                                                                   else "functions cannot be compared"), e)
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

    def attr(self, e: ast.Attr, v: ast.Expr, name: str, expected: Type | None = None) -> Type:
        """Type a field access `value.name`, or a variant without data `Enum.Variant`."""
        e.unwrap = None
        generic = self.generic_enum_of(v)
        if generic:
            return self.generic_variant(e, generic, name, None, expected)
        enum = self.variant_of(v)
        if enum:
            return self.variant_ctor(e, enum, name, None)
        t = self.expr(v)
        info = self.struct_of(t, e, "fields")
        ft = info.fields.get(name)
        if ft is None:
            have = ", ".join(info.fields)
            hint = f" (has: {have})" if info.fields else ""
            if name in info.methods:
                raise err(f"{name!r} is a method of {t}; call it with parentheses: .{name}(...)", e)
            raise err(f"{t} has no field {name!r}{hint}", e)
        if (key := field_path(e)) is not None and (info := self.scopes.lookup(key)) is not None:
            meta = self.field_meta.get(id(info))
            if meta and self.scopes.lookup(key.split(".")[0]) is meta[0]:
                e.unwrap = meta[1]
                return info.ty
        return ft

    SPEC_RE = re.compile(r"^(?:[<>^])?0?\d*(?:\.\d+)?([fdsxXb]?)$")

    def format_part(self, p: ast.FormatPart) -> None:
        """Check one `{expression:spec}` field of an f-string: a printable value and a valid specification."""
        t = self.printable(p.value)
        m = self.SPEC_RE.match(p.spec)
        if not m:
            raise err(f"unsupported format specification {p.spec!r} (use e.g. `:.2f`, `:>8`, `:05d`)", p)
        kind = m.group(1)
        want = {"f": FLOAT, "d": INT, "x": INT, "X": INT, "b": INT, "s": STR}.get(kind)
        if want is not None and t != want:
            raise err(f"format type `{kind}` needs {want}, not {t}", p)

    def printable(self, e: ast.Expr) -> Type:
        """Check an expression that is about to be printed: it needs a value, and functions cannot be shown."""
        t = self.expr(e)
        if t == UNIT:
            raise err("the expression produces no value", e)
        if self.contains_fn(t, need="Show"):
            why = "a type parameter needs the `Show` bound to be printed" if isinstance(t, ParamT) else "functions cannot be printed"
            raise err(f"cannot print {t}: {why}", e)
        return t

    def contains_fn(self, t: Type, seen: frozenset[str] = frozenset(), need: str = "Eq") -> bool:
        """Whether a value of this type holds a function somewhere (functions can be neither compared nor printed).

        A type parameter counts as holding one unless its bounds include `need` (`Eq` to compare, `Show` to print).
        """
        if isinstance(t, FnT):
            return True
        if isinstance(t, ParamT):
            return not allows(t, need)
        if isinstance(t, (ListT, SetT)):
            return self.contains_fn(t.elem, seen, need)
        if isinstance(t, (OptionT, CellT)):
            return self.contains_fn(t.inner, seen, need)
        if isinstance(t, ResultT):
            return self.contains_fn(t.ok, seen, need) or self.contains_fn(t.err, seen, need)
        if isinstance(t, DictT):
            return self.contains_fn(t.val, seen, need)
        if isinstance(t, TupleT):
            return any(self.contains_fn(x, seen, need) for x in t.elems)
        if isinstance(t, (StructT, EnumT)) and t.name not in seen:
            seen = seen | {t.name}
            if isinstance(t, StructT):
                return any(self.contains_fn(x, seen, need) for x in self.structs[t.name].fields.values())
            return any(self.contains_fn(x, seen, need) for ts in self.enums[t.name].variants.values() for x in ts)
        return False

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

        def no_fn(t: Type) -> None:
            if self.contains_fn(t):
                raise err(f"{n}() compares elements, and functions cannot be compared", e)

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
                no_fn(el)
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
                no_fn(el)
                return OptionT(INT)
            if n == "count":
                arity(1)
                arg(0, el)
                no_fn(el)
                return INT
            if n == "map":
                arity(1)
                ft = self.fn_arg(args[0], [el])
                if ft.ret == UNIT:
                    raise err("the function given to map must return a value", args[0])
                return ListT(ft.ret)
            if n == "filter":
                arity(1)
                self.fn_arg(args[0], [el], BOOL)
                return rt
            if n in ("any", "all"):
                arity(1)
                self.fn_arg(args[0], [el], BOOL)
                return BOOL
            if n == "find":
                arity(1)
                if isinstance(el, OptionT):
                    raise err("find() on a list of Option values is not supported", e)
                self.fn_arg(args[0], [el], BOOL)
                return OptionT(el)
            if n == "fold":
                arity(2)
                acc = self.expr(args[0])
                if acc == UNIT:
                    raise err("the expression produces no value", args[0])
                self.fn_arg(args[1], [acc, el], acc)
                return acc
            if n == "reduce":
                arity(1)
                if isinstance(el, OptionT):
                    raise err("reduce() on a list of Option values is not supported", e)
                self.fn_arg(args[0], [el, el], el)
                return OptionT(el)
            if n == "sort_by":
                arity(1)
                ft = self.fn_arg(args[0], [el])
                if not sortable(ft.ret):
                    raise err(f"sort_by needs a key function that returns int, float, str, bool or a tuple of those, not {ft.ret}", args[0])
                mutates()
                return UNIT
            have = ("append, pop, insert, remove, clear, reverse, sort, extend, index, count, map, filter, fold, reduce, "
                    "sort_by, any, all, find")
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
        elif isinstance(rt, CellT):
            if n == "get":
                arity(0)
                return rt.inner
            if n == "set":
                arity(1)
                arg(0, rt.inner)
                return UNIT
            if n == "update":
                arity(1)
                self.fn_arg(args[0], [rt.inner], rt.inner)
                return UNIT
            have = "get, set, update"
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

    def is_narrowed(self, e: ast.Expr) -> bool:
        """Whether an expression is a variable or field path an `is` check has narrowed in the current scope."""
        key = field_path(e)
        info = self.scopes.lookup(key) if key else None
        return info is not None and info.narrowed

    def method_call(self, e: ast.MethodCall, expected: Type | None = None) -> Type:
        """Type a method call on a struct, a string, a list, a dict, a set, an Option or a Result, or `Enum.Variant(...)`."""
        generic = self.generic_enum_of(e.value)
        if generic:
            return self.generic_variant(e, generic, e.name, e.args, expected)
        enum = self.variant_of(e.value)
        if enum:
            return self.variant_ctor(e, enum, e.name, e.args)
        rt = self.expr(e.value)
        if e.name in ("unwrap", "unwrap_or", "unwrap_err") and not isinstance(rt, (OptionT, ResultT)) and self.is_narrowed(e.value):
            what = "an Option" if e.name != "unwrap_err" else "a Result"
            call = "(...)" if e.name == "unwrap_or" else "()"
            raise err(f"already narrowed: the `is` check has already unwrapped {what} here, so {field_path(e.value)!r} is "
                      f"{rt}; drop `.{e.name}{call}`", e)
        if e.named and (isinstance(rt, (OptionT, ResultT, ListT, DictT, SetT, CellT)) or rt == STR):
            raise err(f"{rt}.{e.name} takes positional arguments only", e.named[0])
        if isinstance(rt, (OptionT, ResultT)):
            return self.builtin_method(e, rt)
        if rt == STR or isinstance(rt, (ListT, DictT, SetT, CellT)):
            return self.type_method(e, rt)
        info = self.enums[rt.name] if isinstance(rt, EnumT) else self.struct_of(rt, e, "methods")
        sig = info.methods.get(e.name)
        if sig is None and e.name in info.gmethods:
            return self.generic_method_call(e, rt, info, expected)
        if e.targs and sig is not None:
            raise err(f"method {e.name!r} is not generic, so it takes no type arguments", e)
        e.inst = None
        if sig is None:
            ft = info.fields.get(e.name) if isinstance(rt, StructT) else None
            if isinstance(ft, FnT):  # `self.handler(req)`: calling a field that holds a function
                self.check_args(e, f"{rt}.{e.name}", e.args, ft)
                e.field_call = True
                return ft.ret
            if isinstance(rt, StructT) and e.name in info.fields:
                raise err(f"{e.name!r} is a field of {rt}, not a method", e)
            have = ", ".join(info.methods)
            hint = f" (has: {have})" if info.methods else ""
            raise err(f"{rt} has no method {e.name!r}{hint}", e)
        self.check_args(e, f"{rt}.{e.name}", e.args, sig, self.specs.get(f"{rt.name}.{e.name}"))
        if (rt.name, e.name) in self.mutating:
            self.mark_mutable(e.value)
        return sig.ret

    def ctor(self, e: ast.Call) -> Type:
        """Type a struct constructor call: arguments (positional, then named) cover every field without a default."""
        info = self.structs[e.func]
        types = list(info.fields.values())
        self.check_args(e, e.func, e.args, FnT(tuple(types), StructT(e.func)), self.specs.get(e.func))
        e.ctor_fields = list(info.fields)
        return StructT(e.func)

    # ---- functions as values ---------------------------------------------------------------

    def fn_arg(self, a: ast.Expr, params: list[Type], ret: Type | None = None) -> FnT:
        """Check an argument that must be a function with the given parameter types.

        Args:
            a: The argument. An anonymous function may leave its parameter types out: they come from `params`.
            params: The parameter types the function must take.
            ret: The return type it must have, or None to accept (and report) any.

        Returns:
            The argument's function type.

        Raises:
            ZyloError: If the argument is not a function of that shape.
        """
        if self.generic_fn_ref(a) and not a.targs:  # a generic function passed as is: its type arguments follow from `params`
            t = a.ty = self.fn_value(a, None, params, ret)
        else:
            t = self.lambda_expr(a, None, params, ret) if isinstance(a, ast.Lambda) else self.expr(a)
        if isinstance(a, ast.Lambda):
            a.ty = t
        want = FnT(tuple(params), ret if ret is not None else (t.ret if isinstance(t, FnT) else UNIT))
        if t != want:
            raise err(f"expected a function {want}, got {t}", a)
        return t

    def lambda_expr(self, e: ast.Lambda, expected: Type | None, params: list[Type] | None = None,
                    ret: Type | None = None) -> Type:
        """Type an anonymous function (or a local `fn`) and work out what it captures.

        Args:
            e: The function.
            expected: The type the context wants, if any; it fills in omitted parameter types.
            params: Parameter types the context requires (for the arguments of built-in methods).
            ret: A return type the context requires, used as a hint for the body.

        Returns:
            The function's type.

        Raises:
            ZyloError: On a missing or mismatched parameter type, or a body that does not fit its return type.
        """
        if params is None and isinstance(expected, FnT):
            params, ret = list(expected.params), expected.ret
        if params is not None and len(params) != len(e.params):
            raise err(f"this function takes {len(e.params)} parameter(s) but {len(params)} are expected here", e)
        types: list[Type] = []
        seen: set[str] = set()
        for i, p in enumerate(e.params):
            if p.name in seen:
                raise err(f"duplicate parameter {p.name!r}", p)
            seen.add(p.name)
            if p.type is not None:
                pt = self.resolve(p.type)
                if params is not None and params[i] != pt:
                    raise err(f"parameter {p.name!r} is {pt} but {params[i]} is expected here", p)
            elif params is not None:
                pt = params[i]
            else:
                raise err(f"parameter {p.name!r} needs a type (`{p.name}: int`): nothing here tells which one it is", p)
            types.append(pt)
        declared = self.resolve(e.ret) if e.ret else None
        if declared is None and e.expr is None:
            declared = UNIT  # a block body returns nothing unless `-> T` says so
        want = declared if declared is not None else ret
        e.captures = []
        saved = (self.ret, self.loops)
        self.scopes.push()
        self.closures.append(ClosureFrame(len(self.scopes.stack) - 1, e))
        try:
            for p, pt in zip(e.params, types):
                p.info = self.scopes.declare(p.name, pt)
            self.ret, self.loops = want, 0
            if e.expr is not None:
                got = self.expr(e.expr, want)
                if declared is not None and got != declared:
                    raise err(f"the function declares -> {declared} but its body is {got}", e.expr)
            else:
                before = len(self.errors)
                for s in e.body:
                    self.stmt(s)
                got = declared
                if len(self.errors) == before and got != UNIT and not always_returns(e.body):
                    raise err(f"function {e.name or '(anonymous)'!r} can finish without a `return`", e)
                self.lost_mutation(e.params, e)
        finally:
            self.closures.pop()
            self.scopes.pop()
            self.ret, self.loops = saved
        return FnT(tuple(types), got)

    # ---- calls ------------------------------------------------------------------

    def check_args(self, e: ast.Node, name: str, args: list[ast.Expr], sig: FnT, specs: list | None = None) -> None:
        """Check call arguments against a signature.

        When the call uses named arguments or leaves out arguments that have defaults, the call is rewritten to one
        positional argument per parameter (`e.args`), so later stages never see named arguments or missing ones.

        Args:
            e: The call, for error positions.
            name: The callee's name, for messages.
            args: The argument expressions.
            sig: The callee's type.
            specs: The callee's parameters or fields with their names and defaults, when it accepts named arguments.

        Raises:
            ZyloError: On a wrong number of arguments, an unknown, repeated or missing argument, or a mismatched type.
        """
        named = getattr(e, "named", [])
        if specs is None and named:
            raise err(f"{name} takes positional arguments only", named[0])
        if specs is not None and (named or (len(args) < len(specs) and any(p.default is not None for p in specs))):
            args = e.args = self.fill_args(e, name, args, named, specs)
        if len(args) != len(sig.params):
            raise err(f"{name} takes {len(sig.params)} argument(s), got {len(args)}", e)
        for i, (a, pt) in enumerate(zip(args, sig.params), start=1):
            t = self.expr(a, pt)
            if t != pt:
                raise err(f"argument {i} of {name}: expected {pt}, got {t}", a)

    def fill_args(self, e: ast.Node, name: str, args: list[ast.Expr], named: list[ast.NamedArg], specs: list) -> list[ast.Expr]:
        """Place positional and named arguments in parameter order and fill the gaps with defaults.

        Args:
            e: The call, for error positions.
            name: The callee's name, for messages.
            args: The positional arguments.
            named: The named arguments.
            specs: The parameters or fields, with `name` and `default`.

        Returns:
            One expression per parameter; each default is a fresh copy.

        Raises:
            ZyloError: On too many arguments, an unknown or repeated name, or a missing required argument.
        """
        if len(args) > len(specs):
            raise err(f"{name} takes {len(specs)} argument(s), got {len(args)}", e)
        slots: list[ast.Expr | None] = [*args, *([None] * (len(specs) - len(args)))]
        index = {p.name: i for i, p in enumerate(specs)}
        for n in named:
            i = index.get(n.name)
            if i is None:
                what = "field" if isinstance(specs[0], ast.Field) else "parameter"
                raise err(f"{name} has no {what} {n.name!r} (has: {', '.join(index)})", n)
            if slots[i] is not None:
                raise err(f"argument {n.name!r} of {name} is given more than once", n)
            slots[i] = n.value
        missing = [p.name for p, v in zip(specs, slots) if v is None and p.default is None]
        if missing:
            raise err(f"{name} is missing {'argument' if len(missing) == 1 else 'arguments'} "
                      f"{', '.join(repr(m) for m in missing)}", e)
        for i, p in enumerate(specs):
            if slots[i] is None:
                slots[i] = copy.deepcopy(p.default)
        e.named = []
        return slots  # type: ignore[return-value]

    def call(self, e: ast.Call, expected: Type | None = None) -> Type:
        """Type a call: a function-typed variable, a struct constructor, a built-in, or a user function."""
        e.inst = None
        var = self.lookup_var(e.func, e)
        if e.targs and (var is not None or e.func not in self.generics):
            what = "a variable" if var is not None else "not generic"
            hint = f"; to index it and call the result, write ({e.func}[...])(...)" if var is not None else ""
            raise err(f"{e.func!r} is {what}, so it takes no type arguments{hint}", e)
        if e.func in self.generics:
            return self.generic_call(e, expected)
        if var is not None and isinstance(var.ty, FnT):
            e.var = var
            self.check_args(e, e.func, e.args, var.ty)
            return var.ty.ret
        if var is not None and e.func not in self.structs and e.func not in self.fns and e.func not in BUILTINS:
            raise err(f"{e.func!r} is a {var.ty}, not a function", e)
        if e.func in self.structs:
            return self.ctor(e)
        if e.func in BUILTINS and not (e.func in SHADOWABLE and e.func in self.fns):
            if e.named:
                raise err(f"{e.func} takes positional arguments only", e.named[0])
            return self.builtin(e, expected)
        sig = self.fns.get(e.func)
        if sig is None:
            raise err(f"undefined function {e.func!r}", e)
        self.check_args(e, e.func, e.args, sig, self.specs.get(e.func))
        return sig.ret

    def typed_json(self, e: ast.Call) -> Type:
        """Type `to_json`, `from_json`, `json_schema` or `json_schema_defs` by rewriting the call into a call of a generated function.

        The functions are written for the type involved (see `semantics/derive.py`), declared, and added to the program, where
        they are checked like any other function. The call is left as an ordinary call of one of them.
        """
        name, args = e.func, e.args
        if not self.deriver.available:
            raise err(f"{name} needs the module std.json; compile a file instead of a string", e)
        if name == "to_json":
            if len(args) != 1:
                raise err(f"to_json takes 1 argument, got {len(args)}", e)
            ty = self.expr(args[0])
            if has_param(ty):  # inside a generic template: the real conversion is written for each copy
                return JSON_T
            helper = self.deriver.to_json(ty, e)
        else:
            want = 2 if name == "from_json" else 1
            if len(args) != want:
                raise err(f"{name} takes {want} argument(s): {'a type and a Json value' if want == 2 else 'a type'}", e)
            if not isinstance(args[0], ast.TypeRef):
                raise err(f"the first argument of {name} must be a type", args[0])
            ty = self.resolve(args[0])
            if has_param(ty):
                if name == "from_json" and self.expr(args[1], JSON_T) != JSON_T:
                    raise err(f"from_json expects a Json value, not {args[1].ty}", args[1])
                return ResultT(ty, STR) if name == "from_json" else JSON_T
            if name == "from_json":
                t = self.expr(args[1], JSON_T)
                if t != JSON_T:
                    raise err(f"from_json expects a Json value, not {t}", args[1])
                helper = self.deriver.from_json(ty, e)
                root = ast.Str("$", line=e.line, col=e.col, file=e.file)
                root.ty = STR
                e.args = [args[1], root]
            else:
                helper = getattr(self.deriver, name)(ty, e)
                e.args = []
        for f in self.deriver.take():
            self.declare_fn(f)
            self.program.body.append(f)
        e.func = helper
        return self.fns[helper].ret

    def type_name(self, e: ast.Call) -> Type:
        """Type `type_name(T)`: the call becomes a string literal holding the type as written.

        Inside a generic template the name is not known yet, so the call stays as it is; each copy of the template has
        the real type and gets its own name.

        Args:
            e: The call.

        Returns:
            Always `str`.

        Raises:
            ZyloError: If the call does not have exactly one type as its argument.
        """
        if len(e.args) != 1:
            raise err(f"type_name takes 1 argument, a type, got {len(e.args)}", e)
        arg = e.args[0]
        if isinstance(arg, ast.Str):  # already rewritten by an earlier round of checking
            return STR
        if not isinstance(arg, ast.TypeRef):
            raise err("the argument of type_name must be a type", arg)
        ty = self.resolve(arg)
        if not has_param(ty):
            name = ast.Str(re.sub(r"(?:\w+\u00b7)+", "", str(ty)), line=e.line, col=e.col, file=e.file)
            name.ty = STR
            e.args = [name]
        return STR

    def builtin(self, e: ast.Call, expected: Type | None = None) -> Type:
        """Type a call of a built-in function such as `print`, `len`, `sum` or `read_file`."""
        name, args = e.func, e.args

        def arity(n: int) -> None:
            if len(args) != n:
                raise err(f"{name} takes {n} argument(s), got {len(args)}", e)

        match name:
            case "to_json" | "from_json" | "json_schema" | "json_schema_defs":
                return self.typed_json(e)
            case "type_name":
                return self.type_name(e)
            case "print":  # any number of values, separated by spaces
                for a in args:
                    self.printable(a)
                return UNIT
            case "cell":
                arity(1)
                t = self.expr(args[0], expected.inner if isinstance(expected, CellT) else None)
                if t == UNIT:
                    raise err("the expression produces no value", args[0])
                return CellT(t)
            case "len":
                arity(1)
                t = self.expr(args[0])
                if t != STR and not isinstance(t, (ListT, DictT, SetT)):
                    raise err(f"len expects str, list, dict or set, not {t}", args[0])
                return INT
            case "int" | "float":
                arity(1)
                t = self.expr(args[0])
                if not (is_num(t) or t == STR):
                    raise err(f"{name}() cannot convert {t}" + needs("Num", t), args[0])
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
                if not (isinstance(t, ListT) and is_num(t.elem)):
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
                if t != u or not is_ord(t):
                    raise err(f"{name} compares two int, float or str values of the same type, not {t} and {u}", e)
                return t
            case "abs":
                arity(1)
                t = self.expr(args[0])
                if not is_num(t):
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
                    self.printable(a)
                return UNIT
            case "sleep":
                arity(1)
                if self.expr(args[0], FLOAT) != FLOAT:
                    raise err(f"sleep expects float seconds, not {args[0].ty} (use float(x))", args[0])
                return UNIT
            case "range":
                raise err("range(...) can only be used in a `for`", e)
        raise err(f"unknown built-in function {name!r}", e)


def needs(bound: str, t: Type) -> str:
    """A hint for an error about a type parameter that lacks a bound (empty for any other type)."""
    return f" (declare the type parameter as {t}: {bound})" if isinstance(t, ParamT) else ""


def field_path(e: ast.Expr) -> str | None:
    """The dotted path of a variable or a chain of field accesses (`p`, `p.name`, `self.a.b`), or None for anything else."""
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attr) and not e.variant and (base := field_path(e.value)) is not None:
        return f"{base}.{e.name}"
    return None


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
    if isinstance(last, ast.Match):
        return all(always_returns(a.body) for a in last.arms)
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
    if isinstance(last, ast.Match):
        return all(exits(a.body) for a in last.arms)
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
