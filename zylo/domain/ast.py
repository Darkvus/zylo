"""Syntax tree of Zylo programs.

Every node is a dataclass that records where it came from (`line`, `col`, `file`) and, once the checker has run,
its type (`ty`). Expressions and statements are grouped in the `Expr` and `Stmt` unions. The checker and the linker
also leave annotations on nodes (resolved variables, whether a name is declared or reassigned, borrow decisions);
`children` and `walk` skip those annotations and visit only real sub-nodes.
"""

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field

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
        args: Type arguments (`list[int]` has one, `Result[T, E]` has two, `T?` has one, `Box[int]` has one). For a
            function type (`fn(A, B) -> R`, name `fn`) they are the parameter types.
        ret: The return type of a function type, or None when it returns nothing (or for any other type).
        fixed: Filled in by the checker on a copy of a generic definition: the type that a type parameter stands for there.
    """
    name: str  # int float bool str list
    args: list["TypeRef"]
    ret: "TypeRef | None" = None
    fixed: Type | None = None  # set by the checker on a copy of a generic definition: the type this written type stands for


# ---- expressions ---------------------------------------------------------------

@dataclass
class TypeParam(Node):
    """A type parameter of a generic function, struct or enum: `T` or `T: Ord + Show`.

    Attributes:
        name: The parameter's name.
        bounds: Names of the bounds it must satisfy: the built-in ones (`Eq`, `Ord`, `Num`, `Show`, `Key`) or traits
            (`Shape`, `shapes.Shape`), possibly none. The linker rewrites a trait's name to its linked name.
        default: The type used when nothing decides the parameter (`T = int`), or None.
    """
    name: str
    bounds: list[str] = field(default_factory=list)
    default: "TypeRef | None" = None


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
        targs: Explicit type arguments written on a generic enum (`Opt[int].Some(1)`) or on a generic function used as a
            value (`first[int]`), otherwise empty.
        inst: The instance of a generic function this name stands for when it is used as a value, or None.
    """
    id: str
    var: VarInfo | None = None  # filled in by the checker: the variable this name refers to
    targs: list[TypeRef] = field(default_factory=list)
    inst: str | None = None  # filled in by the checker: the instance of the generic function used as a value


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
class NamedArg(Node):
    """A named argument `name=value` in a call.

    Attributes:
        name: The parameter (or struct field) it sets.
        value: The argument expression.
    """
    name: str
    value: "Expr"


@dataclass
class Call(Node):
    """A call of a function, a built-in, or a struct constructor.

    Attributes:
        func: Name of the callee (mangled `module·name` after linking).
        args: Argument expressions. After checking, one per parameter: named arguments and defaults are folded in.
        ctor_fields: Field names when the checker found this is a struct constructor, otherwise None.
        named: Named arguments as written; the checker moves them into `args` and empties this list.
        var: The variable of function type being called (`f(3)`), when the callee is a variable rather than a function.
        targs: Explicit type arguments of a generic function or struct (`first[int](xs)`), otherwise empty.
        inst: The instance the checker picked for a call of a generic function or struct (`first[int]`), or None.
    """
    func: str
    args: list["Expr"]
    ctor_fields: list[str] | None = None  # filled in by the checker if this is a struct constructor
    named: list[NamedArg] = field(default_factory=list)
    var: VarInfo | None = None  # filled in by the checker if the callee is a variable holding a function
    targs: list[TypeRef] = field(default_factory=list)
    inst: str | None = None  # filled in by the checker: the instance of a generic function or struct that is called


@dataclass
class CallValue(Node):
    """A call of any expression that is not a plain name: `handlers["GET"](req)`, `make()(1)`, `(fn(x) => x)(2)`.

    Attributes:
        callee: The expression that produces the function.
        args: Argument expressions.
    """
    callee: "Expr"
    args: list["Expr"]


@dataclass
class Lambda(Node):
    """An anonymous function `fn(x: int) => x * 2`, or a local `fn name(...)` with a block body.

    Attributes:
        params: Parameters; the type is None when it is left to the context.
        ret: Declared return type, or None to infer it (an expression body) or return nothing (a block body).
        expr: The body of the single-expression form, otherwise None.
        body: The statements of the block form (empty for the expression form).
        name: The function's name for a local `fn name(...)`, otherwise None.
        captures: `(outer, inner)` variable pairs: what the closure copies when it is created (set by the checker).
    """
    params: list["Param"]
    ret: "TypeRef | None"
    expr: "Expr | None"
    body: list["Stmt"] = field(default_factory=list)
    name: str | None = None
    captures: list = field(default_factory=list)


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
    """A tuple literal such as `(1, "a")`; always has at least two items.

    Attributes:
        items: The elements.
        bare: True for the list in `pick[point, int](xs)`, which is written without parentheses and which the linker
            turns into type arguments.
    """
    items: list["Expr"]
    bare: bool = False


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
    """Field access `p.x`, or a variant without data `Shape.Empty`.

    Attributes:
        value: The expression whose field is read.
        name: The field or variant name.
        variant: `(enum, variant)` when the checker found a variant without data, otherwise None.
        unwrap: `"unwrap"` / `"unwrap_err"` when an `is` check narrowed this field path to the inner value.
        targs: Explicit type arguments of a generic enum named through a module (`lib.Opt[int].Empty`), otherwise empty.
    """
    value: "Expr"
    name: str
    variant: tuple[str, str] | None = None
    unwrap: str | None = None
    targs: list[TypeRef] = field(default_factory=list)


@dataclass
class MethodCall(Node):
    """Method call `p.move(1.0)`, or a variant with data `Shape.Circle(2.0)`.

    Attributes:
        value: The receiver.
        name: The method or variant name.
        args: Argument expressions (completed with defaults by the checker, like `Call.args`).
        variant: `(enum, variant)` when the checker found a variant constructor, otherwise None.
        named: Named arguments as written; the checker moves them into `args` and empties this list.
        field_call: True when the checker found that `name` is a field of function type, not a method.
        targs: Explicit type arguments of a generic method (`b.map[str](f)`) or of a generic function named through a
            module (`lib.first[int](xs)`).
        inst: The instance of a generic method this call uses (`map[str]`), or None.
    """
    value: "Expr"
    name: str
    args: list["Expr"]
    variant: tuple[str, str] | None = None
    named: list[NamedArg] = field(default_factory=list)
    field_call: bool = False
    targs: list[TypeRef] = field(default_factory=list)
    inst: str | None = None  # filled in by the checker: the copy of a generic method that is called


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
        | MethodCall | NoneLit | IsCheck | Try | FString | TupleLit | DictLit | SetLit | Slice | CallValue | Lambda)


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
class Pass(Node):
    """A `pass` statement: does nothing, for a block (such as a `match` arm) that has nothing to do."""
    pass


@dataclass
class Param(Node):
    """A function parameter.

    Attributes:
        name: Parameter name.
        type: Its declared type (None only for an anonymous function's parameter whose type comes from the context).
        info: The checker's record of the variable; says whether the function modifies it or may borrow it.
        default: The default value (a constant expression), or None when the argument is required.
    """
    name: str
    type: TypeRef | None
    info: VarInfo | None = None
    default: "Expr | None" = None


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
        tparams: The type parameters of a generic function or method (`fn first[T](...)`); empty for an ordinary one.
        instance: True for a copy of a generic function or method made by the checker for one list of type arguments.
    """
    name: str
    params: list[Param]
    ret: TypeRef | None  # None = no return value
    body: list["Stmt"]
    has_self: bool = False  # method (first parameter is `self`)
    self_mut: bool = False  # filled in by the checker: the method modifies `self`
    tparams: list[TypeParam] = field(default_factory=list)
    instance: bool = False  # a copy made by the checker for one list of type arguments


@dataclass
class ExternFn(FnDef):
    """`extern fn f(a: int) -> int = "rust::path"`: a function whose body is Rust (see docs/rust-interop.md)."""
    template: str = ""


@dataclass
class Field(Node):
    """One field of a struct definition (`name: type`), with an optional default (`name: type = value`)."""
    name: str
    type: TypeRef
    default: "Expr | None" = None
    doc: str | None = None  # the string literal written right after the field
    doc_line: int = 0


@dataclass
class StructDef(Node):
    """A struct definition with its fields and methods.

    Attributes:
        name: Struct name (mangled after linking).
        fields: Declared fields, in order.
        methods: Methods defined inside the struct.
        display: Original name to show to users when `name` was mangled.
        doc: The description written as the first line of the body (a string literal), or None.
        doc_line: Source line of that description.
        tparams: The type parameters of a generic struct (`struct Box[T]`); empty for an ordinary one.
        instance: True for a copy of a generic struct made by the checker for one list of type arguments.
        traits: The traits the struct implements (`struct Circle(Shape):`), as written; the linker rewrites their names.
    """
    name: str
    fields: list[Field]
    methods: list[FnDef]
    display: str | None = None  # original name when the linker has mangled `name`
    doc: str | None = None
    doc_line: int = 0
    tparams: list[TypeParam] = field(default_factory=list)
    instance: bool = False
    traits: list[TypeRef] = field(default_factory=list)


@dataclass
class Variant(Node):
    """One variant of an enum: a name and the types of its data (none for a plain variant)."""
    name: str
    fields: list[TypeRef]
    tys: list = field(default_factory=list)  # resolved types, filled in by the checker
    doc: str | None = None  # the string literal written right after the variant
    doc_line: int = 0


@dataclass
class EnumDef(Node):
    """An enum definition: a value that is exactly one of several variants, each with its own data.

    Attributes:
        name: Enum name (mangled after linking).
        variants: The variants, in order.
        display: Original name to show to users when `name` was mangled.
        methods: Methods defined inside the enum; `self` is the enum value.
        doc: The description written as the first line of the body (a string literal), or None.
        doc_line: Source line of that description.
        tparams: The type parameters of a generic enum (`enum Opt[T]`); empty for an ordinary one.
        instance: True for a copy of a generic enum made by the checker for one list of type arguments.
        traits: The traits the enum implements (`enum Light(Describe):`), as written.
    """
    name: str
    variants: list[Variant]
    display: str | None = None
    methods: list[FnDef] = field(default_factory=list)
    doc: str | None = None
    doc_line: int = 0
    tparams: list[TypeParam] = field(default_factory=list)
    instance: bool = False
    traits: list[TypeRef] = field(default_factory=list)


@dataclass
class TraitDef(Node):
    """A trait: a named set of method signatures that structs and enums implement and generics use as a bound.

    Attributes:
        name: Trait name (mangled after linking).
        methods: The required methods: each a `FnDef` with `self` and no body.
        display: Original name to show to users when `name` was mangled.
        doc: The description written as the first line of the body (a string literal), or None.
        doc_line: Source line of that description.
    """
    name: str
    methods: list[FnDef]
    display: str | None = None
    doc: str | None = None
    doc_line: int = 0


@dataclass
class Pattern(Node):
    """One pattern of a `match` arm; patterns nest (`Shape.Circle(some(r))`, `ok((a, b))`, `some(1..=5)`).

    The kinds, in `kind`:

    - `wild`: `_`, matches anything and binds nothing.
    - `bind`: a name, matches anything and binds it (only inside another pattern).
    - `lit`: an `int`, `str` or `bool` literal (`literal`).
    - `range`: an integer range `lo..hi`, `lo..=hi`, `lo..`, `..hi` or `..=hi`; a missing bound is None.
    - `enum`: `Enum.Variant` or `Enum.Variant(p, ...)` (`type`, `name`, `subs`).
    - `some` / `none` / `ok` / `err`: the `Option` and `Result` patterns (`subs` holds the one inner pattern).
    - `tuple`: `(p, q, ...)` (`subs`).
    - `or`: `p | q` (`subs` holds the alternatives); inside brackets or at the top of an arm.
    - `struct`: `Point(x=0, y)`: some of a struct's fields, each matched by a pattern (`type`, `fields`, `subs`); a bare
      name is the shorthand for `name=name`.
    - `at`: `name @ pattern` matches what the pattern matches and binds the whole value to `name` (`name`, and the
      pattern as the only element of `subs`).

    A bare `bind` is also allowed at the top of an arm, where it catches every value that is left (`other:`).

    Attributes:
        kind: One of the kinds above.
        name: The bound name (`bind`, `at`) or the variant name (`enum`).
        type: The enum or struct as written (`Shape`, `mod.Shape`, `Opt[int]`) for an `enum` or `struct` pattern,
            otherwise None.
        subs: Sub-patterns: the data of a variant, the elements of a tuple, the alternatives of an `or`, the field
            patterns of a `struct`, the pattern after `@`.
        fields: The field names of a `struct` pattern, one per element of `subs`, in the order written.
        literal: The literal of a `lit` pattern.
        lo: The lower bound of a `range` (None when open).
        hi: The upper bound of a `range` (None when open).
        inclusive: Whether the upper bound of a `range` is included (`..=`).
        enum: The (mangled) enum or struct this pattern matches, set by the checker for an `enum` or `struct` pattern.
        slots: The sub-pattern of every field of the struct in declaration order (None for a field the pattern does
            not mention), set by the checker for a `struct` pattern.
        ty: (inherited) the type of the value this pattern matches, set by the checker.
    """
    kind: str
    name: str = ""
    type: TypeRef | None = None
    subs: list["Pattern"] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    literal: "Expr | None" = None
    lo: "Expr | None" = None
    hi: "Expr | None" = None
    inclusive: bool = False
    enum: str | None = None
    slots: list["Pattern | None"] | None = None


@dataclass
class MatchArm(Node):
    """One arm of a `match`: a pattern, an optional guard and a block.

    Attributes:
        pattern: The pattern; an `or` pattern for `1 | 2`.
        body: Statements of the arm.
        guard: The condition of `Pattern if cond:`, otherwise None.
        binds: The names the pattern binds, in order of first appearance (every alternative of an or-pattern binds
            the same names); set by the checker.
        vars: The variables for `binds` (set by the checker).
    """
    pattern: Pattern
    body: list["Stmt"]
    guard: "Expr | None" = None
    binds: list[str] = field(default_factory=list)
    vars: list = field(default_factory=list)


@dataclass
class Match(Node):
    """`match value:` on an enum, struct, `int`, `str`, `bool`, `Option`, `Result` or tuple; it must be exhaustive.

    Arms may carry a guard (`Pattern if cond:`), or-patterns (`1 | 2:`), nested patterns and integer ranges; a
    guarded arm does not count toward exhaustiveness.
    """
    value: "Expr"
    arms: list[MatchArm]
    enum: str | None = None  # the enum matched on, set by the checker


@dataclass
class Import(Node):
    """`import a.b` / `import a.b as c`."""
    module: str
    alias: str | None


@dataclass
class FromImport(Node):
    """`from a.b import x, y as z`; names are (name, alias) pairs. `pub from ...` also re-exports the names."""
    module: str
    names: list[tuple[str, str | None]]
    pub: bool = False


Stmt = (ExprStmt | Assign | Unpack | If | While | For | Return | Break | Continue | Pass | FnDef | StructDef
        | EnumDef | TraitDef | Match | Import | FromImport)


@dataclass
class Program(Node):
    """A whole program: the statements of one module, or of all modules after linking."""
    body: list[Stmt]


INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1


def literal_int(e: "Expr") -> int:
    """The value of an integer literal pattern, `-3` included."""
    if isinstance(e, UnaryOp):
        return -e.operand.value
    return e.value


def range_bounds(p: Pattern) -> tuple[int, int]:
    """The first and last integer of a `range` pattern, both included (it may be empty: `lo > hi`)."""
    lo = literal_int(p.lo) if p.lo is not None else INT_MIN
    if p.hi is None:
        return lo, INT_MAX
    hi = literal_int(p.hi)
    return lo, hi if p.inclusive else hi - 1


# ---- generic traversal -----------------------------------------------------------------------

_ANNOTATIONS = {"tys", "variant", "enum", "ty", "var", "vars", "info", "infos", "unwrap", "then_bind", "else_bind", "after_bind", "captures", "inst", "fixed", "slots"}


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
