from dataclasses import dataclass


@dataclass(frozen=True)
class Prim:
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class ListT:
    elem: "Type"

    def __str__(self) -> str:
        return f"list[{self.elem}]"


@dataclass(frozen=True)
class StructT:
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class OptionT:
    inner: "Type"

    def __str__(self) -> str:
        return f"{self.inner}?"


@dataclass(frozen=True)
class ResultT:
    ok: "Type"
    err: "Type"

    def __str__(self) -> str:
        return f"Result[{self.ok}, {self.err}]"


@dataclass(frozen=True)
class TupleT:
    elems: tuple["Type", ...]

    def __str__(self) -> str:
        return f"tuple[{', '.join(map(str, self.elems))}]"


@dataclass(frozen=True)
class DictT:
    key: "Type"
    val: "Type"

    def __str__(self) -> str:
        return f"dict[{self.key}, {self.val}]"


@dataclass(frozen=True)
class SetT:
    elem: "Type"

    def __str__(self) -> str:
        return f"set[{self.elem}]"


@dataclass(frozen=True)
class FnT:
    params: tuple["Type", ...]
    ret: "Type"

    def __str__(self) -> str:
        return f"fn({', '.join(map(str, self.params))}) -> {self.ret}"


INT = Prim("int")
FLOAT = Prim("float")
BOOL = Prim("bool")
STR = Prim("str")
UNIT = Prim("unit")

Type = Prim | ListT | FnT | StructT | OptionT | ResultT | TupleT | DictT | SetT

PRIMS = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR}


@dataclass
class VarInfo:
    """Una variable resuelta. `mutable` lo marca el checker si se reasigna o muta;
    el codegen lo usa para emitir `let mut`."""
    name: str
    ty: Type
    mutable: bool = False
    narrowed: bool = False  # rebound by `is` narrowing; cannot be reassigned
