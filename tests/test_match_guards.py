"""`match` guards and or-patterns (v0.18)."""
import pytest

from zylo.application.compile import transpile_source
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.semantics.checker import check
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

COLOR = "enum Color:\n    Red\n    Green\n    Blue\n    Rgb(int, int)\n    Gray(int)\n\n"


def fails(src: str, match: str) -> None:
    with pytest.raises(ZyloError, match=match):
        check(parse(src))


def test_parse_guard_and_alternatives():
    m = parse("x = 1\nmatch x:\n    1 | 2 | 3 if x > 1:\n        print(1)\n    _:\n        print(2)\n").body[1]
    arm = m.arms[0]
    assert arm.pattern.kind == "or" and len(arm.pattern.subs) == 3 and isinstance(arm.guard, ast.BinOp)
    assert m.arms[1].guard is None and m.arms[1].pattern.kind == "wild"


def test_formatter_round_trips():
    src = ('x = 1\nmatch x:\n    1 | 2:\n        print(1)\n    3 if x > 2:\n        print(2)\n'
           '    _ if x < 0:\n        print(3)\n    _:\n        print(4)\n')
    assert format_source(src) == src
    assert format_source(format_source(src)) == src
    assert format_source("x = 1\nmatch x:\n    1|2 if  x>1:\n        print(1)\n    _:\n        print(2)\n") \
        == "x = 1\nmatch x:\n    1 | 2 if x > 1:\n        print(1)\n    _:\n        print(2)\n"


def test_codegen_emits_guard_and_alternatives():
    rs = transpile_source("x = 1\nmatch x:\n    1 | 2 if x > 1:\n        print(1)\n    _:\n        print(2)\n")
    assert "1i64 | 2i64 if " in rs


def test_guarded_enum_arms_do_not_cover():
    fails(COLOR + "c = Color.Red\nmatch c:\n    Color.Red if true:\n        print(1)\n    Color.Green | Color.Blue:\n"
          "        print(2)\n    Color.Rgb(a, b):\n        print(a)\n    Color.Gray(g):\n        print(g)\n",
          "does not cover Color.Red")


def test_guarded_arm_then_unguarded_is_complete():
    check(parse(COLOR + "c = Color.Red\nmatch c:\n    Color.Red if false:\n        print(0)\n    Color.Red | Color.Green | "
                "Color.Blue:\n        print(2)\n    Color.Rgb(a, _) | Color.Gray(a):\n        print(a)\n"))


def test_guarded_wildcard_is_not_final():
    fails("x = 1\nmatch x:\n    _ if x > 0:\n        print(1)\n", "needs a final `_` arm")
    check(parse("x = 1\nmatch x:\n    _ if x > 0:\n        print(1)\n    _:\n        print(2)\n"))


def test_guarded_wildcard_does_not_make_later_arms_unreachable():
    check(parse("x = 1\nmatch x:\n    _ if x > 0:\n        print(1)\n    5:\n        print(2)\n    _:\n        print(3)\n"))


def test_guard_must_be_bool():
    fails("x = 1\nmatch x:\n    1 if x:\n        print(1)\n    _:\n        print(2)\n", "guard must be bool")


def test_guard_sees_bindings():
    check(parse("o: int? = some(1)\nmatch o:\n    some(v) if v > 0:\n        print(v)\n    some(_) | none:\n        print(0)\n"))


def test_duplicate_in_or_pattern():
    fails("x = 1\nmatch x:\n    1 | 1:\n        print(1)\n    _:\n        print(2)\n", "matched twice")


def test_or_pattern_repeating_earlier_arm():
    fails('s = "a"\nmatch s:\n    "a":\n        print(1)\n    "b" | "a":\n        print(2)\n    _:\n        print(3)\n',
          "matched twice")


def test_wildcard_in_or_pattern_rejected():
    fails("x = 1\nmatch x:\n    1 | _:\n        print(1)\n", "cannot be part of an or-pattern")
    fails("x = 1\nmatch x:\n    _ | 1:\n        print(1)\n", "cannot be part of an or-pattern")


def test_or_pattern_binding_mismatch():
    fails(COLOR + "c = Color.Red\nmatch c:\n    Color.Rgb(a, b) | Color.Gray(a):\n        print(a)\n    _:\n        print(0)\n",
          "same names with the same types")
    fails(COLOR + "c = Color.Red\nmatch c:\n    Color.Gray(a) | Color.Red:\n        print(1)\n    _:\n        print(0)\n",
          "same names with the same types")


def test_or_pattern_binding_type_mismatch():
    fails("enum E:\n    A(int)\n    B(str)\n\ne = E.A(1)\nmatch e:\n    E.A(v) | E.B(v):\n        print(1)\n",
          "same names with the same types")


def test_or_pattern_literal_type_mismatch():
    fails('x = 1\nmatch x:\n    1 | "a":\n        print(1)\n    _:\n        print(2)\n', "pattern is str")


def test_bool_guards():
    check(parse("b = true\nmatch b:\n    true if 1 > 2:\n        print(1)\n    true | false:\n        print(2)\n"))


def test_diagnostics_in_the_editor_for_guards():
    from pathlib import Path

    from zylo.application.compile import diagnose_source
    errs = diagnose_source("x = 1\nmatch x:\n    1 if x:\n        print(1)\n    _:\n        print(2)\n", Path("doc.zy"))
    assert any("guard must be bool" in e.msg for e in errs)
