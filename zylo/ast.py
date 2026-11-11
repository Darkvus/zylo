import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass

from .types import Type, VarInfo


@dataclass(kw_only=True)
class Node:
    line: int = 0
    col: int = 0
    ty: Type | None = None  # filled in by the checker for expressions
    file: str | None = None  # source file, so errors in imported modules point at the right file


# ---- types written by the user -------------------------------------------

@dataclass
class TypeRef(Node):
    name: str  # int float bool str list
    args: list["TypeRef"]


# ---- expresiones ---------------------------------------------------------------

@dataclass
class Int(Node):
    value: int


@dataclass
class Float(Node):
    value: float


@dataclass
class Str(Node):
    value: str


@dataclass
class Bool(Node):
    value: bool


@dataclass
class Name(Node):
    id: str
    var: VarInfo | None = None  # filled in by the checker: the variable this name refers to


@dataclass
class BinOp(Node):
    op: str  # + - * / % == != < > <= >= and or
    left: "Expr"
    right: "Expr"


@dataclass
class UnaryOp(Node):
    op: str  # - not
    operand: "Expr"


@dataclass
class Call(Node):
    func: str
    args: list["Expr"]
    ctor_fields: list[str] | None = None  # filled in by the checker if this is a struct constructor


@dataclass
class ListLit(Node):
    items: list["Expr"]


@dataclass
class Index(Node):
    value: "Expr"
    index: "Expr"


@dataclass
class TupleLit(Node):
    items: list["Expr"]


@dataclass
class DictLit(Node):
    keys: list["Expr"]
    values: list["Expr"]


@dataclass
class SetLit(Node):
    items: list["Expr"]


@dataclass
class Slice(Node):
    """`xs[lo:hi]`; either bound may be missing."""
    value: "Expr"
    lo: "Expr | None"
    hi: "Expr | None"


@dataclass
class FormatPart(Node):
    """One `{expr:spec}` replacement field of an f-string."""
    value: "Expr"
    spec: str


@dataclass
class FString(Node):
    """`f"text {expr} text"`: `parts` mixes literal `str`s and FormatParts."""
    parts: list


@dataclass
class Attr(Node):
    """Acceso a un campo: `p.x`."""
    value: "Expr"
    name: str


@dataclass
class MethodCall(Node):
    """Method call: `p.move(1.0)`."""
    value: "Expr"
    name: str
    args: list["Expr"]


@dataclass
class NoneLit(Node):
    """The `none` literal; its Option type comes from context."""


@dataclass
class IsCheck(Node):
    """`x is some` / `x is none` / `x is ok` / `x is err`."""
    value: "Expr"
    kind: str


@dataclass
class Try(Node):
    """Postfix `?`: unwrap an Option/Result or return early."""
    value: "Expr"


Expr = (Int | Float | Str | Bool | Name | BinOp | UnaryOp | Call | ListLit | Index | Attr
        | MethodCall | NoneLit | IsCheck | Try | FString | TupleLit | DictLit | SetLit | Slice)


# ---- sentencias ----------------------------------------------------------------

@dataclass
class ExprStmt(Node):
    expr: Expr


@dataclass
class Assign(Node):
    """`x = v`, `x: T = v`, `xs[i] = v`. Declarar o reasignar lo decide el chequeo de tipos."""
    target: Name | Index | Attr
    annotation: TypeRef | None
    value: Expr
    declares: bool = False  # filled in by the checker: True if this is a declaration
    var: VarInfo | None = None


@dataclass
class If(Node):
    cond: Expr
    then: list["Stmt"]
    else_: list["Stmt"]  # empty if there is no else; `elif` is a nested If
    then_bind: VarInfo | None = None   # set by the checker: `x` narrowed inside the then-branch
    else_bind: VarInfo | None = None   # ... inside the else-branch
    after_bind: VarInfo | None = None  # ... for the statements after an early-exit `if`


@dataclass
class While(Node):
    cond: Expr
    body: list["Stmt"]


@dataclass
class For(Node):
    var: str
    iter: Expr
    body: list["Stmt"]
    info: VarInfo | None = None
    targets: list[str] | None = None  # `for a, b in ...`: unpack each element (var is then unused)
    infos: list[VarInfo] | None = None


@dataclass
class Unpack(Node):
    """`a, b = value`: every target is declared or reassigned (filled in by the checker)."""
    targets: list[Name]
    value: Expr
    declares: list[bool] = dataclasses.field(default_factory=list)
    vars: list[VarInfo] = dataclasses.field(default_factory=list)


@dataclass
class Return(Node):
    value: Expr | None


@dataclass
class Break(Node):
    pass


@dataclass
class Continue(Node):
    pass


@dataclass
class Param(Node):
    name: str
    type: TypeRef
    info: VarInfo | None = None


@dataclass
class FnDef(Node):
    name: str
    params: list[Param]
    ret: TypeRef | None  # None = no return value
    body: list["Stmt"]
    has_self: bool = False  # method (first parameter is `self`)
    self_mut: bool = False  # filled in by the checker: the method modifies `self`


@dataclass
class Field(Node):
    name: str
    type: TypeRef


@dataclass
class StructDef(Node):
    name: str
    fields: list[Field]
    methods: list[FnDef]
    display: str | None = None  # original name when the linker has mangled `name`


@dataclass
class Import(Node):
    """`import a.b` / `import a.b as c`."""
    module: str
    alias: str | None


@dataclass
class FromImport(Node):
    """`from a.b import x, y as z`; names are (name, alias) pairs."""
    module: str
    names: list[tuple[str, str | None]]


Stmt = (ExprStmt | Assign | Unpack | If | While | For | Return | Break | Continue | FnDef | StructDef
        | Import | FromImport)


@dataclass
class Program(Node):
    body: list[Stmt]


# ---- generic traversal -----------------------------------------------------------------------

_ANNOTATIONS = {"ty", "var", "vars", "info", "infos", "then_bind", "else_bind", "after_bind"}


def children(node: Node) -> Iterator[Node]:
    """Direct child nodes (skipping checker annotations such as types and variable infos)."""
    for f in dataclasses.fields(node):
        if f.name in _ANNOTATIONS:
            continue
        v = getattr(node, f.name)
        if isinstance(v, Node):
            yield v
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, Node):
                    yield x


def walk(node: Node) -> Iterator[Node]:
    yield node
    for c in children(node):
        yield from walk(c)


def stamp(node: Node, file: str) -> None:
    for n in walk(node):
        n.file = file
