"""v0.10: methods on enums, `match` on values / Option / Result, default values and named arguments."""
import shutil
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application.editor import document_symbols
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import EnumT
from zylo.semantics.checker import check, check_all
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

SHAPE = '''\
enum Shape:
    Circle(float)
    Rect(float, float)
    Empty

    fn area(self) -> float:
        match self:
            Shape.Circle(r):
                return 3.0 * r * r
            Shape.Rect(w, h):
                return w * h
            Shape.Empty:
                return 0.0

    fn scaled(self, k: float = 2.0) -> Shape:
        match self:
            Shape.Circle(r):
                return Shape.Circle(r * k)
            _:
                return self

'''


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


# ---- parser and formatter -------------------------------------------------------------------

def test_parse_enum_methods():
    e = parse(SHAPE).body[0]
    assert [m.name for m in e.methods] == ["area", "scaled"]
    assert all(m.has_self for m in e.methods)
    assert [v.name for v in e.variants] == ["Circle", "Rect", "Empty"]


def test_parse_defaults_and_named_arguments():
    prog = parse("struct P:\n    x: float = 0.0\n    y: float\nfn f(a: int, b: int = 2):\n    print(a)\nf(1, b=3)\n")
    assert prog.body[0].fields[0].default.value == 0.0 and prog.body[0].fields[1].default is None
    assert prog.body[1].params[1].default.value == 2
    call = prog.body[2].expr
    assert len(call.args) == 1 and [n.name for n in call.named] == ["b"]


def test_parse_match_patterns():
    src = "match x:\n    1:\n        pass\n    -2:\n        pass\n    \"a\":\n        pass\n    true:\n        pass\n"
    src += "    some(v):\n        pass\n    none:\n        pass\n    ok(v):\n        pass\n    err(_):\n        pass\n    _:\n        pass\n"
    arms = parse(src.replace("pass", "print(1)")).body[0].arms
    assert [a.pattern.kind for a in arms] == ["lit"] * 4 + ["some", "none", "ok", "err", "wild"]
    assert arms[1].pattern.literal.op == "-" and arms[4].pattern.subs[0].name == "v"


def test_parse_errors():
    with pytest.raises(ZyloError, match="positional arguments must come before named"):
        parse("f(a=1, 2)\n")
    with pytest.raises(ZyloError, match="holds exactly one pattern"):
        parse("match x:\n    some:\n        print(1)\n")
    assert parse("match x:\n    Circle:\n        print(1)\n").body[0].arms[0].pattern.kind == "bind"


def test_formatter_roundtrips_new_syntax():
    src = (SHAPE + "struct C:\n    n: int = 1\n    tags: list[str] = []\n\nfn f(a: int, b: int? = none) -> int:\n    return a\n\n"
           "x = f(1, b=some(2))\nc = C(n=3)\nmatch x:\n    1:\n        print(1)\n    -1:\n        print(2)\n    _:\n        print(3)\n")
    once = format_source(src)
    assert once == src
    assert format_source(once) == once


# ---- enum methods --------------------------------------------------------------------------

def test_enum_method_types():
    prog = checked(SHAPE + "x = Shape.Rect(1.0, 2.0).scaled()\ny = x.area()\n")
    assert prog.body[-2].value.ty == EnumT("Shape")
    assert prog.body[-1].value.ty.name == "float"


def test_enum_method_errors():
    fails(SHAPE + "Shape.Empty.nope()\n", "Shape has no method 'nope' \\(has: area, scaled\\)")
    fails(SHAPE + "Shape.Empty.area(1.0)\n", "takes 0 argument")
    fails("enum E:\n    A\n    fn A(self) -> int:\n        return 1\n", "both a variant and a method")
    fails("enum E:\n    A\n    fn f() -> int:\n        return 1\n", "must take `self`")
    fails("enum E:\n    A\n    fn f(self):\n        self = E.A\n", "enum values are immutable", 4)
    fails("enum E:\n    A\n    fn f(self) -> int:\n        return 1\n    fn f(self) -> int:\n        return 2\n", "already defined")


def test_enum_method_is_checked_inside():
    fails("enum E:\n    A\n    fn f(self) -> int:\n        return \"x\"\n", "returning str", 4)


def test_enum_method_in_a_module(tmp_path: Path):
    (tmp_path / "shapes.zy").write_text(SHAPE)
    main = tmp_path / "main.zy"
    main.write_text("import shapes\ns = shapes.Shape.Circle(1.0)\nprint(s.scaled(k=3.0).area())\n")
    assert "area" in pipeline.transpile(main)


# ---- match on values, Option and Result -----------------------------------------------------

def test_match_int_and_str_need_a_wildcard():
    fails("x = 1\nmatch x:\n    1:\n        print(1)\n", "needs a final `_` arm", 2)
    fails("x = \"a\"\nmatch x:\n    \"a\":\n        print(1)\n", "needs a final `_` arm")
    checked("x = 1\nmatch x:\n    1:\n        print(1)\n    _:\n        print(2)\n")


def test_match_bool_is_exhaustive_with_both_values():
    checked("b = true\nmatch b:\n    true:\n        print(1)\n    false:\n        print(2)\n")
    fails("b = true\nmatch b:\n    true:\n        print(1)\n", "does not cover false")
    fails("b = true\nmatch b:\n    true:\n        print(1)\n    false:\n        print(2)\n    _:\n        print(3)\n", "can never run")


def test_match_option_and_result():
    checked("x: int? = none\nmatch x:\n    some(v):\n        print(v + 1)\n    none:\n        print(0)\n")
    checked("r: Result[int, str] = ok(1)\nmatch r:\n    ok(v):\n        print(v)\n    err(e):\n        print(e + \"!\")\n")
    fails("x: int? = none\nmatch x:\n    some(v):\n        print(v)\n", "does not cover none")
    fails("r: Result[int, str] = ok(1)\nmatch r:\n    ok(v):\n        print(v)\n", "does not cover err\\(_\\)")
    fails("x: int? = none\nmatch x:\n    ok(v):\n        print(v)\n    _:\n        print(1)\n", "can only match a Result")
    fails("r: Result[int, str] = ok(1)\nmatch r:\n    some(v):\n        print(v)\n    _:\n        print(1)\n", "can only match an Option")


def test_match_binding_types():
    prog = checked("r: Result[int, str] = ok(1)\nmatch r:\n    ok(v):\n        a = v + 1\n    err(e):\n        b = e + \"x\"\n")
    arms = prog.body[1].arms
    assert arms[0].vars[0].ty.name == "int" and arms[1].vars[0].ty.name == "str"
    fails("x: int? = none\nmatch x:\n    some(v):\n        print(v)\n    none:\n        print(\"a\" + v)\n", "undefined variable 'v'")


def test_match_pattern_errors():
    fails("x = 1\nmatch x:\n    \"a\":\n        print(1)\n    _:\n        print(2)\n", "is on int, but the pattern is str", 3)
    fails("x = 1\nmatch x:\n    1:\n        print(1)\n    1:\n        print(2)\n    _:\n        print(3)\n", "matched twice", 5)
    fails("x = 1\nmatch x:\n    _:\n        print(1)\n    2:\n        print(2)\n", "can never run", 5)
    fails("x = 1\nmatch x:\n    1.5:\n        print(1)\n    _:\n        print(2)\n", "floats cannot be matched")
    fails("x = 1\nmatch x:\n    -1.5:\n        print(1)\n    _:\n        print(2)\n", "floats cannot be matched")
    fails("x = 1\nmatch x:\n    -true:\n        print(1)\n    _:\n        print(2)\n", "must be a literal")
    fails(SHAPE + "x = 1\nmatch x:\n    Shape.Empty:\n        print(1)\n    _:\n        print(2)\n", "is a variant pattern")
    fails(SHAPE + "x = Shape.Empty\nmatch x:\n    1:\n        print(1)\n    _:\n        print(2)\n", "is on Shape, but the pattern is int")


def test_the_body_of_a_wildcard_arm_is_checked():
    fails("x = 1\nmatch x:\n    _:\n        print(undefined_name)\n", "undefined variable", 4)


def test_function_returning_in_every_match_arm_needs_no_final_return():
    checked("fn f(x: int?) -> int:\n    match x:\n        some(v):\n            return v\n        none:\n            return 0\n")


def test_is_narrowing_still_works():
    checked("fn f(x: int?) -> int:\n    if x is none:\n        return 0\n    return x + 1\n")


# ---- defaults and named arguments ---------------------------------------------------------------

DEFAULTS = "fn f(a: int, b: int = 2, c: str = \"z\") -> int:\n    return a + b\n"


def test_calls_are_completed_with_defaults():
    prog = checked(DEFAULTS + "x = f(1)\ny = f(1, c=\"q\", b=5)\n")
    x, y = prog.body[1].value, prog.body[2].value
    assert [type(a).__name__ for a in x.args] == ["Int", "Int", "Str"] and not x.named
    assert [a.value for a in y.args] == [1, 5, "q"] and not y.named


def test_checking_twice_is_stable():
    prog = parse(DEFAULTS + "x = f(1, c=\"q\")\n")
    assert not check_all(prog) and not check_all(prog)
    assert [a.value for a in prog.body[1].value.args] == [1, 2, "q"]


def test_argument_errors():
    fails(DEFAULTS + "f()\n", "f is missing argument 'a'", 3)
    fails(DEFAULTS + "f(b=1)\n", "f is missing argument 'a'")
    fails("fn g(a: int, b: int):\n    print(a)\ng(b=1)\n", "g is missing argument 'a'")
    fails(DEFAULTS + "f(1, d=3)\n", "f has no parameter 'd' \\(has: a, b, c\\)", 3)
    fails(DEFAULTS + "f(1, a=3)\n", "argument 'a' of f is given more than once")
    fails(DEFAULTS + "f(1, b=3, b=4)\n", "argument 'b' of f is given more than once")
    fails(DEFAULTS + "f(1, 2, \"a\", 4)\n", "f takes 3 argument\\(s\\), got 4")
    fails(DEFAULTS + "f(1, b=\"x\")\n", "argument 2 of f: expected int, got str")
    fails("print(1, sep=2)\n", "print takes positional arguments only")
    fails("x = [1]\nx.append(item=1)\n", "takes positional arguments only")
    fails("x: int? = none\nx.unwrap_or(default=1)\n", "takes positional arguments only")
    fails(SHAPE + "Shape.Circle(r=1.0)\n", "takes positional arguments only")


def test_default_declaration_errors():
    fails("fn f(a: int = 1, b: int):\n    print(a)\n", "parameter 'b' has no default but follows 'a'", 1)
    fails("fn f(a: int = \"x\"):\n    print(a)\n", "the default of parameter 'a' is str but it is declared int")
    fails("fn f(a: int = 1 + 2):\n    print(a)\n", "must be a constant")
    fails("x = 1\nfn f(a: int = x):\n    print(a)\n", "must be a constant")
    fails("fn f(a: list[int] = [1]):\n    print(a)\n", "must be a constant")
    fails("fn f(a: int? = 1):\n    print(a)\n", "the default of parameter 'a' is int but it is declared int\\?")
    fails("struct S:\n    a: int = 1\n    b: int\n", "field 'b' has no default but follows 'a'", 3)
    checked("fn f(a: list[int] = [], b: dict[str, int] = {}, c: tuple[int, str] = (1, \"a\"), d: int? = none, e: int = -1):\n    print(a)\n")


def test_constant_defaults_are_independent_per_call():
    prog = checked("fn f(xs: list[int] = []) -> list[int]:\n    xs.append(1)\n    return xs\nf()\nf()\n")
    a, b = prog.body[1].expr.args[0], prog.body[2].expr.args[0]
    assert a is not b


def test_struct_constructor_defaults_and_names():
    s = "struct P:\n    n: int\n    x: float = 0.0\n    y: float = 1.0\n"
    checked(s + "p = P(n=1)\nq = P(2, y=3.0)\nr = P(1, 2.0, 3.0)\n")
    fails(s + "P()\n", "P is missing argument 'n'")
    fails(s + "P(z=1.0, n=1)\n", "P has no field 'z' \\(has: n, x, y\\)")
    fails(s + "P(1, n=2)\n", "argument 'n' of P is given more than once")


def test_method_defaults_and_names():
    s = "struct P:\n    x: float\n    fn move(self, dx: float = 1.0, dy: float = 0.0):\n        self.x += dx\n"
    checked(s + "p = P(1.0)\np.move()\np.move(dy=2.0)\np.move(2.0, dy=1.0)\n")
    fails(s + "p = P(1.0)\np.move(dz=1.0)\n", "P.move has no parameter 'dz'")


def test_defaults_in_extern_and_modules(tmp_path: Path):
    (tmp_path / "lib.zy").write_text("struct Opt:\n    level: int = 3\n\nfn go(a: int, b: int = 10) -> int:\n    return a + b\n")
    main = tmp_path / "main.zy"
    main.write_text("import lib\nfrom lib import go\nx = lib.go(1)\ny = go(1, b=2)\no = lib.Opt()\nz = lib.Opt(level=5)\n")
    assert "fn lib__go" in pipeline.transpile(main)


def test_editor_symbols_show_defaults_and_enum_methods():
    syms = document_symbols(SHAPE + "fn f(a: int, b: int = 2):\n    print(a)\n\nstruct C:\n    n: int = 5\n")
    shape = next(s for s in syms if s.name == "Shape")
    assert [(c.name, c.kind) for c in shape.children][-2:] == [("area", "method"), ("scaled", "method")]
    assert shape.children[-1].detail == "fn scaled(self, k: float = 2.0) -> Shape"
    assert next(s for s in syms if s.name == "f").detail == "fn f(a: int, b: int = 2)"
    assert next(s for s in syms if s.name == "C").children[0].detail == "int = 5"


# ---- end to end --------------------------------------------------------------------------------

@needs_rustc
def test_generated_rust_for_new_features(tmp_path: Path):
    src = tmp_path / "t.zy"
    src.write_text(SHAPE + "match 3:\n    3:\n        print(Shape.Circle(1.0).area())\n    _:\n        print(0)\n")
    rust = pipeline.transpile(src)
    assert "impl Shape" in rust and "3i64 =>" in rust


def test_enum_variants_are_valid_defaults():
    checked(SHAPE + "struct S:\n    shape: Shape = Shape.Empty\n    other: Shape = Shape.Circle(1.0)\nfn f(s: Shape = Shape.Empty):\n    print(s)\nx = S()\nf()\n")
    fails(SHAPE + "fn f(s: Shape = Shape.Circle(1.0 + 2.0)):\n    print(s)\n", "must be a constant")
    fails(SHAPE + "fn f(s: Shape = Shape.Rect(1.0)):\n    print(s)\n", "takes 2 argument")
