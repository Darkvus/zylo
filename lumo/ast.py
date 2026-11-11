from dataclasses import dataclass


@dataclass(kw_only=True)
class Node:
    line: int = 0
    col: int = 0


# ---- tipos escritos por el usuario -------------------------------------------

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


@dataclass
class ListLit(Node):
    items: list["Expr"]


@dataclass
class Index(Node):
    value: "Expr"
    index: "Expr"


Expr = Int | Float | Str | Bool | Name | BinOp | UnaryOp | Call | ListLit | Index


# ---- sentencias ----------------------------------------------------------------

@dataclass
class ExprStmt(Node):
    expr: Expr


@dataclass
class Assign(Node):
    """`x = v`, `x: T = v`, `xs[i] = v`. Declarar o reasignar lo decide el chequeo de tipos."""
    target: Name | Index
    annotation: TypeRef | None
    value: Expr


@dataclass
class If(Node):
    cond: Expr
    then: list["Stmt"]
    else_: list["Stmt"]  # vacío si no hay else; `elif` es un If anidado


@dataclass
class While(Node):
    cond: Expr
    body: list["Stmt"]


@dataclass
class For(Node):
    var: str
    iter: Expr
    body: list["Stmt"]


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


@dataclass
class FnDef(Node):
    name: str
    params: list[Param]
    ret: TypeRef | None  # None = sin valor de retorno
    body: list["Stmt"]


Stmt = ExprStmt | Assign | If | While | For | Return | Break | Continue | FnDef


@dataclass
class Program(Node):
    body: list[Stmt]
