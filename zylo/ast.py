from dataclasses import dataclass

from .types import Type, VarInfo


@dataclass(kw_only=True)
class Node:
    line: int = 0
    col: int = 0
    ty: Type | None = None  # filled in by the checker for expressions


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
        | MethodCall | NoneLit | IsCheck | Try)


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


Stmt = ExprStmt | Assign | If | While | For | Return | Break | Continue | FnDef | StructDef


@dataclass
class Program(Node):
    body: list[Stmt]
