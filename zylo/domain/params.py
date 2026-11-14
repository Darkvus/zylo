"""The `query(...)` and `path(...)` parameter markers of endpoint functions: what they say and what they may say.

A marker is the default of a parameter of a function given to `endpoint`: `limit: int = query(10, ge=1, le=100,
description="Page size")`. The first positional argument is the default value; leaving it out makes the parameter
required. The named arguments are constraints (`ge`, `gt`, `le`, `lt` for numbers, `min_length`, `max_length` for
strings) and a `description`; every value is a literal, because the compiler writes it into the OpenAPI document and the
validation code. See docs/guide.md ("Endpoints").
"""
from dataclasses import dataclass, field

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import FLOAT, INT, STR, OptionT, Type

MARKERS = ("query", "path")
NUMBER_LIMITS = ("ge", "gt", "le", "lt")
LENGTH_LIMITS = ("min_length", "max_length")
LIMITS = NUMBER_LIMITS + LENGTH_LIMITS


@dataclass
class ParamSpec:
    """What a marker default says about a parameter.

    Attributes:
        kind: "query" or "path".
        default: The default value expression, or None when the parameter is required.
        limits: The constraints by name (`ge`, `gt`, `le`, `lt`, `min_length`, `max_length`) and their literal values.
        description: The description, or "".
        node: The marker call, where errors are reported.
    """
    kind: str
    default: ast.Expr | None
    node: ast.Node
    limits: dict[str, int | float] = field(default_factory=dict)
    description: str = ""


def is_marker(e: ast.Expr | None) -> bool:
    """Whether a parameter default is `query(...)` or `path(...)`."""
    return isinstance(e, ast.Call) and e.func in MARKERS


def literal_number(e: ast.Expr) -> int | float | None:
    """The value of a number literal, `-3` and `-0.5` included; None for anything else."""
    if isinstance(e, (ast.Int, ast.Float)):
        return e.value
    if isinstance(e, ast.UnaryOp) and e.op == "-" and isinstance(e.operand, (ast.Int, ast.Float)):
        return -e.operand.value
    return None


def spec_of(p: ast.Param) -> ParamSpec | None:
    """Read the marker in the default of a parameter.

    Args:
        p: The parameter.

    Returns:
        What the marker says, or None when the default is not a marker.

    Raises:
        ZyloError: If the marker has more than one positional argument, an unknown or repeated named argument, or a
            value that is not a literal of the right kind.
    """
    d = p.default
    if not is_marker(d):
        return None
    assert isinstance(d, ast.Call)

    def fail(why: str, at: ast.Node = d) -> ZyloError:
        return ZyloError(f"{d.func}(...): {why}", at.line, at.col, at.file)

    if d.targs:
        raise fail("takes no type arguments")
    if len(d.args) > 1 or (d.func == "path" and d.args):
        raise fail("takes the default value as its only positional argument" if d.func == "query"
                   else "a path parameter is always required; it takes no default value")
    spec = ParamSpec(d.func, d.args[0] if d.args else None, d)
    for n in d.named:
        if n.name in spec.limits or (n.name == "description" and spec.description):
            raise fail(f"{n.name!r} is given more than once", n)
        if n.name == "description":
            if not isinstance(n.value, ast.Str):
                raise fail("description must be a string literal", n.value)
            spec.description = n.value.value
        elif n.name in NUMBER_LIMITS:
            value = literal_number(n.value)
            if value is None:
                raise fail(f"{n.name} must be a number literal (it is written into the OpenAPI document)", n.value)
            spec.limits[n.name] = value
        elif n.name in LENGTH_LIMITS:
            if not isinstance(n.value, ast.Int) or n.value.value < 0:
                raise fail(f"{n.name} must be a whole number literal, 0 or more", n.value)
            spec.limits[n.name] = n.value.value
        else:
            raise fail(f"unknown argument {n.name!r} (known: {', '.join(LIMITS)}, description)", n)
    return spec


def check_limits(spec: ParamSpec, ty: Type, name: str) -> None:
    """Check that the constraints of a marker suit the type of the parameter.

    Args:
        spec: What the marker says.
        ty: The parameter's type.
        name: The parameter's name, for messages.

    Raises:
        ZyloError: If a number constraint is on something that is not an `int` or `float` (or an optional of those), a
            length constraint on something that is not a `str`, or an `int` parameter has a fractional limit.
    """
    inner = ty.inner if isinstance(ty, OptionT) else ty
    for key, value in spec.limits.items():
        node = spec.node
        if key in NUMBER_LIMITS and inner not in (INT, FLOAT):
            raise ZyloError(f"{spec.kind}(...): {key} constrains an int or float, but parameter {name!r} is {ty}",
                            node.line, node.col, node.file)
        if key in LENGTH_LIMITS and inner != STR:
            raise ZyloError(f"{spec.kind}(...): {key} constrains a str, but parameter {name!r} is {ty}",
                            node.line, node.col, node.file)
        if inner == INT and isinstance(value, float):
            raise ZyloError(f"{spec.kind}(...): {key} of the int parameter {name!r} must be a whole number, not {value}",
                            node.line, node.col, node.file)


def default_of(p: ast.Param) -> ast.Expr | None:
    """The default value of a parameter: what a marker says, otherwise the default as written (None when required)."""
    return p.default.args[0] if is_marker(p.default) and p.default.args else (None if is_marker(p.default) else p.default)
