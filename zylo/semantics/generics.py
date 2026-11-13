"""Generics: functions, structs and enums with type parameters, expanded by the checker (monomorphization).

A generic definition (`fn first[T](xs: list[T]) -> T`, `struct Box[T]`, `enum Opt[T]`) is a template. The checker never
emits it. For every list of type arguments a program uses it with, the checker makes a copy of the template in which each
`T` is replaced by that type (`first[int]`, `Box[str]`), registers the copy like any ordinary definition, appends it to the
program, and the rest of the compiler (borrow inference, derive, code generator) sees plain, concrete code.

The template itself is checked once, for all types, by making one more copy whose type parameters are `ParamT`s: a value of
type `T` can be stored, passed and returned, and can only be compared, printed, ordered or used in arithmetic when the
parameter declares the matching *bound* (`T: Eq`, `Show`, `Ord`, `Num`, `Key`). That copy is never emitted.

Type arguments are inferred at calls by unifying the declared parameter types with the argument types (and the type the
context expects), or written explicitly (`first[int](xs)`). See docs/design.md ("Generics").
"""
import copy
import re
from dataclasses import dataclass

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import (
    BOOL,
    FLOAT,
    INT,
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
)

BOUNDS = ("Eq", "Ord", "Num", "Show", "Key")
IMPLIES = {
    "Eq": {"Eq"},
    "Show": {"Show"},
    "Ord": {"Ord", "Eq", "Show"},
    "Num": {"Num", "Ord", "Eq", "Show"},
    "Key": {"Key", "Ord", "Eq", "Show"},
}
BOUND_HELP = {
    "Eq": "supports == and != (anything without a function inside)",
    "Show": "can be printed (anything without a function inside)",
    "Ord": "supports < > <= >= (int, float or str)",
    "Num": "supports arithmetic (int or float)",
    "Key": "can be a dict key or set element (int, str, bool or tuples of those)",
}
MAX_DEPTH = 12  # how deeply nested a type argument may be: stops `f[T]` calling `f[list[T]]` forever
MAX_INSTANCES = 500  # how many copies of generic definitions one program may need


def shown(name: str) -> str:
    """A linked name as the user wrote it (`shapes·Shape` is `Shape`)."""
    return name.rsplit("\u00b7", 1)[-1]


def _err(msg: str, node: ast.Node | None) -> ZyloError:
    if node is None:
        return ZyloError(msg)
    return ZyloError(msg, node.line, node.col, node.file)


# ---- what a type parameter is known to support ---------------------------------------------------------

def allows(t: ParamT, bound: str) -> bool:
    """Whether a type parameter's declared bounds give it a capability."""
    return any(bound in IMPLIES.get(b, {b}) for b in t.bounds)


def key_ok(t: Type) -> bool:
    """Types usable as dict keys / set elements (ordered and hashable)."""
    if isinstance(t, ParamT):
        return allows(t, "Key")
    return t in (INT, STR, BOOL) or (isinstance(t, TupleT) and all(key_ok(x) for x in t.elems))


def sortable(t: Type) -> bool:
    """Whether a list of this type can be sorted: int, float, str, bool and tuples of the orderable ones."""
    if isinstance(t, ParamT):
        return allows(t, "Ord")
    return t == FLOAT or key_ok(t)


def is_num(t: Type) -> bool:
    """Whether arithmetic is defined on this type: int, float, or a parameter bounded by `Num`."""
    return t in (INT, FLOAT) or (isinstance(t, ParamT) and allows(t, "Num"))


def is_ord(t: Type) -> bool:
    """Whether `<` is defined on this type: int, float, str, or a parameter bounded by `Ord`."""
    return t in (INT, FLOAT, STR) or (isinstance(t, ParamT) and allows(t, "Ord"))


# ---- structure of types ----------------------------------------------------------------------------------

def parts(t: Type) -> tuple[Type, ...]:
    """The types directly inside a type."""
    match t:
        case ListT(e) | SetT(e):
            return (e,)
        case OptionT(i) | CellT(i):
            return (i,)
        case ResultT(a, b):
            return (a, b)
        case DictT(k, v):
            return (k, v)
        case TupleT(es):
            return es
        case FnT(ps, r):
            return (*ps, r)
        case StructT() | EnumT():
            return t.args
    return ()


def parts_of(e: ast.Expr) -> list:
    """The elements of a list or dict literal."""
    return e.items if isinstance(e, ast.ListLit) else e.keys


def has_param(t: Type) -> bool:
    """Whether a type mentions a type parameter (so it belongs to a template, not to a running program)."""
    return isinstance(t, ParamT) or any(has_param(x) for x in parts(t))


def pattern_params(t: Type) -> set[str]:
    """The names of the type parameters a declared (template) type mentions."""
    if isinstance(t, ParamT):
        return {t.name}
    return {n for x in parts(t) for n in pattern_params(x)}


def param_types(t: Type) -> dict[str, ParamT]:
    """The type parameters a declared (template) type mentions, by name."""
    if isinstance(t, ParamT):
        return {t.name: t}
    found: dict[str, ParamT] = {}
    for x in parts(t):
        found.update(param_types(x))
    return found


def required(params: list[ast.TypeParam]) -> int:
    """How many type arguments a generic needs to be told: the parameters without a default."""
    return sum(p.default is None for p in params)


def arity(params: list[ast.TypeParam]) -> str:
    """The number of type arguments a generic takes, for messages: `2`, or `1 to 2` when some have defaults."""
    lo, hi = required(params), len(params)
    return str(hi) if lo == hi else f"{lo} to {hi}"


def int_literal(e: ast.Expr) -> bool:
    """Whether an expression is an integer literal, possibly negated: the only literals a `Num` type parameter can take."""
    return isinstance(e, ast.Int) or (isinstance(e, ast.UnaryOp) and e.op == "-" and isinstance(e.operand, ast.Int))


def type_depth(t: Type) -> int:
    """How deeply a type is nested (`int` is 0, `list[int]` 1, `Box[list[int]]` 2)."""
    inner = parts(t)
    return 1 + max(map(type_depth, inner)) if inner else 0


def unify(pat: Type, act: Type, bind: dict[str, Type]) -> bool:
    """Match a declared type with a real one, binding the type parameters of the declaration.

    Args:
        pat: A type written with the callee's type parameters.
        act: The type of an argument (or the type the context expects).
        bind: What each parameter is known to be; extended in place.

    Returns:
        False when the two cannot be the same type under any choice of parameters.
    """
    if isinstance(pat, ParamT):
        if pat.name in bind:
            return bind[pat.name] == act
        bind[pat.name] = act
        return True
    if isinstance(pat, (StructT, EnumT)) and pat.args:
        return (type(act) is type(pat) and act.base == pat.base and len(act.args) == len(pat.args)
                and all(unify(p, a, bind) for p, a in zip(pat.args, act.args)))
    if type(pat) is not type(act):
        return False
    if isinstance(pat, TupleT):
        return len(pat.elems) == len(act.elems) and all(unify(p, a, bind) for p, a in zip(pat.elems, act.elems))
    if isinstance(pat, FnT):
        return len(pat.params) == len(act.params) and all(
            unify(p, a, bind) for p, a in zip((*pat.params, pat.ret), (*act.params, act.ret)))
    inner = parts(pat)
    if inner and not isinstance(pat, (StructT, EnumT)):
        return all(unify(p, a, bind) for p, a in zip(inner, parts(act)))
    return pat == act


# ---- the checker's side --------------------------------------------------------------------------------------

@dataclass
class Generic:
    """A generic definition, as the checker keeps it.

    Attributes:
        name: The (linked) name.
        kind: "fn", "struct" or "enum".
        defn: The template, which is never checked or emitted itself.
        params: The type parameters.
        own: The parameters as types, for the one copy that checks the template.
        sig: For a function, its signature in terms of `own`.
        broken: True when checking the template found a mistake, so copies are not checked again.
    """
    name: str
    kind: str
    defn: "ast.FnDef | ast.StructDef | ast.EnumDef"
    params: list[ast.TypeParam]
    own: tuple[ParamT, ...]
    sig: FnT | None = None
    broken: bool = False


class GenericMixin:
    """The checker's handling of generics; `Checker` inherits it. See the module docstring for the design."""

    # ---- registering templates --------------------------------------------------------------------

    def register_generic(self, d: "ast.FnDef | ast.StructDef | ast.EnumDef") -> None:
        """Record a generic definition's name, rejecting duplicates, unknown bounds and reserved names."""
        kind = "fn" if isinstance(d, ast.FnDef) else "struct" if isinstance(d, ast.StructDef) else "enum"
        if d.name in self.generics or d.name in self.structs or d.name in self.enums:
            raise _err(f"{d.name!r} is already defined", d)
        if d.name in self.traits:
            raise _err(f"{d.name!r} is already a trait", d)
        for p in d.tparams:
            self.check_bound_names(p)
        self.check_defaults_of(d.tparams)
        if kind != "fn":
            for m in d.methods:
                if m.tparams:
                    self.check_method_params(m, d.tparams)
        own = tuple(ParamT(p.name, frozenset(p.bounds)) for p in d.tparams)
        self.generics[d.name] = Generic(d.name, kind, d, d.tparams, own)

    def named(self, name: str) -> Type:
        """The type of a struct or enum by its name; instances of generic types remember their arguments."""
        found = self.itypes.get(name)
        if found is not None:
            return found
        return EnumT(name) if name in self.enums else StructT(name)

    def check_method_params(self, m: ast.FnDef, owner_params: list[ast.TypeParam]) -> None:
        """Reject unknown bounds on a method's type parameters, and names the struct or enum already uses."""
        for p in m.tparams:
            if any(q.name == p.name for q in owner_params):
                raise _err(f"type parameter {p.name!r} of method {m.name!r} hides the one of the same name on the "
                           f"struct or enum; use another name", p)
            self.check_bound_names(p)
            if p.default is not None:
                raise _err(f"default type arguments are for functions, structs and enums, not for the methods of a "
                           f"generic: remove `= {p.default.name}` from {p.name!r}", p)

    def check_defaults_of(self, params: list[ast.TypeParam]) -> None:
        """Reject a default type argument that mentions a type parameter (a default is a plain type)."""
        names = {p.name for p in params}
        for p in params:
            if p.default is not None and any(isinstance(n, ast.TypeRef) and n.name in names for n in ast.walk(p.default)):
                raise _err(f"the default of type parameter {p.name!r} cannot mention a type parameter", p.default)

    def fix_method_params(self, m: ast.FnDef) -> tuple[ParamT, ...]:
        """Make a generic method's own type parameters opaque types in its template, and return them."""
        own = tuple(ParamT(p.name, frozenset(p.bounds)) for p in m.tparams)
        by_name = {p.name: t for p, t in zip(m.tparams, own)}
        for n in ast.walk(m):
            if isinstance(n, ast.TypeRef) and n.name in by_name and not n.args and n.ret is None:
                n.fixed = by_name[n.name]
        return own

    def declare_generic_method(self, d: "ast.StructDef | ast.EnumDef", info, m: ast.FnDef) -> None:
        """Record a generic method of a struct or enum (or of one instance of a generic struct) as a template.

        Its signature is written with its own type parameters as opaque types; copies for each list of type arguments are
        made at the calls (`instantiate_method`) and added to the struct, so only they are emitted.
        """
        if not d.instance:
            self.check_method_params(m, d.tparams)
        self.fix_method_params(m)
        m.ty = self.signature(m)
        info.gmethods[m.name] = m
        self.specs[f"{d.name}.{m.name}"] = m.params

    # ---- making copies ----------------------------------------------------------------------------

    def copy_for(self, g: Generic, args: tuple[Type, ...], name: str) -> "ast.FnDef | ast.StructDef | ast.EnumDef":
        """A copy of a template in which every type parameter stands for the given type."""
        d = copy.deepcopy(g.defn)
        by_name = {p.name: a for p, a in zip(g.params, args)}
        for n in ast.walk(d):
            if isinstance(n, ast.TypeRef) and n.name in by_name and not n.args and n.ret is None:
                n.fixed = by_name[n.name]
        d.tparams = []
        d.instance = True
        d.origin = g.name  # which template this is a copy of (not a dataclass field: plain bookkeeping)
        d.name = name
        if not isinstance(d, ast.FnDef):
            d.display = g.defn.display or g.defn.name
        return d

    def instance_name(self, base: str, args: tuple[Type, ...]) -> str:
        """The name of one instance: `Box[int]`, `Pair[int, list[str]]`."""
        key = (base, args)
        name = f"{base}[{', '.join(map(str, args))}]"
        while self.taken.get(name, key) != key:  # an ordinary type that is spelled like a type parameter
            name += "'"
        self.taken[name] = key
        return name

    def check_limits(self, g: Generic, args: tuple[Type, ...], node: ast.Node | None) -> None:
        """Stop a generic that expands into ever bigger types (`f[T]` calling `f[list[T]]`)."""
        self.ninst += 1
        if any(type_depth(a) > MAX_DEPTH for a in args) or self.ninst > MAX_INSTANCES:
            shown = f"{g.name}[{', '.join(map(str, args))}]"
            raise _err(f"generic expansion too deep: {shown[:80]}...; a generic cannot keep instantiating itself with bigger "
                       f"types (limits: {MAX_DEPTH} levels of nesting, {MAX_INSTANCES} instances)", node)

    def instance_type(self, g: Generic, args: tuple[Type, ...], node: ast.Node | None) -> Type:
        """The struct or enum type for one list of type arguments, creating the instance the first time.

        Args:
            g: The generic struct or enum.
            args: One type per type parameter.
            node: Where to report a problem.

        Returns:
            The instance's type (`Box[int]`).
        """
        key = (g.name, args)
        if key in self.insts:
            return self.itypes[self.insts[key]]
        self.check_limits(g, args, node)
        name = self.instance_name(g.name, args)
        self.insts[key] = name
        d = self.copy_for(g, args, name)
        is_struct = isinstance(d, ast.StructDef)
        ty = (StructT if is_struct else EnumT)(name, args, g.name)
        self.itypes[name] = ty
        self.idefs[name] = d
        if is_struct:
            self.register_struct(d)
            self.resolve_struct(d)
        else:
            self.register_enum(d)
            self.resolve_enum(d)
        if not any(has_param(a) for a in args):
            self.program.body.append(d)
        if self.deferred is not None:
            self.deferred.append(d)
        else:
            self.finish_instance(d)
        return ty

    def finish_instance(self, d: "ast.StructDef | ast.EnumDef") -> None:
        """The parts of making an instance that need the rest of the program: defaults, methods, containing itself."""
        if isinstance(d, ast.StructDef):
            self.check_defaults(d.fields, "field", [f.ty for f in d.fields])
        self.declare_methods(d)
        self.check_cycles([d] if isinstance(d, ast.StructDef) else [], [d] if isinstance(d, ast.EnumDef) else [])

    def declare_generic(self, g: Generic) -> None:
        """Prepare to check a template for every type.

        Its type parameters become `ParamT`s, and a struct or enum gets the copy that carries the signatures of its
        methods. The template itself is what gets checked (so the editor can show types inside it); copies are made from it
        afterwards, and they overwrite everything the check left on it.
        """
        for p in g.params:
            if p.default is not None:
                t = self.resolve(p.default)
                for b in p.bounds:
                    if not self.satisfies(t, b):
                        raise _err(f"the default {t} of type parameter {p.name!r} does not satisfy its bound {shown(b)}",
                                   p.default)
        by_name = dict(zip((p.name for p in g.params), g.own))
        for n in ast.walk(g.defn):
            if isinstance(n, ast.TypeRef) and n.name in by_name and not n.args and n.ret is None:
                n.fixed = by_name[n.name]
        if g.kind == "fn":
            g.defn.ty = g.sig = self.signature(g.defn)
            self.specs[g.name] = g.defn.params
            self.abstract.append((g, g.defn, ""))
        else:
            t = self.instance_type(g, g.own, g.defn)
            for m, made in zip(g.defn.methods, self.idefs[t.name].methods):
                if m.tparams:
                    self.fix_method_params(m)
                m.ty = made.ty
            self.abstract.append((g, g.defn, t.name))

    def instantiate_fn(self, g: Generic, args: tuple[Type, ...], node: ast.Node) -> str:
        """The name of the copy of a generic function for one list of type arguments, creating it the first time."""
        name = self.instance_name(g.name, args)
        if name in self.fns:
            return name
        self.check_limits(g, args, node)
        d = self.copy_for(g, args, name)
        d.ty = self.fns[name] = self.signature(d)
        self.fn_insts[(g.name, args)] = name
        self.specs[name] = d.params
        if not g.broken:
            self.program.body.append(d)  # the statement loop checks it, like the functions generated for typed JSON
        return name

    def check_abstract(self) -> None:
        """Check every template once, for all types, before any copy of it is checked."""
        for g, d, owner in self.abstract:
            before = len(self.errors)
            for f in [d] if isinstance(d, ast.FnDef) else d.methods:
                mark = len(self.errors)
                try:
                    self.check_fn(f, owner or None)
                except ZyloError as e:
                    self.errors.append(e)
                if f.tparams and owner:
                    self.mbroken[(g.name if g else owner, f.name)] = len(self.errors) > mark
            if g is not None:
                g.broken = len(self.errors) > before

    # ---- substitution -------------------------------------------------------------------------------

    def subst(self, t: Type, bind: dict[str, Type]) -> Type:
        """Replace the type parameters of a declared type by what they were bound to."""
        match t:
            case ParamT():
                return bind.get(t.name, t)
            case ListT(e):
                return ListT(self.subst(e, bind))
            case SetT(e):
                return SetT(self.subst(e, bind))
            case OptionT(i):
                return OptionT(self.subst(i, bind))
            case CellT(i):
                return CellT(self.subst(i, bind))
            case ResultT(a, b):
                return ResultT(self.subst(a, bind), self.subst(b, bind))
            case DictT(k, v):
                return DictT(self.subst(k, bind), self.subst(v, bind))
            case TupleT(es):
                return TupleT(tuple(self.subst(x, bind) for x in es))
            case FnT(ps, r):
                return FnT(tuple(self.subst(x, bind) for x in ps), self.subst(r, bind))
            case StructT() | EnumT() if t.args:
                return self.instance_type(self.generics[t.base], tuple(self.subst(a, bind) for a in t.args), None)
        return t

    # ---- bounds -------------------------------------------------------------------------------------

    def satisfies(self, t: Type, bound: str) -> bool:
        """Whether a type has the capability a bound names."""
        if isinstance(t, ParamT):
            return allows(t, bound)
        if bound in self.traits:
            return bound in self.implemented(t)
        if bound == "Eq":
            return not self.contains_fn(t, need="Eq")
        if bound == "Show":
            return t != UNIT and not self.contains_fn(t, need="Show")
        if bound == "Ord":
            return t in (INT, FLOAT, STR)
        if bound == "Num":
            return t in (INT, FLOAT)
        return key_ok(t)

    def check_bounds(self, g: Generic, label: str, bind: dict[str, Type], node: ast.Node) -> None:
        """Reject a type argument that lacks what its parameter's bounds ask for."""
        for p in g.params:
            for b in p.bounds:
                if self.satisfies(bind[p.name], b):
                    continue
                if b in self.traits:
                    raise _err(f"{label}: {shown(str(bind[p.name]))} does not implement trait {shown(b)}, which bounds "
                               f"{p.name} ({self.how_to_implement(bind[p.name], b)})", node)
                raise _err(f"{label}: {bind[p.name]} cannot be used for {p.name}, which is bound by `{b}`: the type must be "
                           f"one that {BOUND_HELP[b]}", node)

    def how_to_implement(self, t: Type, trait: str) -> str:
        """The hint for a type argument that lacks a trait: what to write to make it implement it."""
        if isinstance(t, ParamT):
            return f"declare it: `{t}: {shown(trait)}`"
        if isinstance(t, (StructT, EnumT)):
            kind = "struct" if isinstance(t, StructT) else "enum"
            return f"list the trait in its definition: `{kind} {shown(t.base or t.name)}({shown(trait)}):`"
        return "only a struct or enum can implement a trait"

    # ---- inference -------------------------------------------------------------------------------------

    def infer(self, e: ast.Node, label: str, g: Generic, params: list[Type], ret: Type, args: list[ast.Expr],
              specs: list | None, expected: Type | None, written: list[ast.TypeRef],
              known: dict[str, Type] | None = None) -> dict[str, Type]:
        """Work out the type arguments of a call of a generic function, struct constructor or enum variant.

        Explicit arguments come first; then each argument's type is unified with its declared type (so the first argument
        that mentions `T` decides it and the others must agree), and the type the context expects fills in what no argument
        told. The arguments are checked once, here.

        Args:
            e: The call, for error positions.
            label: The callee's name, for messages.
            g: The generic being called.
            params: Declared parameter types, in terms of the generic's own type parameters.
            ret: The declared result type.
            args: The argument expressions as written (named arguments and defaults are folded in here).
            specs: The parameters or fields with their names and defaults, if named arguments are allowed.
            expected: The type the context expects, if any.
            written: Type arguments written by the user (`first[int](...)`).
            known: Type parameters that are already decided: those of the struct or enum around a generic method.

        Returns:
            Each type parameter's type (and the `known` ones).

        Raises:
            ZyloError: On the wrong number of arguments or type arguments, a mismatched argument, a parameter that no
                argument or context determines, or a type that breaks a bound.
        """
        bind: dict[str, Type] = dict(known or {})
        if written:
            if not required(g.params) <= len(written) <= len(g.params):
                raise _err(f"{label} takes {arity(g.params)} type argument(s) ({', '.join(p.name for p in g.params)}), "
                           f"got {len(written)}", e)
            for p, w in zip(g.params, written):
                bind[p.name] = self.resolve(w)
        named = getattr(e, "named", [])
        if specs is None and named:
            raise _err(f"{label} takes positional arguments only", named[0])
        if specs is not None and (named or (len(args) < len(specs) and any(p.default is not None for p in specs))):
            args = e.args = self.fill_args(e, label, args, named, specs)
        if len(args) != len(params):
            raise _err(f"{label} takes {len(params)} argument(s), got {len(args)}", e)
        hint: dict[str, Type] = {}
        if expected is not None:
            unify(ret, expected, hint)
        later = []  # arguments that cannot be typed alone (`none`, `[]`, anonymous functions) wait for the others
        for i, (a, pt) in enumerate(zip(args, params), start=1):
            if (isinstance(a, (ast.Lambda, ast.NoneLit)) or (isinstance(a, (ast.ListLit, ast.DictLit)) and not parts_of(a))
                    or int_literal(a) or self.generic_fn_ref(a)):
                later.append((i, a, pt))
                continue
            self.generic_arg(label, i, a, pt, bind, hint)
        later.sort(key=lambda x: not int_literal(x[1]))  # a literal takes its type from the others, so it goes first
        for i, a, pt in later:
            if isinstance(a, ast.Lambda):
                self.generic_lambda(label, i, a, pt, bind)
            else:
                self.generic_arg(label, i, a, pt, bind, hint)
        for p in g.params:
            if p.name not in bind and p.name in hint:
                bind[p.name] = hint[p.name]
        for p in g.params:
            if p.name not in bind and p.default is not None:
                bind[p.name] = self.resolve(p.default)  # what nothing determines takes its default
        for p in g.params:
            if p.name not in bind:
                raise _err(f"cannot infer type parameter {p.name} of {label}: nothing determines it; write the type arguments "
                           f"({label}[...]) or annotate where the result goes", e)
        self.check_bounds(g, label, bind, e)
        return bind

    def generic_arg(self, label: str, i: int, a: ast.Expr, pt: Type, bind: dict[str, Type], hint: dict[str, Type]) -> None:
        """Check one argument of a generic call and learn from its type what the type parameters are."""
        known = {**hint, **bind}
        want = self.subst(pt, known) if pattern_params(pt) <= known.keys() else None
        if want is None and isinstance(pt, FnT) and self.generic_fn_ref(a) and not a.targs \
                and all(pattern_params(x) <= known.keys() for x in pt.params):
            ret = self.subst(pt.ret, known) if pattern_params(pt.ret) <= known.keys() else None
            t = a.ty = self.fn_value(a, None, [self.subst(x, known) for x in pt.params], ret)
        else:
            t = self.expr(a, want)
        if not unify(pt, t, bind):
            raise _err(f"argument {i} of {label}: expected {self.subst(pt, bind)}, got {t}", a)

    def generic_lambda(self, label: str, i: int, a: ast.Lambda, pt: Type, bind: dict[str, Type]) -> None:
        """Check an anonymous function passed to a generic: its parameter types come from the type parameters already known."""
        if isinstance(pt, FnT) and all(pattern_params(x) <= bind.keys() for x in pt.params):
            params = [self.subst(x, bind) for x in pt.params]
            ret = self.subst(pt.ret, bind) if pattern_params(pt.ret) <= bind.keys() else None
            t = self.lambda_expr(a, None, params, ret)
        elif all(p.type is not None for p in a.params):
            t = self.lambda_expr(a, None)
        else:
            raise _err(f"cannot infer the parameter types of this function (argument {i} of {label}): give them a type "
                       f"(`fn(x: int) => ...`) or write the type arguments", a)
        a.ty = t
        if not unify(pt, t, bind):
            raise _err(f"argument {i} of {label}: expected {self.subst(pt, bind)}, got {t}", a)

    # ---- the three kinds of generic use ---------------------------------------------------------------

    def generic_call(self, e: ast.Call, expected: Type | None) -> Type:
        """Type a call whose callee is a generic function or struct, and pick the instance it uses."""
        g = self.generics[e.func]
        shown = e.func.rsplit("·", 1)[-1]
        if g.kind == "enum":
            raise _err(f"{shown!r} is an enum; name one of its variants: {shown}.Variant", e)
        if g.kind == "fn":
            bind = self.infer(e, shown, g, list(g.sig.params), g.sig.ret, e.args, self.specs.get(g.name), expected, e.targs)
            targs = tuple(bind[p.name] for p in g.params)
            if not any(has_param(t) for t in targs):
                e.inst = self.instantiate_fn(g, targs, e)
            return self.subst(g.sig.ret, bind)
        own = self.instance_type(g, g.own, g.defn)
        info = self.structs[own.name]
        bind = self.infer(e, shown, g, list(info.fields.values()), own, e.args, self.specs.get(own.name), expected, e.targs)
        t = self.instance_type(g, tuple(bind[p.name] for p in g.params), e)
        e.inst = t.name
        e.ctor_fields = list(self.structs[t.name].fields)
        return t

    def generic_variant(self, e: "ast.Attr | ast.MethodCall", g: Generic, name: str, args: list[ast.Expr] | None,
                        expected: Type | None) -> Type:
        """Type `Opt.Some(3)` / `Opt.Empty` on a generic enum, and pick the instance it builds."""
        shown = g.name.rsplit("·", 1)[-1]
        own = self.instance_type(g, g.own, g.defn)
        info = self.enums[own.name]
        tys = info.variants.get(name)
        if tys is None:
            raise _err(f"enum {shown!r} has no variant {name!r} (has: {', '.join(info.variants)})", e)
        if args is None and tys:
            raise _err(f"variant {shown}.{name} carries data; write {shown}.{name}({', '.join(str(t) for t in tys)})", e)
        if args is not None and not tys:
            raise _err(f"variant {shown}.{name} carries no data; write {shown}.{name} without parentheses", e)
        bind = self.infer(e, f"{shown}.{name}", g, tys, own, args or [], None, expected, getattr(e.value, "targs", []))
        t = self.instance_type(g, tuple(bind[p.name] for p in g.params), e)
        e.variant = (t.name, name)
        return t

    def generic_enum_of(self, e: ast.Expr) -> Generic | None:
        """The generic enum a `Name` refers to when it is used as `Enum.Variant`, otherwise None."""
        if isinstance(e, ast.Name) and e.id in self.generics and self.generics[e.id].kind == "enum" \
                and self.scopes.lookup(e.id) is None:
            return self.generics[e.id]
        return None

    def resolve_generic(self, t: ast.TypeRef) -> Type:
        """Resolve `Box[int]` (a written type that names a generic struct or enum)."""
        g = self.generics[t.name]
        shown = t.name.rsplit("·", 1)[-1]
        if g.kind == "fn":
            raise _err(f"{shown!r} is a generic function, not a type", t)
        if not required(g.params) <= len(t.args) <= len(g.params):
            want = ", ".join(p.name + (" = " + p.default.name if p.default else "") for p in g.params)
            raise _err(f"{shown} takes {arity(g.params)} type argument(s) ({shown}[{want}]), got {len(t.args)}", t)
        args = [self.resolve(a) for a in t.args] + [self.resolve(p.default) for p in g.params[len(t.args):]]
        return self.instance_type(g, tuple(args), t)

    # ---- generic methods -------------------------------------------------------------------------------

    def generic_method_call(self, e: ast.MethodCall, rt: Type, info, expected: Type | None) -> Type:
        """Type `b.map(f)` / `b.map[str](f)` where `map` is a generic method, and pick the copy it uses.

        The signature is the one of the template (in terms of the struct's own parameters and the method's), with the
        struct's parameters already bound to what the receiver's type says, so a receiver `Box[U]` inside a generic
        function that has its own `U` cannot be confused with the method's `U`.
        """
        m = info.gmethods[e.name]
        outer: dict[str, Type] = {}
        tinfo = info
        if rt.base:
            gs = self.generics[rt.base]
            own_t = self.instance_type(gs, gs.own, gs.defn)
            tinfo = self.structs[own_t.name] if isinstance(own_t, StructT) else self.enums[own_t.name]
            outer = {p.name: a for p, a in zip(gs.params, rt.args)}
        tm = tinfo.gmethods[e.name]
        shown = f"{str(rt).rsplit('·', 1)[-1]}.{e.name}"
        own = tuple(ParamT(p.name, frozenset(p.bounds)) for p in m.tparams)
        g = Generic(f"{rt.name}.{e.name}", "method", m, m.tparams, own, tm.ty)
        bind = self.infer(e, shown, g, list(tm.ty.params), tm.ty.ret, e.args, self.specs.get(f"{rt.name}.{e.name}"), expected,
                          e.targs, outer)
        targs = tuple(bind[p.name] for p in m.tparams)
        e.inst = None
        if not has_param(rt) and not any(has_param(t) for t in targs):
            e.inst = self.instantiate_method(rt, info, m, targs, e)
            if (rt.name, e.inst) in self.mutating:
                self.mark_mutable(e.value)
        elif (rt.name, e.name) in self.mutating:
            self.mark_mutable(e.value)
        return self.subst(tm.ty.ret, bind)

    def instantiate_method(self, rt: Type, info, m: ast.FnDef, targs: tuple[Type, ...], node: ast.Node) -> str:
        """The name of the copy of a generic method for one list of type arguments, creating it the first time.

        The copy is an ordinary method of the struct or enum (named `map[str]`), checked from the queue like the
        functions generated for typed JSON.
        """
        name = f"{m.name}[{', '.join(map(str, targs))}]"
        if name in info.methods:
            return name
        g = Generic(f"{rt.name}.{m.name}", "method", m, m.tparams, (), m.ty)
        self.check_limits(g, targs, node)
        d = self.copy_for(g, targs, name)
        d.has_self = m.has_self
        d.ty = info.methods[name] = self.signature(d)
        self.specs[f"{rt.name}.{name}"] = d.params
        d.instance = True
        self.minsts.append((rt.base or rt.name, m.name, targs))
        if not self.mbroken.get((rt.base or rt.name, m.name)):
            info.defn.methods.append(d)
            self.pending.append((d, rt.name))
        return name

    # ---- generic functions as values -------------------------------------------------------------------

    def generic_fn_ref(self, e: ast.Expr) -> bool:
        """Whether an expression names a generic function (not shadowed by a variable) rather than calling or reading one."""
        return (isinstance(e, ast.Name) and e.id in self.generics and self.generics[e.id].kind == "fn"
                and self.scopes.lookup(e.id) is None)

    def fn_value(self, e: ast.Name, expected: Type | None, params: list[Type] | None = None, ret: Type | None = None) -> Type:
        """Type a generic function used as a value, and pick the instance it stands for.

        The type arguments are written (`first[int]`), or follow from the function type the context expects, or from the
        parameter types (and result) the receiver of the value is known to need.

        Args:
            e: The name.
            expected: The function type the context expects, if any.
            params: Parameter types the value must accept (when the whole expected type is not known yet).
            ret: The result type it must return, if known.

        Returns:
            The function type of the instance.

        Raises:
            ZyloError: When the type arguments cannot be worked out, or the function does not fit what is expected.
        """
        g = self.generics[e.id]
        shown = e.id.rsplit("\u00b7", 1)[-1]
        bind: dict[str, Type] = {}
        if e.targs:
            if not required(g.params) <= len(e.targs) <= len(g.params):
                raise _err(f"{shown} takes {arity(g.params)} type argument(s) ({', '.join(p.name for p in g.params)}), "
                           f"got {len(e.targs)}", e)
            for p, w in zip(g.params, e.targs):
                bind[p.name] = self.resolve(w)
        else:
            if isinstance(expected, FnT) and params is None:
                params, ret = list(expected.params), expected.ret
            if params is not None:
                ok = len(params) == len(g.sig.params) and all(unify(p, a, bind) for p, a in zip(g.sig.params, params))
                ok = ok and (ret is None or unify(g.sig.ret, ret, bind))
                if not ok:
                    want = expected if expected is not None else f"fn({', '.join(map(str, params))})"
                    raise _err(f"generic function {shown} has type {g.sig}, which does not fit {want}", e)
        for p in g.params:
            if p.name not in bind and p.default is not None:
                bind[p.name] = self.resolve(p.default)
        for p in g.params:
            if p.name not in bind:
                raise _err(f"cannot infer type parameter {p.name} of {shown} used as a value: write the type arguments "
                           f"({shown}[...]) or use it where a function type is expected", e)
        self.check_bounds(g, shown, bind, e)
        targs = tuple(bind[p.name] for p in g.params)
        e.inst = None
        if not any(has_param(t) for t in targs):
            e.inst = self.instantiate_fn(g, targs, e)
        return self.subst(g.sig, bind)

    # ---- what the editor shows ---------------------------------------------------------------------------

    def note_instances(self) -> None:
        """Leave on each template the types its parameters were instantiated with (`tinst`), for hover in the editor."""
        for g in self.generics.values():
            found: dict[str, list[str]] = {p.name: [] for p in g.params}
            for base, args in [*self.insts, *self.fn_insts]:
                if base == g.name:
                    self.note_args(found, g.params, args)
            g.defn.tinst = found
        for s in self.program.body:
            if isinstance(s, (ast.StructDef, ast.EnumDef)) and not s.instance:
                for m in s.methods:
                    if m.tparams:
                        found = {p.name: [] for p in m.tparams}
                        for base, name, args in self.minsts:
                            if base == s.name and name == m.name:
                                self.note_args(found, m.tparams, args)
                        m.tinst = found

    @staticmethod
    def note_args(found: dict[str, list[str]], params: list[ast.TypeParam], args: tuple[Type, ...]) -> None:
        """Add one instance's type arguments to what each parameter was instantiated with, unless it has a parameter."""
        if any(has_param(a) for a in args):
            return
        for p, a in zip(params, args):
            text = re.sub(r"(?:\w+\u00b7)+", "", str(a))
            if text not in found[p.name]:
                found[p.name].append(text)
