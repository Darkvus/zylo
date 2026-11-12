import pytest

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import INT, EnumT
from zylo.semantics.checker import check, check_all
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

SHAPE = "enum Shape:\n    Circle(float)\n    Rect(float, float)\n    Empty\n"


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


# ---- parser and formatter ---------------------------------------------------------------

def test_parse_enum_variants():
    e = parse(SHAPE).body[0]
    assert isinstance(e, ast.EnumDef) and e.name == "Shape"
    assert [(v.name, [t.name for t in v.fields]) for v in e.variants] == [
        ("Circle", ["float"]), ("Rect", ["float", "float"]), ("Empty", [])]


def test_parse_match_arms():
    src = SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Rect(w, h):\n        print(w)\n    _:\n        print(1)\n"
    m = parse(src).body[2]
    assert isinstance(m, ast.Match)
    assert [(a.variant, a.binds) for a in m.arms] == [("Rect", ["w", "h"]), ("_", [])]
    assert m.arms[1].type is None


def test_an_arm_must_name_its_enum():
    with pytest.raises(ZyloError, match="write the enum before the variant"):
        parse("match x:\n    Circle(r):\n        print(r)\n")


def test_empty_match_and_bad_enum_bodies_are_rejected():
    with pytest.raises(ZyloError, match="expected an indented block"):
        parse("match x:\nprint(1)\n")
    with pytest.raises(ZyloError, match="only contain variants"):
        parse("enum E:\n    1\n")


def test_formatter_keeps_enums_and_matches():
    src = SHAPE + "\nx = Shape.Empty\nmatch x:\n    Shape.Circle(r):\n        print(r)\n    _:\n        print(0)\n"
    assert format_source(src) == src
    assert format_source(format_source(src)) == src


# ---- checker ----------------------------------------------------------------------------

def test_constructors_have_the_enum_type():
    prog = checked(SHAPE + "a = Shape.Circle(1.0)\nb = Shape.Empty\n")
    assert prog.body[1].value.ty == EnumT("Shape")
    assert prog.body[2].value.variant == ("Shape", "Empty")


def test_constructor_argument_types_are_checked():
    fails(SHAPE + "x = Shape.Circle(1)\n", "expected float, got int", line=5)
    fails(SHAPE + "x = Shape.Rect(1.0)\n", "takes 2 argument")


def test_variant_data_must_match_the_declaration():
    fails(SHAPE + "x = Shape.Empty(1)\n", "carries no data")
    fails(SHAPE + "x = Shape.Circle\n", "carries data")
    fails(SHAPE + "x = Shape.Oval(1.0)\n", "no variant 'Oval'")
    fails(SHAPE + "x = Shape\n", "is an enum")


def test_match_binds_variant_data_with_its_types():
    prog = checked(SHAPE + "x = Shape.Rect(1.0, 2.0)\nmatch x:\n    Shape.Rect(w, h):\n        print(w * h)\n    _:\n        print(0)\n")
    arm = prog.body[2].arms[0]
    assert [v.name for v in arm.vars] == ["w", "h"]
    assert prog.body[2].enum == "Shape"


def test_match_must_cover_every_variant():
    fails(SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Empty:\n        print(1)\n", r"does not cover Shape\.Circle, Shape\.Rect")


def test_wildcard_covers_the_rest_but_not_when_everything_is_matched():
    checked(SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Empty:\n        print(1)\n    _:\n        print(2)\n")
    src = (SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Circle(r):\n        print(r)\n    Shape.Rect(w, h):\n        print(w)\n"
           "    Shape.Empty:\n        print(1)\n    _:\n        print(2)\n")
    fails(src, "can never run")


def test_arms_after_the_wildcard_or_repeated_are_rejected():
    fails(SHAPE + "x = Shape.Empty\nmatch x:\n    _:\n        print(1)\n    Shape.Empty:\n        print(2)\n", "can never run")
    fails(SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Empty:\n        print(1)\n    Shape.Empty:\n        print(2)\n    _:\n        print(3)\n",
          "matched twice")


def test_pattern_arity_and_enum_are_checked():
    fails(SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Circle(a, b):\n        print(a)\n    _:\n        print(0)\n", "carries 1 value")
    fails(SHAPE + "enum Other:\n    A\nx = Shape.Empty\nmatch x:\n    Other.A:\n        print(1)\n    _:\n        print(0)\n", "is on Shape, not Other")
    fails(SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Circle(a, a):\n        print(a)\n    _:\n        print(0)\n", "carries 1 value")


def test_match_needs_an_enum():
    fails("match 5:\n    _:\n        print(1)\n", "needs an enum value, not int")
    fails("x: int? = none\nmatch x:\n    _:\n        print(1)\n", "use `is some`")


def test_bound_names_stay_inside_their_arm():
    fails(SHAPE + "x = Shape.Circle(1.0)\nmatch x:\n    Shape.Circle(r):\n        print(r)\n    _:\n        print(0)\nprint(r)\n",
          "undefined variable 'r'")


def test_a_function_whose_arms_all_return_needs_no_final_return():
    checked(SHAPE + "fn f(s: Shape) -> int:\n    match s:\n        Shape.Circle(r):\n            return 1\n        _:\n            return 2\n")
    fails(SHAPE + "fn f(s: Shape) -> int:\n    match s:\n        Shape.Circle(r):\n            return 1\n        _:\n            print(2)\n",
          "can finish without a `return`")


def test_enums_work_as_types_and_in_collections():
    checked(SHAPE + "fn f(xs: list[Shape]) -> Shape?:\n    return none\nys: list[Shape] = [Shape.Empty, Shape.Circle(1.0)]\n"
            "print(ys[0] == Shape.Empty)\n")


def test_an_enum_cannot_contain_itself():
    fails("enum Tree:\n    Node(Tree, Tree)\n    Leaf\n", "contains itself")
    checked("enum Tree:\n    Node(list[Tree])\n    Leaf\n")


def test_enum_declarations_are_validated():
    fails("enum E:\n    A\n    A\n", "duplicate variant 'A'")
    fails("enum E:\n    A\nenum E:\n    B\n", "enum 'E' is already defined")
    fails("struct E:\n    n: int\nenum E:\n    B\n", "already a struct")
    fails("enum int:\n    A\n", "reserved name")
    fails("enum E:\n    A(Missing)\n", "unknown type 'Missing'")


def test_enums_are_top_level_only():
    fails("fn f():\n    enum E:\n        A\n", "top level")


def test_every_mistake_in_a_match_is_reported():
    src = SHAPE + "x = Shape.Empty\nmatch x:\n    Shape.Circle(a, b):\n        print(a)\n    Shape.Oval:\n        print(1)\n    _:\n        print(0)\n"
    errors = check_all(parse(src))
    assert len(errors) == 2


def test_variant_names_do_not_clash_between_enums():
    prog = checked("enum A:\n    X(int)\nenum B:\n    X(str)\na = A.X(1)\nb = B.X(\"s\")\n")
    assert prog.body[2].value.ty == EnumT("A") and prog.body[3].value.ty == EnumT("B")
    assert INT  # keep the import honest
