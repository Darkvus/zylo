"""Types of the Zylo language, as immutable value objects.

Two types are the same when they are equal, so the checker can compare them with `==`. `str()` of a type gives
the spelling used in Zylo source and in error messages (`list[int]`, `int?`, `Result[int, str]`).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Prim:
    """A primitive type: `int`, `float`, `bool`, `str` or the type of "no value".

    Attributes:
        name: The type's name.
    """
    name: str

    def __str__(self) -> str:
        """Return the primitive's name."""
        return self.name


@dataclass(frozen=True)
class ListT:
    """The type `list[T]`.

    Attributes:
        elem: The element type.
    """
    elem: "Type"

    def __str__(self) -> str:
        """Return the type as `list[T]`."""
        return f"list[{self.elem}]"


@dataclass(frozen=True)
class StructT:
    """The type of a user-defined struct.

    Attributes:
        name: The struct's name (mangled `module·Name` when it comes from another module). An instance of a generic
            struct is named after its arguments (`Box[int]`), so two instances are the same type exactly when
            their names are equal.
        args: The type arguments of an instance of a generic struct (empty for an ordinary struct); not compared.
        base: The generic struct this is an instance of (`Box`), or "" for an ordinary struct; not compared.
    """
    name: str
    args: tuple["Type", ...] = field(default=(), compare=False)
    base: str = field(default="", compare=False)

    def __str__(self) -> str:
        """Return the struct's name."""
        return self.name


@dataclass(frozen=True)
class EnumT:
    """The type of a user-defined enum.

    Attributes:
        name: The enum's name (mangled `module·Name` when it comes from another module); `Opt[int]` for an instance
            of a generic enum.
        args: The type arguments of an instance of a generic enum (empty otherwise); not compared.
        base: The generic enum this is an instance of, or "" for an ordinary enum; not compared.
    """
    name: str
    args: tuple["Type", ...] = field(default=(), compare=False)
    base: str = field(default="", compare=False)

    def __str__(self) -> str:
        """Return the enum's name."""
        return self.name


@dataclass(frozen=True)
class OptionT:
    """The type `T?`: either a `T` or nothing.

    Attributes:
        inner: The type of the value that may be missing.
    """
    inner: "Type"

    def __str__(self) -> str:
        """Return the type as `T?`."""
        return f"{self.inner}?"


@dataclass(frozen=True)
class ResultT:
    """The type `Result[T, E]`: either a `T` or an error `E`.

    Attributes:
        ok: The type of a success value.
        err: The type of an error value.
    """
    ok: "Type"
    err: "Type"

    def __str__(self) -> str:
        """Return the type as `Result[T, E]`."""
        return f"Result[{self.ok}, {self.err}]"


@dataclass(frozen=True)
class TupleT:
    """The type `tuple[A, B, ...]`.

    Attributes:
        elems: The element types, in order (at least two).
    """
    elems: tuple["Type", ...]

    def __str__(self) -> str:
        """Return the type as `tuple[A, B, ...]`."""
        return f"tuple[{', '.join(map(str, self.elems))}]"


@dataclass(frozen=True)
class DictT:
    """The type `dict[K, V]`.

    Attributes:
        key: The key type (int, str, bool or a tuple of those).
        val: The value type.
    """
    key: "Type"
    val: "Type"

    def __str__(self) -> str:
        """Return the type as `dict[K, V]`."""
        return f"dict[{self.key}, {self.val}]"


@dataclass(frozen=True)
class SetT:
    """The type `set[T]`.

    Attributes:
        elem: The element type (int, str, bool or a tuple of those).
    """
    elem: "Type"

    def __str__(self) -> str:
        """Return the type as `set[T]`."""
        return f"set[{self.elem}]"


@dataclass(frozen=True)
class CellT:
    """The type `Cell[T]`: a shared, mutable box holding one `T`.

    Copies of a cell (a closure capturing it, a struct field, an argument) all share the same storage.

    Attributes:
        inner: The type of the value in the cell.
    """
    inner: "Type"

    def __str__(self) -> str:
        """Return the type as `Cell[T]`."""
        return f"Cell[{self.inner}]"


@dataclass(frozen=True)
class FnT:
    """The type of a function.

    Attributes:
        params: Parameter types.
        ret: Return type.
    """
    params: tuple["Type", ...]
    ret: "Type"

    def __str__(self) -> str:
        """Return the type as `fn(A, B) -> R` (`fn(A, B)` when nothing is returned)."""
        ret = "" if self.ret == UNIT else f" -> {self.ret}"
        return f"fn({', '.join(map(str, self.params))}){ret}"


@dataclass(frozen=True)
class ParamT:
    """A type parameter of a generic definition, while the checker checks that definition once for all its types.

    Attributes:
        name: The parameter's name (`T`).
        bounds: The bounds the parameter was declared with (`Eq`, `Ord`, `Num`, `Show`, `Key`): what it is known to support.
    """
    name: str
    bounds: frozenset[str] = frozenset()

    def __str__(self) -> str:
        """Return the parameter's name."""
        return self.name


INT = Prim("int")
FLOAT = Prim("float")
BOOL = Prim("bool")
STR = Prim("str")
UNIT = Prim("unit")

Type = Prim | ListT | FnT | StructT | EnumT | OptionT | ResultT | TupleT | DictT | SetT | CellT | ParamT

PRIMS = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR}


@dataclass
class VarInfo:
    """A variable as the checker resolved it.

    Attributes:
        name: The variable's name.
        ty: Its type.
        mutable: True if the variable is ever reassigned or modified; the generator then emits `let mut`.
        narrowed: True for a variable rebound with a narrower type by an `is` check; it cannot be reassigned.
        by_ref: True for a parameter its function never modifies: it is lent to the function instead of cloned.
        captured: True for a read-only copy of an outer variable inside a closure; it cannot be modified there.
        place_mut: True if a part of the variable is modified in place (`x.f = v`, `x[i] = v`, `x.push(v)`).
        uses: Ids of the syntax nodes that mention the variable.
        mut_roots: Ids of the nodes that only name the variable as the root of a modified place.
    """
    name: str
    ty: Type
    mutable: bool = False
    narrowed: bool = False  # rebound by `is` narrowing; cannot be reassigned
    by_ref: bool = False  # a parameter the function never modifies: passed by reference, not cloned
    captured: bool = False  # a copy of an outer variable inside a closure: read-only
    place_mut: bool = False  # a part of it is modified in place
    uses: set[int] = field(default_factory=set)  # nodes that mention it
    mut_roots: set[int] = field(default_factory=set)  # nodes that mention it only to modify a part of it


def is_copy_type(t: Type) -> bool:
    """Types that are `Copy` in the generated Rust (cheap to duplicate, never need `.clone()`)."""
    if isinstance(t, TupleT):
        return all(is_copy_type(x) for x in t.elems)
    if isinstance(t, OptionT):
        return is_copy_type(t.inner)
    if isinstance(t, ResultT):
        return is_copy_type(t.ok) and is_copy_type(t.err)
    return t in (INT, FLOAT, BOOL)
