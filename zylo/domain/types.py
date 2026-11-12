"""Types of the Zylo language, as immutable value objects.

Two types are the same when they are equal, so the checker can compare them with `==`. `str()` of a type gives
the spelling used in Zylo source and in error messages (`list[int]`, `int?`, `Result[int, str]`).
"""

from dataclasses import dataclass


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
        name: The struct's name (mangled `module·Name` when it comes from another module).
    """
    name: str

    def __str__(self) -> str:
        """Return the struct's name."""
        return self.name


@dataclass(frozen=True)
class EnumT:
    """The type of a user-defined enum.

    Attributes:
        name: The enum's name (mangled `module·Name` when it comes from another module).
    """
    name: str

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


INT = Prim("int")
FLOAT = Prim("float")
BOOL = Prim("bool")
STR = Prim("str")
UNIT = Prim("unit")

Type = Prim | ListT | FnT | StructT | EnumT | OptionT | ResultT | TupleT | DictT | SetT

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
    """
    name: str
    ty: Type
    mutable: bool = False
    narrowed: bool = False  # rebound by `is` narrowing; cannot be reassigned
    by_ref: bool = False  # a parameter the function never modifies: passed by reference, not cloned
    captured: bool = False  # a copy of an outer variable inside a closure: read-only


def is_copy_type(t: Type) -> bool:
    """Types that are `Copy` in the generated Rust (cheap to duplicate, never need `.clone()`)."""
    if isinstance(t, TupleT):
        return all(is_copy_type(x) for x in t.elems)
    if isinstance(t, OptionT):
        return is_copy_type(t.inner)
    if isinstance(t, ResultT):
        return is_copy_type(t.ok) and is_copy_type(t.err)
    return t in (INT, FLOAT, BOOL)
