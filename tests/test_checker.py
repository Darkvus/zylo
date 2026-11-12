import pytest

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import BOOL, FLOAT, INT, STR, UNIT, ListT
from zylo.semantics.checker import check
from zylo.syntax.parser import parse


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


# ---- inferencia -----------------------------------------------------------------

def test_literal_and_binop_types():
    p = checked("x = 1 + 2\ny = 1.5 * 2.0\nz = \"a\" + \"b\"\nw = 1 < 2\n")
    assert [s.value.ty for s in p.body] == [INT, FLOAT, STR, BOOL]


def test_list_inference_and_index():
    p = checked("xs = [1, 2, 3]\ny = xs[0]\n")
    assert p.body[0].value.ty == ListT(INT)
    assert p.body[1].value.ty == INT


def test_fn_call_type_and_forward_reference():
    p = checked("print(double(2))\nfn double(n: int) -> int:\n    return n * 2\n")
    assert p.body[0].expr.args[0].ty == INT


def test_recursion():
    checked("fn fib(n: int) -> int:\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\nprint(fib(5))\n")


def test_conversions():
    p = checked("a = float(1)\nb = int(2.5)\nc = str(3)\nd = len(\"hi\")\n")
    assert [s.value.ty for s in p.body] == [FLOAT, INT, STR, INT]


def test_for_range_and_list():
    p = checked("for i in range(3):\n    print(i)\nfor x in [1.5]:\n    print(x)\n")
    assert p.body[0].info.ty == INT
    assert p.body[1].info.ty == FLOAT


def test_empty_list_with_annotation():
    p = checked("xs: list[int] = []\n")
    assert p.body[0].var.ty == ListT(INT)


def test_empty_list_as_argument_uses_param_type():
    checked("fn f(a: list[str]):\n    print(len(a))\nf([])\n")


# ---- declarar vs reasignar -------------------------------------------------------------

def test_first_assign_declares_second_reassigns():
    p = checked("x = 1\nx = 2\n")
    assert p.body[0].declares is True and p.body[1].declares is False
    assert p.body[0].var is p.body[1].var
    assert p.body[0].var.mutable is True


def test_never_reassigned_is_immutable():
    assert checked("x = 1\nprint(x)\n").body[0].var.mutable is False


def test_augmented_assign_marks_mutable():
    assert checked("x = 1\nx += 1\n").body[0].var.mutable is True


def test_append_and_index_assign_mark_list_mutable():
    p = checked("xs = [1]\nappend(xs, 2)\nys = [1]\nys[0] = 5\n")
    assert p.body[0].var.mutable and p.body[2].var.mutable


def test_reassign_inside_block_hits_outer_variable():
    p = checked("x = 1\nif true:\n    x = 2\n")
    assert p.body[0].var.mutable is True
    assert p.body[1].then[0].declares is False


def test_block_scope_var_not_visible_outside():
    fails("if true:\n    y = 1\nprint(y)\n", "undefined", line=3)


def test_top_level_vars_survive_fn_definitions():
    checked("x = 1\nfn f() -> int:\n    return 2\nprint(x)\n")


def test_fn_cannot_see_globals():
    fails("x = 1\nfn f() -> int:\n    return x\n", "undefined", line=3)


def test_param_reassign_marks_mutable():
    p = checked("fn f(n: int) -> int:\n    n = n + 1\n    return n\n")
    assert p.body[0].params[0].info.mutable is True


# ---- errores de tipos -----------------------------------------------------------------

def test_no_implicit_coercion():
    e = fails("x = 1 + 2.0\n", "types must match", line=1)
    assert "float(x)" in e.msg


def test_reassign_different_type():
    fails("x = 1\nx = \"a\"\n", "is int but str is assigned", line=2)


def test_annotation_mismatch():
    fails("x: int = \"a\"\n", "is int but str is assigned")


def test_annotated_redeclaration():
    fails("x = 1\nx: int = 2\n", "is already declared")


def test_condition_must_be_bool():
    fails("if 1:\n    print(1)\n", "must be bool", line=1)
    fails("while \"a\":\n    print(1)\n", "must be bool")


def test_logic_ops_need_bool():
    fails("x = 1 and 2\n", "expects bool")
    fails("x = not 1\n", "expects bool")


def test_undefined_variable_position():
    e = fails("print(1)\nprint(zzz)\n", "undefined")
    assert (e.line, e.col) == (2, 7)


def test_function_used_without_call():
    fails("x = print\n", "built-in function")
    fails("struct P:\n    a: int\nx = P\n", "build it with parentheses")


def test_empty_list_needs_annotation():
    fails("xs = []\n", "empty list")


def test_mixed_list():
    fails("xs = [1, \"a\"]\n", "mixes")


def test_index_errors():
    fails("x = 1\ny = x[0]\n", "cannot index")
    fails("xs = [1]\ny = xs[1.5]\n", "expected int")


def test_unit_value_cannot_be_assigned():
    fails("x = print(1)\n", "produces no value")


def test_comparisons_and_equality():
    checked("a = \"a\" < \"b\"\nb = [1] == [1]\nc = true == false\n")
    fails("x = true < false\n", "is not defined for bool")


def test_str_arith_only_plus():
    fails("x = \"a\" - \"b\"\n", "is not defined for str")


# ---- funciones ---------------------------------------------------------------------------

def test_wrong_arity_and_arg_type():
    fails("fn f(a: int):\n    return\nf(1, 2)\n", "takes 1 argument")
    e = fails("fn f(a: int):\n    return\nf(\"x\")\n", "argument 1 of f")
    assert e.line == 3


def test_undefined_function():
    fails("g(1)\n", "undefined")


def test_return_type_mismatch():
    fails("fn f() -> int:\n    return \"a\"\n", "but the function declares int", line=2)


def test_missing_return():
    fails("fn f(a: int) -> int:\n    if a > 0:\n        return 1\n", "without a `return`", line=1)


def test_if_else_both_return_is_ok():
    checked("fn f(a: int) -> int:\n    if a > 0:\n        return 1\n    else:\n        return 2\n")


def test_return_outside_fn_and_bare_return():
    fails("return 1\n", "outside a function", line=1)
    fails("fn f() -> int:\n    return\n", "without a value")
    fails("fn f():\n    return 1\n", "declares no return type")


def test_unit_fn_call_type():
    p = checked("fn hi():\n    print(1)\nhi()\n")
    assert p.body[1].expr.ty == UNIT


def test_duplicate_fn_and_param_and_builtin_shadow():
    fails("fn f():\n    return\nfn f():\n    return\n", "is already defined", line=3)
    fails("fn f(a: int, a: int):\n    return\n", "duplicate parameter")
    fails("fn print():\n    return\n", "built-in")


def test_nested_fn_is_a_local_function():
    check(parse("fn f():\n    fn g():\n        return\n    g()\n"))  # a local fn is a closure (see test_functions.py)


# ---- bucles -----------------------------------------------------------------------------------

def test_break_continue_outside_loop():
    fails("break\n", "outside a loop", line=1)
    fails("fn f():\n    continue\n", "outside a loop")
    checked("while true:\n    break\n")


def test_break_inside_fn_inside_loop_context_resets():
    fails("while true:\n    print(1)\nfn f():\n    break\n", "outside a loop")


def test_for_over_non_iterable():
    fails("for i in 5:\n    print(i)\n", "can only iterate")


def test_range_outside_for_and_bad_args():
    fails("x = range(3)\n", "can only be used in a `for`")
    fails("for i in range(1.5):\n    print(i)\n", "range expects int")
    fails("for i in range(1, 2, 3):\n    print(i)\n", "1 or 2 arguments")


def test_append_type_mismatch():
    fails("xs = [1]\nappend(xs, \"a\")\n", "cannot append")
