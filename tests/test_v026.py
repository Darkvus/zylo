"""v0.26: lost parameter mutations, `pass`, `;`, `type_name` and the "already narrowed" error."""
import shutil
import subprocess

import pytest

from zylo.application.compile import transpile_source
from zylo.application.format import format_text
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check, check_all
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

A = "struct A:\n    names: dict[str, str]\n\n    fn add(self, k: str):\n        self.names[k] = \"v\"\n"


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        check(parse(src))
    if line is not None:
        assert e.value.line == line
    return e.value


def run(src: str, tmp_path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout


# ---- a modified parameter nobody sees ----------------------------------------------------------------

LOST = "fn add(a: A, k: str):\n    a.names[k] = \"v\"\n"


def test_modifying_a_struct_parameter_without_using_it_is_an_error():
    e = fails(A + LOST, "parameter 'a' is modified but the changes are not visible to the caller", line=6)
    assert "return it" in str(e) and "Cell" in str(e)


@pytest.mark.parametrize("body", [
    "fn f(xs: list[int]):\n    append(xs, 1)\n",
    "fn f(xs: list[int]):\n    xs.append(1)\n",
    "fn f(xs: list[int]):\n    xs[0] = 1\n",
    "fn f(d: dict[str, int]):\n    d[\"a\"] = 1\n",
    "fn f(s: set[int]):\n    s.add(1)\n",
    "struct P:\n    n: int\nfn f(p: P):\n    p.n = 1\n",
    "struct P:\n    n: int\n\n    fn bump(self):\n        self.n += 1\nfn f(p: P):\n    p.bump()\n",
])
def test_every_kind_of_lost_modification_is_rejected(body):
    fails(body, "is modified but the changes are not visible")


@pytest.mark.parametrize("body", [
    "fn f(xs: list[int]) -> list[int]:\n    xs.sort()\n    return xs\n",          # returned
    "fn f(xs: list[int]) -> int:\n    append(xs, 1)\n    return len(xs)\n",       # read afterwards
    "fn f(xs: list[int]):\n    append(xs, 1)\n    print(xs)\n",                    # printed
    "fn f(xs: list[int]):\n    xs = [1]\n",                                        # a rebinding is a local
    "fn f(c: Cell[int]):\n    c.set(1)\n",                                         # cells are shared
    "fn f(a: A, b: A) -> A:\n    a.names[\"k\"] = \"v\"\n    return a\n",
])
def test_using_the_modified_copy_is_fine(body):
    check(parse(A + body))


def test_methods_may_modify_self():
    check(parse(A))


def test_the_rule_covers_anonymous_functions():
    fails("fn outer():\n    fn g(xs: list[int]):\n        xs[0] = 1\n    g([1])\n", "parameter 'xs' is modified")
    check(parse("fn outer():\n    fn g(xs: list[int]) -> list[int]:\n        xs[0] = 1\n        return xs\n    g([1])\n"))


def test_the_rule_covers_generic_functions():
    fails("fn f[T](xs: list[T], x: T):\n    append(xs, x)\nf([1], 2)\n", "parameter 'xs' is modified")
    check(parse("fn f[T](xs: list[T], x: T) -> list[T]:\n    append(xs, x)\n    return xs\nf([1], 2)\n"))


def test_closures_cannot_modify_what_they_capture():
    fails("fn outer():\n    xs = [1]\n    fn g():\n        xs[0] = 2\n    g()\n", "closures capture a copy")


# ---- pass ----------------------------------------------------------------------------------------------------

def test_pass_parses_checks_and_formats():
    src = "enum E:\n    A\n    B\n\nx = E.A\nmatch x:\n    E.A:\n        pass\n    E.B:\n        print(1)\n"
    prog = check(parse(src))
    arm = next(s for s in prog.body if isinstance(s, ast.Match)).arms[0]
    assert isinstance(arm.body[0], ast.Pass)
    assert format_text(src) == src


def test_pass_is_a_keyword_and_needs_its_own_line():
    with pytest.raises(ZyloError):
        parse("pass = 1\n")
    with pytest.raises(ZyloError):
        parse("pass 1\n")


def test_pass_in_functions_and_loops_generates_valid_rust():
    rs = transpile_source("fn f():\n    pass\nfor i in range(3):\n    pass\nf()\n")
    assert "fn f()" in rs


# ---- ; ----------------------------------------------------------------------------------------------------

def test_semicolon_separates_statements():
    prog = parse("a = 1; b = 2\n")
    assert [type(s) for s in prog.body] == [ast.Assign, ast.Assign]
    assert [s.line for s in prog.body] == [1, 1]


def test_trailing_semicolon_is_ignored_and_formatter_splits_lines():
    assert format_text("a = 1; b = 2;\nprint(a); print(b)\n") == "a = 1\nb = 2\nprint(a)\nprint(b)\n"


def test_semicolon_inside_a_block_and_before_a_block():
    prog = parse("x = 1; y = 2\nif x == 1:\n    y = 3; x = 4\n")
    assert len(prog.body[2].then) == 2


@pytest.mark.parametrize("src", ["; a = 1\n", "a = 1;; b = 2\n", "a = [1; 2]\n", "a = (1;\n2)\n"])
def test_misplaced_semicolons_are_errors(src):
    with pytest.raises(ZyloError):
        parse(src)


def test_semicolon_inside_a_string_is_not_a_separator():
    assert len(parse("s = \"a; b\"\n").body) == 1


@needs_rustc
def test_semicolons_run(tmp_path):
    assert run("x = 1; y = 2; z = x + y\nprint(x, y, z)\n", tmp_path) == "1 2 3\n"


# ---- type_name -----------------------------------------------------------------------------------------------

def test_type_name_of_concrete_types_is_a_string_literal():
    prog = check(parse("print(type_name(int), type_name(dict[str, list[int]]), type_name(int?))\n"))
    call = prog.body[0].expr
    assert [a.args[0].value for a in call.args] == ["int", "dict[str, list[int]]", "int?"]


def test_type_name_errors():
    with pytest.raises(ZyloError):
        parse("print(type_name(1))\n")
    fails("print(type_name(int, str))\n", "type_name takes 1 argument")
    fails("print(type_name(Nope))\n", "Nope")


@needs_rustc
def test_type_name_resolves_per_instance(tmp_path):
    src = (
        "struct Box[T]:\n    v: T\n\n"
        "fn label[T](x: T) -> str:\n    return type_name(T) + \"/\" + type_name(list[T])\n\n"
        "print(label(1), label(\"a\"), label(Box(1.5)))\n"
        "print(type_name(Box[int]))\n"
    )
    assert run(src, tmp_path) == "int/list[int] str/list[str] Box[float]/list[Box[float]]\nBox[int]\n"


# ---- already narrowed ----------------------------------------------------------------------------------------

def test_unwrap_on_a_narrowed_field_is_a_precise_error():
    src = "struct Op:\n    field: str?\nfn f(op: Op) -> str:\n    if op.field is some:\n        return op.field.unwrap()\n    return \"\"\n"
    e = fails(src, "already narrowed", line=5)
    assert "drop `.unwrap()`" in str(e) and "'op.field' is str" in str(e)


def test_unwrap_on_a_narrowed_variable_and_result():
    fails("fn f(x: int?) -> int:\n    if x is some:\n        return x.unwrap_or(0)\n    return 0\n", r"drop `\.unwrap_or\(\.\.\.\)`")
    fails("fn f(r: Result[int, str]) -> int:\n    if r is ok:\n        return r.unwrap()\n    return 0\n", "already narrowed")


def test_unwrap_still_works_outside_the_narrowed_region():
    check(parse("fn f(x: int?) -> int:\n    if x is some:\n        return x\n    return x.unwrap_or(0)\n"))


def test_check_all_reports_the_lost_mutation_next_to_other_errors():
    prog = parse(A + LOST + "y: int = \"s\"\n")
    assert len(check_all(prog)) == 2
