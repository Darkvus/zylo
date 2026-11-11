"""Syntax tree of Zylo programs.

Every node is a dataclass that records where it came from (`line`, `col`, `file`) and, once the checker has run,
its type (`ty`). Expressions and statements are grouped in the `Expr` and `Stmt` unions. The checker and the linker
also leave annotations on nodes (resolved variables, whether a name is declared or reassigned, borrow decisions);
`children` and `walk` skip those annotations and visit only real sub-nodes.
"""

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass

from zylo.domain.types import Type, VarInfo


@dataclass(kw_only=True)
class Node:
    """Base of every syntax node.

    Attributes:
        line: 1-based source line (0 when unknown).
        col: 1-based source column (0 when unknown).
        ty: The node's type; filled in by the checker for expressions.
        file: Source file the node was parsed from, so errors in imported modules name the right file.
    """
    line: int = 0
    col: int = 0
    ty: Type | None = None  # filled in by the checker for expressions
    file: str | None = None  # source file, so errors in imported modules point at the right file


# ---- types written by the user -------------------------------------------

@dataclass
class TypeRef(Node):
    """A type exactly as the user wrote it, before it is resolved.

    Attributes:
        name: `int`, `list`, `Result`, a struct name, `mod.Struct`, or `?` for an optional type.
        args: Type arguments (`list[int]` has one, `Result[T, E]` has two, `T?` has one).
    """
    name: str  # int float bool str list
    args: list["TypeRef"]


# ---- expressions ---------------------------------------------------------------

@dataclass
class Int(Node):
    """An integer literal."""
    value: int


@dataclass
class Float(Node):
    """A floating-point literal."""
    value: float


@dataclass
class Str(Node):
    """A string literal, with its escapes already processed."""
    value: str


@dataclass
class Bool(Node):
    """The literal `true` or `false`."""
    value: bool


@dataclass
class Name(Node):
    """A reference to a variable.

    Attributes:
        id: The variable's name.
        var: The variable this name refers to; filled in by the checker.
    """
    id: str
    var: VarInfo | None = None  # filled in by the checker: the variable this name refers to


@dataclass
class BinOp(Node):
    """A binary operation such as `a + b`, `a and b`, `x in xs` or `x not in xs`.

    Attributes:
        op: The operator as written (`+`, `==`, `and`, `in`, `not in`...).
        left: Left operand.
        right: Right operand.
    """
    op: str  # + - * / % == != < > <= >= and or
    left: "Expr"
    right: "Expr"


@dataclass
class UnaryOp(Node):
    """A prefix operation: negation (`-x`) or `not x`.

    Attributes:
        op: `-` or `not`.
        operand: The operand.
    """
    op: str  # - not
    operand: "Expr"


@dataclass
class Call(Node):
    """A call of a function, a built-in, or a struct constructor.

    Attributes:
        func: Name of the callee (mangled `module·name` after linking).
        args: Argument expressions.
        ctor_fields: Field names when the checker found this is a struct constructor, otherwise None.
    """
    func: str
    args: list["Expr"]
    ctor_fields: list[str] | None = None  # filled in by the checker if this is a struct constructor


@dataclass
class ListLit(Node):
    """A list literal such as `[1, 2, 3]`."""
    items: list["Expr"]


@dataclass
class Index(Node):
    """An index expression `value[index]` on a list, string, tuple or dict.

    Attributes:
        value: The indexed expression.
        index: The index or key expression.
    """
    value: "Expr"
    index: "Expr"


@dataclass
class TupleLit(Node):
    """A tuple literal such as `(1, "a")`; always has at least two items."""
    items: list["Expr"]


@dataclass
class DictLit(Node):
    """A dict literal `{k: v, ...}`; the empty `{}` is typed by context and may be a set.

    Attributes:
        keys: Key expressions.
        values: Value expressions, parallel to `keys`.
    """
    keys: list["Expr"]
    values: list["Expr"]


@dataclass
class SetLit(Node):
    """A set literal such as `{1, 2, 3}`."""
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


# ---- statements ----------------------------------------------------------------

@dataclass
class ExprStmt(Node):
    """An expression evaluated for its effect, such as a call."""
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
    """An `if` / `elif` / `else` statement (an `elif` is a nested `If` in `else_`).

    Attributes:
        cond: The condition.
        then: Statements of the `if` branch.
        else_: Statements of the `else` branch; empty when there is none.
        then_bind: Variable narrowed by an `is` check inside the then-branch (set by the checker).
        else_bind: Variable narrowed inside the else-branch.
        after_bind: Variable narrowed for the statements after an early-exit `if`.
    """
    cond: Expr
    then: list["Stmt"]
    else_: list["Stmt"]  # empty if there is no else; `elif` is a nested If
    then_bind: VarInfo | None = None   # set by the checker: `x` narrowed inside the then-branch
    else_bind: VarInfo | None = None   # ... inside the else-branch
    after_bind: VarInfo | None = None  # ... for the statements after an early-exit `if`


@dataclass
class While(Node):
    """A `while` loop."""
    cond: Expr
    body: list["Stmt"]


@dataclass
class For(Node):
    """A `for` loop over a range, list, string, set or dict.

    Attributes:
        var: Loop variable (the first one when unpacking).
        iter: The iterated expression.
        body: Loop body.
        info: The checker's record of the loop variable.
        targets: Variable names when the loop unpacks tuples (`for a, b in ...`), otherwise None.
        infos: The checker's records of those variables.
    """
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
    """A `return` statement, with an optional value."""
    value: Expr | None


@dataclass
class Break(Node):
    """A `break` statement."""
    pass


@dataclass
class Continue(Node):
    """A `continue` statement."""
    pass


@dataclass
class Param(Node):
    """A function parameter.

    Attributes:
        name: Parameter name.
        type: Its declared type.
        info: The checker's record of the variable; says whether the function modifies it or may borrow it.
    """
    name: str
    type: TypeRef
    info: VarInfo | None = None


@dataclass
class FnDef(Node):
    """A function or method definition.

    Attributes:
        name: Function name.
        params: Parameters (excluding `self`).
        ret: Declared return type, or None for no value.
        body: Statements of the body.
        has_self: True for a method (its first parameter is `self`).
        self_mut: True when the checker found that the method modifies `self`.
    """
    name: str
    params: list[Param]
    ret: TypeRef | None  # None = no return value
    body: list["Stmt"]
    has_self: bool = False  # method (first parameter is `self`)
    self_mut: bool = False  # filled in by the checker: the method modifies `self`


@dataclass
class ExternFn(FnDef):
    """`extern fn f(a: int) -> int = "rust::path"`: a function whose body is Rust (see docs/rust-interop.md)."""
    template: str = ""


@dataclass
class Field(Node):
    """One field of a struct definition (`name: type`)."""
    name: str
    type: TypeRef


@dataclass
class StructDef(Node):
    """A struct definition with its fields and methods.

    Attributes:
        name: Struct name (mangled after linking).
        fields: Declared fields, in order.
        methods: Methods defined inside the struct.
        display: Original name to show to users when `name` was mangled.
    """
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
    """A whole program: the statements of one module, or of all modules after linking."""
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
    """Visit a node and all of its descendants, depth first.

    Args:
        node: Where to start.

    Yields:
        `node` first, then every nested node.
    """
    yield node
    for c in children(node):
        yield from walk(c)


def stamp(node: Node, file: str) -> None:
    """Record the source file on a node and on all of its descendants.

    Args:
        node: Root of the tree.
        file: The file the tree was parsed from.
    """
    for n in walk(node):
        n.file = file
