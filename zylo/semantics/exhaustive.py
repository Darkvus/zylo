"""Exhaustiveness and reachability of `match` patterns.

The algorithm is Maranget's *usefulness* check ("Warnings for pattern matching", 2007). A pattern matrix has one row
per unguarded arm and one column per value still to be matched; a pattern vector is *useful* with respect to a matrix
when it matches some value that no row matches. Two questions are answered with it:

- **Reachability**: an arm (or one alternative of an or-pattern) can run only if it is useful with respect to the
  unguarded arms before it.
- **Exhaustiveness**: the match covers every value unless a vector of wildcards is useful with respect to all the
  unguarded arms. `missing` finds such values and writes them as patterns, for the error message.

Patterns are first lowered to `Con` (a constructor with sub-patterns), `Alt` (an or-pattern) or None (a wildcard or a
binding; `name @ pattern` is lowered as the pattern). The constructors are:

- `("v", name)`: an enum variant, `some`, `none`, `ok` or `err`; its sub-patterns are the variant's data.
- `("t",)`: a tuple; its sub-patterns are the elements.
- `("st",)`: a struct, the only constructor of its type; its sub-patterns are the fields in declaration order (a field the
  pattern does not mention is a wildcard).
- `("b", value)`: `true` or `false`.
- `("s", text)`: a string literal; `str` has infinitely many values, so a column of strings is never complete.
- `("i", lo, hi)`: an integer interval, both ends included (a literal is `lo == hi`). The columns of integers are
  split into the disjoint intervals their patterns delimit, and each one is treated as a constructor of its own.

Guarded arms are never rows of the matrix: a guard may reject the value, so the arm proves nothing.
"""
from collections.abc import Callable
from dataclasses import dataclass

from zylo.domain import ast
from zylo.domain.types import BOOL, INT, STR, EnumT, OptionT, ResultT, StructT, TupleT, Type

INT_MIN, INT_MAX = ast.INT_MIN, ast.INT_MAX
LIMIT = 6  # witnesses collected for an error message

Ctor = tuple


@dataclass(frozen=True)
class Con:
    """A constructor applied to sub-patterns."""
    ctor: Ctor
    subs: tuple = ()


@dataclass(frozen=True)
class Alt:
    """An or-pattern."""
    alts: tuple


Pat = Con | Alt | None


def lower(p: ast.Pattern) -> Pat:
    """Turn a checked pattern into the form the algorithm works on."""
    match p.kind:
        case "wild" | "bind":
            return None
        case "lit":
            if p.ty == BOOL:
                return Con(("b", p.literal.value))
            if p.ty == STR:
                return Con(("s", p.literal.value))
            value = ast.literal_int(p.literal)
            return Con(("i", value, value))
        case "range":
            lo, hi = ast.range_bounds(p)
            return Con(("i", lo, hi))
        case "enum":
            return Con(("v", p.name), tuple(lower(s) for s in p.subs))
        case "some" | "none" | "ok" | "err":
            return Con(("v", p.kind), tuple(lower(s) for s in p.subs))
        case "tuple":
            return Con(("t",), tuple(lower(s) for s in p.subs))
        case "struct":
            return Con(("st",), tuple(None if s is None else lower(s) for s in p.slots))
        case "at":
            return lower(p.subs[0])
        case "or":
            return Alt(tuple(lower(s) for s in p.subs))
    raise ValueError(f"unknown pattern kind {p.kind!r}")


def show_interval(lo: int, hi: int) -> str:
    """Write an integer interval as a pattern."""
    if lo == INT_MIN and hi == INT_MAX:
        return "_"
    if lo == hi:
        return str(lo)
    if lo == INT_MIN:
        return f"..={hi}"
    if hi == INT_MAX:
        return f"{lo}.."
    return f"{lo}..={hi}"


def covers(head: Ctor, c: Ctor) -> bool:
    """Whether a row whose pattern starts with constructor `head` matches every value of constructor `c`."""
    if c[0] == "i":
        return head[0] == "i" and head[1] <= c[1] and c[2] <= head[2]
    return head == c


class Space:
    """The usefulness algorithm, for the enums of one program.

    Args:
        variants: Gives the data types of each variant of an enum type, by variant name.
        structs: Gives the field types of a struct type, by field name in declaration order.
    """

    def __init__(self, variants: Callable[[EnumT], dict[str, list[Type]]],
                 structs: Callable[[StructT], dict[str, Type]] = lambda ty: {}):
        """Remember how to look up the variants of an enum type and the fields of a struct type."""
        self.variants = variants
        self.structs = structs

    # ---- what a type is made of ------------------------------------------------------------

    def fields(self, ty: Type, c: Ctor) -> list[Type]:
        """The types of the sub-patterns of constructor `c` of type `ty`."""
        if c[0] == "v":
            if isinstance(ty, EnumT):
                return list(self.variants(ty)[c[1]])
            if isinstance(ty, OptionT):
                return [ty.inner] if c[1] == "some" else []
            if isinstance(ty, ResultT):
                return [ty.ok if c[1] == "ok" else ty.err]
        if c[0] == "t":
            return list(ty.elems)
        if c[0] == "st":
            return list(self.structs(ty).values())
        return []

    def universe(self, ty: Type) -> list[Ctor] | None:
        """Every constructor of a type with finitely many, or None for `int`, `str` and the types a pattern cannot take apart."""
        if isinstance(ty, EnumT):
            return [("v", n) for n in self.variants(ty)]
        if isinstance(ty, OptionT):
            return [("v", "some"), ("v", "none")]
        if isinstance(ty, ResultT):
            return [("v", "ok"), ("v", "err")]
        if isinstance(ty, TupleT):
            return [("t",)]
        if isinstance(ty, StructT):
            return [("st",)]
        if ty == BOOL:
            return [("b", True), ("b", False)]
        return None

    @staticmethod
    def atoms(heads: list[Ctor]) -> list[Ctor]:
        """The disjoint intervals that the ends of the integer intervals in `heads` cut the whole `int` range into."""
        cuts = {INT_MIN, INT_MAX + 1}
        for h in heads:
            cuts.update((max(h[1], INT_MIN), min(h[2], INT_MAX) + 1))
        ordered = sorted(cuts)
        return [("i", a, b - 1) for a, b in zip(ordered, ordered[1:])]

    def signature(self, ty: Type, heads: list[Ctor]) -> tuple[list[Ctor], bool]:
        """Split a column by the constructors its patterns use.

        Args:
            ty: The type of the column.
            heads: The constructors the column's patterns start with.

        Returns:
            The constructors to try and whether they are all of them: when they are (the column is *complete*) every
            one must be tried; otherwise the ones listed are those no pattern mentions, and they all behave like a
            wildcard.
        """
        if ty == INT:
            atoms = self.atoms(heads)
            uncovered = [a for a in atoms if not any(covers(h, a) for h in heads)]
            return (atoms, True) if not uncovered else (uncovered, False)
        universe = self.universe(ty)
        if universe is None:  # `str` and every type that cannot be taken apart (lists, ...): never complete
            return [("*",)], False
        missing = [c for c in universe if c not in heads]
        return (universe, True) if not missing else (missing, False)

    # ---- the matrix ------------------------------------------------------------------------

    @staticmethod
    def flatten(rows: list[list[Pat]]) -> list[list[Pat]]:
        """Replace each row that starts with an or-pattern by one row per alternative."""
        out: list[list[Pat]] = []
        for r in rows:
            if isinstance(r[0], Alt):
                out.extend(Space.flatten([[a, *r[1:]] for a in r[0].alts]))
            else:
                out.append(r)
        return out

    @staticmethod
    def specialize(rows: list[list[Pat]], c: Ctor, arity: int) -> list[list[Pat]]:
        """The rows that can match constructor `c`, with its sub-patterns in place of their first column."""
        out = []
        for r in rows:
            h = r[0]
            if h is None:
                out.append([*([None] * arity), *r[1:]])
            elif covers(h.ctor, c):
                out.append([*(h.subs if c[0] != "i" else ()), *r[1:]])
        return out

    def useful(self, rows: list[list[Pat]], q: list[Pat], tys: list[Type]) -> bool:
        """Whether `q` matches a value that no row of `rows` matches."""
        if not tys:
            return not rows
        rows = self.flatten(rows)
        head, ty, rest = q[0], tys[0], tys[1:]
        if isinstance(head, Alt):
            return any(self.useful(rows, [a, *q[1:]], tys) for a in head.alts)
        heads = [r[0].ctor for r in rows if r[0] is not None]
        if head is None:
            ctors, complete = self.signature(ty, heads)
            if not complete:
                return self.useful([r[1:] for r in rows if r[0] is None], q[1:], rest)
            for c in ctors:
                fields = self.fields(ty, c)
                if self.useful(self.specialize(rows, c, len(fields)), [*([None] * len(fields)), *q[1:]], [*fields, *rest]):
                    return True
            return False
        if head.ctor[0] == "i":
            parts = [(a, ()) for a in self.atoms([*heads, head.ctor]) if covers(head.ctor, a)]
        else:
            parts = [(head.ctor, head.subs)]
        for c, subs in parts:
            fields = self.fields(ty, c)
            if self.useful(self.specialize(rows, c, len(fields)), [*subs, *q[1:]], [*fields, *rest]):
                return True
        return False

    def missing(self, rows: list[list[Pat]], tys: list[Type]) -> list[list[str]] | None:
        """Values the rows do not match, as lists of patterns (one per column), or None when they match everything.

        At most `LIMIT` are returned; each is a pattern with `_` for the parts that do not matter.
        """
        if not tys:
            return [[]] if not rows else None
        rows = self.flatten(rows)
        ty, rest = tys[0], tys[1:]
        heads = [r[0].ctor for r in rows if r[0] is not None]
        ctors, complete = self.signature(ty, heads)
        if not complete:
            sub = self.missing([r[1:] for r in rows if r[0] is None], rest)
            if sub is None:
                return None
            return [[self.show(ty, c, ["_"] * len(self.fields(ty, c))), *sub[0]] for c in ctors][:LIMIT]
        found: list[list[str]] = []
        for c in ctors:
            fields = self.fields(ty, c)
            sub = self.missing(self.specialize(rows, c, len(fields)), [*fields, *rest])
            for w in sub or []:
                found.append([self.show(ty, c, w[:len(fields)]), *w[len(fields):]])
            if len(found) >= LIMIT:
                break
        return found[:LIMIT] or None

    def show(self, ty: Type, c: Ctor, subs: list[str]) -> str:
        """Write constructor `c` of type `ty`, applied to already written sub-patterns, as a pattern."""
        match c[0]:
            case "v":
                if isinstance(ty, EnumT):  # `Shape.Circle`, not `Shape.Circle(_)`, when what it carries does not matter
                    return f"{ty}.{c[1]}" + (f"({', '.join(subs)})" if any(x != "_" for x in subs) else "")
                return c[1] + (f"({', '.join(subs)})" if subs else "")
            case "t":
                return f"({', '.join(subs)})"
            case "st":  # only the fields that matter: `Point(x=0)`
                shown = [f"{n}={x}" for n, x in zip(self.structs(ty), subs) if x != "_"]
                return f"{ty}({', '.join(shown)})"
            case "b":
                return "true" if c[1] else "false"
            case "i":
                return show_interval(c[1], c[2])
        return "_"
