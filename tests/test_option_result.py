import pytest

from zylo import ast
from zylo.checker import check
from zylo.errors import ZyloError
from zylo.parser import parse
from zylo.types import BOOL, FLOAT, INT, STR, OptionT, ResultT, StructT


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


# ---- parser ------------------------------------------------------------------------------

def test_parse_option_and_result_types():
    s = parse("x: int? = none\n").body[0]
    assert s.annotation.name == "?" and s.annotation.args[0].name == "int"
    s = parse("r: Result[int, str] = ok(1)\n").body[0]
    assert s.annotation.name == "Result" and [a.name for a in s.annotation.args] == ["int", "str"]
    s = parse("xs: list[int?] = []\n").body[0]
    assert s.annotation.args[0].name == "?"
    s = parse("xs: list[int]? = none\n").body[0]
    assert s.annotation.name == "?" and s.annotation.args[0].name == "list"


def test_parse_nested_option_type_is_an_error():
    with pytest.raises(ZyloError, match="nested Option"):
        parse("x: int?? = none\n")


def test_parse_none_is_and_try():
    assert isinstance(parse("x = none\n").body[0].value, ast.NoneLit)
    e = parse("x is some\n").body[0].expr
    assert isinstance(e, ast.IsCheck) and e.kind == "some"
    for kind in ("some", "none", "ok", "err"):
        assert parse(f"x is {kind}\n").body[0].expr.kind == kind
    e = parse("f(1)?\n").body[0].expr
    assert isinstance(e, ast.Try) and isinstance(e.value, ast.Call)
    e = parse("a.b()?.c\n").body[0].expr
    assert isinstance(e, ast.Attr) and isinstance(e.value, ast.Try)


def test_parse_is_precedence():
    e = parse("x is some and y > 1\n").body[0].expr
    assert e.op == "and" and isinstance(e.left, ast.IsCheck)
    e = parse("not x is none\n").body[0].expr
    assert e.op == "not" and isinstance(e.operand, ast.IsCheck)


def test_parse_is_requires_a_valid_kind():
    with pytest.raises(ZyloError, match="`is` must be followed by"):
        parse("x is banana\n")
    with pytest.raises(ZyloError, match="`is` must be followed by"):
        parse("x is 3\n")


def test_ok_some_err_remain_usable_as_variable_names():
    checked("ok = 1\nr: Result[int, int] = err(ok)\n")


# ---- inference and constructors -------------------------------------------------------------------

def test_basic_types():
    p = checked("a: int? = none\nb = some(1)\nc = parse_int(\"1\")\nd = parse_float(\"1.5\")\n"
                "e: Result[int, str] = ok(1)\nf: Result[int, str] = err(\"x\")\n")
    assert p.body[1].value.ty == OptionT(INT)
    assert p.body[2].value.ty == OptionT(INT)
    assert p.body[3].value.ty == OptionT(FLOAT)
    assert p.body[4].value.ty == ResultT(INT, STR)
    assert p.body[5].value.ty == ResultT(INT, STR)


def test_none_needs_context():
    fails("x = none\n", "cannot infer the type of `none`", line=1)


def test_ok_and_err_need_context():
    e = fails("r = ok(1)\n", "cannot infer the type of `ok", line=1)
    assert "Result[int, str]" in e.msg
    fails("r = err(\"x\")\n", "cannot infer the type of `err")


def test_context_flows_through_return_args_fields_lists_and_reassign():
    checked("fn f() -> int?:\n    return none\n")
    checked("fn f(a: int?):\n    return\nf(none)\nf(some(1))\n")
    checked("struct A:\n    n: int?\na = A(none)\na.n = some(2)\na.n = none\n")
    checked("xs: list[int?] = [some(1), none]\nys: list[int?] = [none]\n")
    checked("x = some(1)\nx = none\n")


def test_nested_option_is_rejected():
    fails("x = some(some(1))\n", "nested Option types", line=1)


def test_ok_err_argument_type_checked():
    fails("r: Result[int, str] = ok(\"x\")\n", "ok\\(\\.\\.\\.\\) expects int, got str")
    fails("r: Result[int, str] = err(1)\n", "err\\(\\.\\.\\.\\) expects str, got int")


def test_option_mismatch_is_reported():
    fails("x: int? = some(\"a\")\n", "'x' is int\\? but str\\? is assigned")
    fails("x: int? = 5\n", "'x' is int\\? but int is assigned")


def test_parse_builtins_require_str():
    fails("x = parse_int(1)\n", "parse_int expects str")


def test_equality_with_none_and_some():
    checked("x = parse_int(\"1\")\na = x == none\nb = none == x\nc = x == some(1)\nd = x != none\n")
    p = checked("x = parse_int(\"1\")\na = x == none\n")
    assert p.body[1].value.ty == BOOL


def test_option_not_orderable_or_arithmetic():
    fails("x = parse_int(\"1\")\ny = x + 1\n", "types must match")
    fails("x = parse_int(\"1\")\ny = x < x\n", "is not defined for int\\?")


# ---- `is` and narrowing -----------------------------------------------------------------------------

def test_is_on_wrong_type_or_kind():
    fails("x = 1\ny = x is some\n", "needs an Option or a Result, not int")
    fails("x = parse_int(\"1\")\ny = x is ok\n", "cannot be applied to int\\?; use `is none` or `is some`")
    fails("r: Result[int, str] = ok(1)\ny = r is some\n", "use `is err` or `is ok`")


def test_is_check_is_a_bool_expression():
    p = checked("x = parse_int(\"1\")\nb = x is some\n")
    assert p.body[1].value.ty == BOOL


def test_narrowing_in_then_and_else_branches_for_option():
    p = checked("x = parse_int(\"1\")\nif x is some:\n    y = x + 1\nelse:\n    z = 0\n")
    s = p.body[1]
    assert s.then_bind.ty == INT and s.then_bind.narrowed
    assert s.else_bind is None and s.after_bind is None
    p = checked("x = parse_int(\"1\")\nif x is none:\n    z = 0\nelse:\n    y = x + 1\n")
    assert p.body[1].then_bind is None and p.body[1].else_bind.ty == INT


def test_narrowing_for_result_gives_ok_type_and_err_type():
    src = ("r: Result[int, str] = ok(1)\nif r is ok:\n    a = r + 1\nelse:\n    b = r + \"!\"\n")
    p = checked(src)
    assert p.body[1].then_bind.ty == INT and p.body[1].else_bind.ty == STR
    p = checked("r: Result[int, str] = ok(1)\nif r is err:\n    b = r + \"!\"\nelse:\n    a = r + 1\n")
    assert p.body[1].then_bind.ty == STR and p.body[1].else_bind.ty == INT


def test_using_option_as_plain_value_without_narrowing_fails():
    fails("x = parse_int(\"1\")\ny = x + 1\n", "types must match", line=2)


def test_narrowing_does_not_leak_out_of_the_branch():
    fails("x = parse_int(\"1\")\nif x is some:\n    y = x + 1\nz = x + 1\n", "types must match", line=4)


def test_narrowing_with_elif_chain():
    checked("x = parse_int(\"1\")\nif x is none:\n    a = 0\nelif x > 5:\n    a = 1\nelse:\n    a = 2\n")


def test_narrowed_variable_cannot_be_reassigned():
    fails("x = parse_int(\"1\")\nif x is some:\n    x = 5\n", "narrowed to int by an `is` check", line=3)
    fails("x = parse_int(\"1\")\nif x is some:\n    x += 1\n", "narrowed to int")


def test_mutating_a_narrowed_list_marks_the_shadow_mutable():
    p = checked("xs: list[int]? = some([1])\nif xs is some:\n    append(xs, 2)\n")
    assert p.body[1].then_bind.mutable is True


def test_no_narrowing_for_complex_conditions():
    fails("x = parse_int(\"1\")\nif x is some and true:\n    y = x + 1\n", "types must match", line=3)


# ---- early exit ---------------------------------------------------------------------------------------

def test_early_exit_narrows_the_rest_of_the_block():
    src = ("fn f(s: str) -> int:\n    n = parse_int(s)\n    if n is none:\n        return 0\n    return n + 1\n")
    p = checked(src)
    s = p.body[0].body[1]
    assert s.after_bind.ty == INT and s.after_bind.narrowed


@pytest.mark.parametrize("exit_stmt", ["return 0", "break", "continue", "exit(1)"])
def test_every_kind_of_exit_counts(exit_stmt):
    src = f"fn f(s: str) -> int:\n    while true:\n        n = parse_int(s)\n        if n is none:\n            {exit_stmt}\n        return n\n    return 0\n"
    checked(src)


def test_non_exiting_branch_does_not_narrow():
    fails("fn f(s: str) -> int:\n    n = parse_int(s)\n    if n is none:\n        print(1)\n    return n\n",
          "but the function declares int")


def test_early_exit_nested_if_else_both_exit():
    src = ("fn f(s: str) -> int:\n    n = parse_int(s)\n    if n is none:\n        if s == \"\":\n            return 1\n"
           "        else:\n            return 2\n    return n\n")
    checked(src)


def test_early_exit_for_result_err_narrows_to_ok_afterwards():
    src = ("fn f(r: Result[int, str]) -> int:\n    if r is err:\n        return 0\n    return r\n")
    p = checked(src)
    assert p.body[0].body[0].after_bind.ty == INT
    assert p.body[0].body[0].then_bind.ty == STR


def test_early_exit_with_else_does_not_use_after_binding():
    src = ("fn f(s: str) -> int:\n    n = parse_int(s)\n    if n is none:\n        return 0\n    else:\n        return n\n")
    p = checked(src)
    assert p.body[0].body[1].after_bind is None


def test_narrowing_at_top_level_with_break():
    checked("for s in [\"1\"]:\n    n = parse_int(s)\n    if n is none:\n        break\n    print(n + 1)\n")


# ---- `?` operator -----------------------------------------------------------------------------------------

def test_try_on_result_and_option():
    p = checked("fn a(s: str) -> Result[int, str]:\n    return ok(1)\n"
                "fn b(s: str) -> Result[int, str]:\n    x = a(s)?\n    return ok(x + 1)\n"
                "fn c(s: str) -> int?:\n    x = parse_int(s)?\n    return some(x + 1)\n")
    assert p.body[1].body[0].value.ty == INT
    assert p.body[2].body[0].value.ty == INT


def test_try_error_type_must_match():
    fails("fn a() -> Result[int, str]:\n    return ok(1)\nfn b() -> Result[int, int]:\n    x = a()?\n    return ok(x)\n",
          "requires the function to return Result\\[\\.\\.\\., str\\]", line=4)


def test_try_kind_must_match_function_return():
    fails("fn b() -> Result[int, str]:\n    x = parse_int(\"1\")?\n    return ok(x)\n",
          "requires the function to return an Option", line=2)
    fails("fn a() -> Result[int, str]:\n    return ok(1)\nfn b() -> int?:\n    x = a()?\n    return some(x)\n",
          "requires the function to return Result")
    fails("fn b() -> int:\n    x = parse_int(\"1\")?\n    return x\n", "requires the function to return an Option")


def test_try_outside_function_and_on_plain_values():
    fails("x = parse_int(\"1\")?\n", "only be used inside a function", line=1)
    fails("fn f() -> int?:\n    x = 5?\n    return some(x)\n", "only be applied to an Option or a Result, not int")


def test_try_works_in_methods():
    checked("struct A:\n    n: int\n\n    fn get(self, s: str) -> int?:\n        v = parse_int(s)?\n        return some(v + self.n)\n")


# ---- methods ----------------------------------------------------------------------------------------------------

def test_unwrap_and_unwrap_or_types():
    p = checked("x = parse_int(\"1\")\na = x.unwrap()\nb = x.unwrap_or(0)\n"
                "r: Result[str, int] = ok(\"a\")\nc = r.unwrap()\nd = r.unwrap_or(\"z\")\n")
    assert [s.value.ty for s in p.body if isinstance(s, ast.Assign) and s.target.id in "abcd"] == [INT, INT, STR, STR]


def test_method_errors_on_option():
    fails("x = parse_int(\"1\")\ny = x.unwrap_or(\"a\")\n", "unwrap_or on int\\? expects int, got str")
    fails("x = parse_int(\"1\")\ny = x.unwrap(1)\n", "unwrap takes no arguments")
    fails("x = parse_int(\"1\")\ny = x.unwrap_or()\n", "exactly 1 argument")
    e = fails("x = parse_int(\"1\")\ny = x.map()\n", "has no method 'map'")
    assert "unwrap, unwrap_or" in e.msg


def test_unwrap_or_gives_empty_list_context():
    checked("xs: list[int]? = none\nys = xs.unwrap_or([])\n")


def test_struct_fields_of_option_type():
    p = checked("struct U:\n    age: int?\nu = U(some(1))\nn = u.age.unwrap_or(0)\n")
    assert p.body[0].fields[0].ty == OptionT(INT)
    assert p.body[-1].value.ty == INT


def test_result_struct_name_is_reserved():
    fails("struct Result:\n    n: int\n", "reserved")


def test_option_of_struct_and_struct_equality():
    checked("struct P:\n    x: int\na: P? = some(P(1))\nb = a == some(P(1))\n")
    assert checked("struct P:\n    x: int\na: P? = none\n").body[1].var.ty == OptionT(StructT("P"))
