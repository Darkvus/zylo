"""`Cell[T]`: a shared mutable cell that closures, structs and functions can hold."""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application.compile import load_program, transpile_source
from zylo.domain.errors import ZyloError
from zylo.domain.types import INT, CellT, ListT
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check, check_all
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")


def fails(src: str, match: str) -> None:
    with pytest.raises(ZyloError, match=match):
        check(parse(src))


def run(src: str, tmp_path: Path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout


def test_types():
    prog = check(parse("c = cell(1)\nl: Cell[list[int]] = cell([])\n"))
    assert prog.body[0].value.ty == CellT(INT)
    assert prog.body[1].value.ty == CellT(ListT(INT))
    assert str(CellT(INT)) == "Cell[int]"


def test_rust_type_and_methods():
    rust = transpile_source("c = cell(1)\nc.set(c.get() + 1)\nc.update(fn(n: int) => n * 2)\n")
    assert "__ZyCell::new(1i64)" in rust
    assert ".update(" in rust


def test_wrong_set_type():
    fails("c = cell(1)\nc.set('x')\n", "expected int, got str")


def test_wrong_update_function():
    fails("c = cell(1)\nc.update(fn(s: str) => s)\n", "expected here|is str but int")


def test_unknown_method_and_arity():
    fails("c = cell(1)\nc.push(1)\n", "no method 'push'")
    fails("c = cell(1)\nc.get(1)\n", "takes 0 argument")


def test_annotation_mismatch():
    fails("c: Cell[str] = cell(1)\n", "str")


def test_cell_cannot_hold_nothing():
    fails("fn f():\n    return\nc = cell(f())\n", "no value")


def test_cell_is_reserved_type_name():
    fails("struct Cell:\n    x: int\n", "reserved")


def test_cell_of_function_cannot_be_printed():
    fails("c = cell(fn(x: int) => x)\nprint(c)\n", "functions cannot be printed")


def test_cell_not_json_data(tmp_path):
    f = tmp_path / "m.zy"
    f.write_text("struct S:\n    c: Cell[int]\ns = S(cell(1))\nj = to_json(s)\n")
    errors = check_all(load_program(f))
    assert any("shared mutable state" in str(e) for e in errors)


def test_closure_hint_mentions_cell():
    src = "fn mk() -> fn() -> int:\n    n = 0\n    fn f() -> int:\n        n = n + 1\n        return n\n    return f\n"
    fails(src, "Cell")


def test_variable_named_cell_still_works():
    check(parse("cell = 3\nprint(cell + 1)\n"))


@needs_rustc
def test_closures_share_state(tmp_path):
    src = (
        "fn counter(c: Cell[int]) -> fn() -> int:\n"
        "    fn next() -> int:\n"
        "        c.update(fn(n: int) => n + 1)\n"
        "        return c.get()\n"
        "    return next\n"
        "c = cell(0)\n"
        "a = counter(c)\nb = counter(c)\n"
        "print(a(), b(), a(), c.get())\n"
    )
    assert run(src, tmp_path) == "1 2 3 3\n"


@needs_rustc
def test_struct_with_cell_derives(tmp_path):
    src = (
        "struct S:\n    n: Cell[int]\n    name: str\n"
        "a = S(cell(1), 'x')\nb = a\nb.n.set(5)\n"
        "print(a.n.get(), a == b, a)\n"
    )
    assert run(src, tmp_path) == '5 true S(n=Cell(5), name="x")\n'
