"""Traits: named sets of method signatures that structs and enums implement and generics use as bounds.

A `trait Shape:` declares method signatures (`fn area(self) -> float`). A struct or enum implements it by listing it
after its name (`struct Circle(Shape):`) and defining every method with exactly that signature, where `Self` in the trait
stands for the implementing type. A generic bounds a type parameter by the trait (`fn total[T: Shape](xs: list[T])`,
combined with the built-in bounds: `T: Shape + Show`).

Traits cost nothing at run time. Generics are monomorphized, so a bound is only checked where a generic is instantiated
(the type argument must list the trait), and a method call on a `T` is resolved per copy to the real method of the real
type. The template itself is checked once with `T` opaque: calls on it may only use the methods its bounds declare.
Nothing is dispatched dynamically, and the generator never sees a trait. See docs/design.md ("Traits").
"""
from dataclasses import dataclass, field

from zylo.domain import ast
from zylo.domain.types import UNIT, EnumT, FnT, ParamT, StructT, Type
from zylo.semantics.generics import BOUNDS, _err, shown

SELF = ParamT("Self")


@dataclass
class TraitInfo:
    """What the checker knows about one trait.

    Attributes:
        name: The trait's (linked) name.
        defn: Its definition node.
        methods: The required methods' signatures by name, written with `Self` as an opaque type.
    """
    name: str
    defn: ast.TraitDef
    methods: dict[str, FnT] = field(default_factory=dict)


def signature_text(name: str, params: list[ast.Param], sig: FnT) -> str:
    """A method's signature as one would write it: `fn scale(self, by: float) -> Circle`."""
    inner = ", ".join(["self", *(f"{p.name}: {t}" for p, t in zip(params, sig.params))])
    ret = f" -> {sig.ret}" if sig.ret != UNIT else ""
    return f"fn {name}({inner}){ret}"


class TraitMixin:
    """The checker's handling of traits; `Checker` inherits it. See the module docstring."""

    # ---- declaring traits -----------------------------------------------------------------------

    def register_trait(self, d: ast.TraitDef) -> None:
        """Record a trait's name, rejecting duplicates and reserved names."""
        if d.name in self.traits:
            raise _err(f"trait {d.name!r} is already defined", d)
        if d.name in BOUNDS or d.name == "Self":
            raise _err(f"{shown(d.name)!r} is a reserved name", d)
        if d.name in self.type_names:
            raise _err(f"{shown(d.name)!r} is already a struct or enum", d)
        self.traits[d.name] = TraitInfo(d.name, d)

    def resolve_traits(self) -> None:
        """Resolve the signatures of every trait's methods, with `Self` standing for the implementing type."""
        for info in self.traits.values():
            d = info.defn
            if not d.methods:
                raise _err(f"trait {shown(d.name)!r} needs at least one method", d)
            self.self_ty = SELF
            try:
                for m in d.methods:
                    if not m.has_self:
                        raise _err(f"method {m.name!r} of trait {shown(d.name)!r} must take `self` as its first parameter", m)
                    if m.name in info.methods:
                        raise _err(f"method {m.name!r} is already declared in trait {shown(d.name)!r}", m)
                    if m.tparams:
                        raise _err(f"method {m.name!r} of trait {shown(d.name)!r} cannot be generic", m)
                    for p in m.params:
                        if p.default is not None:
                            raise _err(f"parameter {p.name!r} of trait method {m.name!r} cannot have a default value: "
                                       f"the trait only declares the signature", p)
                    m.ty = info.methods[m.name] = self.signature(m)
                    self.specs[f"{d.name}.{m.name}"] = m.params
            finally:
                self.self_ty = None

    def check_trait_lists(self, body: list[ast.Stmt]) -> None:
        """Reject a struct or enum that lists something that is not a trait, or the same trait twice."""
        for s in body:
            seen: set[str] = set()
            for r in getattr(s, "traits", ()):
                if r.name not in self.traits:
                    raise _err(self.not_a_trait(r.name, "trait"), r)
                if r.name in seen:
                    raise _err(f"{shown(s.name)!r} implements trait {shown(r.name)!r} twice", r)
                seen.add(r.name)

    def not_a_trait(self, name: str, what: str) -> str:
        """The error for a name used as a trait (a base or a bound) that is not one."""
        if name in self.type_names or name in self.generics:
            kind = "an enum" if name in self.enums or getattr(self.generics.get(name), "kind", "") == "enum" else "a struct"
            if name in self.generics and self.generics[name].kind == "fn":
                kind = "a function"
            return f"{shown(name)!r} is {kind}, not a trait"
        have = ", ".join(shown(n) for n in self.traits)
        hint = f" (declared: {have})" if have else " (no trait is declared: `trait Name:` with method signatures)"
        return f"unknown {what} {shown(name)!r}{hint}"

    def check_bound_names(self, p: ast.TypeParam) -> None:
        """Reject a type parameter bound that is neither a built-in bound nor a trait."""
        for b in p.bounds:
            if b in BOUNDS or b in self.traits:
                continue
            if b in self.type_names or b in self.generics:
                raise _err(self.not_a_trait(b, "bound"), p)
            have = ", ".join([*BOUNDS, *(shown(n) for n in self.traits)])
            raise _err(f"unknown bound {shown(b)!r} on type parameter {p.name!r} (available: {have})", p)

    # ---- what a type implements -----------------------------------------------------------------------

    def implemented(self, t: Type) -> frozenset[str]:
        """The traits a type is known to implement: those a struct or enum lists, or a type parameter is bounded by."""
        if isinstance(t, ParamT):
            return frozenset(b for b in t.bounds if b in self.traits)
        if isinstance(t, StructT) and t.name in self.structs:
            return frozenset(self.structs[t.name].traits)
        if isinstance(t, EnumT) and t.name in self.enums:
            return frozenset(self.enums[t.name].traits)
        return frozenset()

    def check_impls(self, d: "ast.StructDef | ast.EnumDef", info) -> None:
        """Check that a struct or enum defines every method of the traits it lists, with the traits' signatures."""
        own = self.named(d.name)
        for r in d.traits:
            trait = self.traits[r.name]
            for name, want in trait.methods.items():
                want = self.subst(want, {"Self": own})
                spec = trait.defn.methods[list(trait.methods).index(name)]
                if name in info.gmethods:
                    raise _err(f"method {name!r} of {shown(d.name)!r} is generic, so it cannot implement trait "
                               f"{shown(r.name)!r}", info.gmethods[name])
                have = info.methods.get(name)
                if have is None:
                    raise _err(f"{shown(d.name)!r} implements trait {shown(r.name)!r} but has no method {name!r}; add "
                               f"`{signature_text(name, spec.params, want)}:`", r)
                m = next(x for x in d.methods if x.name == name)
                names = [p.name for p in m.params]
                if have != want or names != [p.name for p in spec.params]:
                    raise _err(f"method {name!r} of {shown(d.name)!r} does not match trait {shown(r.name)!r}: expected "
                               f"`{signature_text(name, spec.params, want)}`, found `{signature_text(name, m.params, have)}`", m)

    # ---- using a trait as a bound -------------------------------------------------------------------

    def param_method(self, e: ast.MethodCall, rt: ParamT) -> Type:
        """Type a method call on a value whose type is a type parameter: only the methods its trait bounds declare.

        The call is checked against the trait's signature, with `Self` replaced by the parameter. In a copy of the
        template the parameter is a real type and the call is an ordinary method call, so nothing is recorded here.
        """
        found = [(self.traits[b], self.traits[b].methods[e.name]) for b in sorted(rt.bounds)
                 if b in self.traits and e.name in self.traits[b].methods]
        e.inst = None
        if not found:
            declared = [shown(b) for b in sorted(rt.bounds) if b in self.traits]
            owners = [shown(t.name) for t in self.traits.values() if e.name in t.methods]
            if owners:
                raise _err(f"type parameter {rt} has no method {e.name!r}; declare it with a bound: {rt}: {owners[0]}", e)
            have = f" (its traits: {', '.join(declared)})" if declared else " (give it a trait bound that declares methods)"
            raise _err(f"type parameter {rt} has no method {e.name!r}{have}", e)
        if len({sig for _, sig in found}) > 1:
            names = " and ".join(shown(t.name) for t, _ in found)
            raise _err(f"method {e.name!r} is declared differently by {names}, so the call on {rt} is ambiguous", e)
        trait, sig = found[0]
        sig = self.subst(sig, {"Self": rt})
        self.check_args(e, f"{rt}.{e.name}", e.args, sig, self.specs.get(f"{trait.name}.{e.name}"))
        return sig.ret
