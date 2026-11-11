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

Type = Prim | ListT | FnT | StructT

PRIMS = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR}


@dataclass
class VarInfo:
    """Una variable resuelta. `mutable` lo marca el checker si se reasigna o muta;
    el codegen lo usa para emitir `let mut`."""
    name: str
    ty: Type
    mutable: bool = False
