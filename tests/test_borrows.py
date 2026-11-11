"""Borrow inference: parameters a function never modifies are lent, not cloned."""
import shutil

import pytest

from zylo import ast
from zylo.checker import check
from zylo.cli import transpile_source
from zylo.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")


def params(src: str, fn: str = "f") -> dict[str, bool]:
    prog = check(parse(src))
    f = next(s for s in prog.body if isinstance(s, ast.FnDef) and s.name == fn)
    return {p.name: p.info.by_ref for p in f.params}


def method_params(src: str, method: str) -> dict[str, bool]:
    prog = check(parse(src))
    s = next(s for s in prog.body if isinstance(s, ast.StructDef))
    m = next(m for m in s.methods if m.name == method)
    return {p.name: p.info.by_ref for p in m.params}


# ---- which parameters are lent ---------------------------------------------------------------------

def test_read_only_non_copy_parameters_are_borrowed():
    got = params("fn f(xs: list[int], s: str, d: dict[str, int], t: tuple[str, int], n: int, x: float) -> int:\n    return len(xs) + len(s) + len(d) + n\n")
    assert got == {"xs": True, "s": True, "d": True, "t": True, "n": False, "x": False}


def test_copy_types_are_never_borrowed():
    assert params("fn f(a: int, b: float, c: bool, d: int?, e: tuple[int, int]):\n    print(a, b, c, d, e)\n") == {
        "a": False, "b": False, "c": False, "d": False, "e": False}


def test_modified_parameters_are_taken_by_value():
    assert params("fn f(xs: list[int]):\n    append(xs, 1)\n") == {"xs": False}
    assert params("fn f(xs: list[int]):\n    xs[0] = 1\n") == {"xs": False}
    assert params("fn f(xs: list[int]):\n    xs = [1]\n") == {"xs": False}
    assert params("fn f(xs: list[int]):\n    xs.sort()\n") == {"xs": False}
    assert params("fn f(d: dict[str, int]):\n    d[\"a\"] = 1\n") == {"d": False}
    assert params("struct P:\n    n: int\nfn f(p: P):\n    p.n = 1\n") == {"p": False}
    assert params("struct P:\n    n: int\n\n    fn bump(self):\n        self.n += 1\nfn f(p: P):\n    p.bump()\n") == {"p": False}


def test_only_the_modified_parameter_is_by_value():
    assert params("fn f(a: list[int], b: list[int]):\n    append(b, len(a))\n") == {"a": True, "b": False}


def test_struct_and_option_parameters():
    assert params("struct P:\n    name: str\nfn f(p: P, o: str?) -> int:\n    return len(p.name)\n") == {"p": True, "o": True}


def test_methods_that_modify_self_take_their_arguments_by_value():
    src = "struct B:\n    items: list[str]\n\n    fn add(self, x: str):\n        append(self.items, x)\n\n    fn size(self, x: str) -> int:\n        return len(x)\n"
    assert method_params(src, "add") == {"x": False}
    assert method_params(src, "size") == {"x": True}


# ---- generated Rust --------------------------------------------------------------------------------------

def test_signatures_and_calls_use_references():
    rs = transpile_source("fn total(xs: list[int]) -> int:\n    return len(xs)\ndata = [1, 2]\nprint(total(data))\nprint(total([3]))\n")
    assert "fn total(xs: &Vec<i64>) -> i64" in rs
    assert "total(&data)" in rs and "total(&vec![3i64])" in rs
    assert ".clone()" not in rs


def test_modified_parameter_is_still_cloned_at_the_call():
    rs = transpile_source("fn grow(xs: list[int]):\n    append(xs, 1)\ndata = [1]\ngrow(data)\n")
    assert "fn grow(mut xs: Vec<i64>)" in rs and "grow(data.clone())" in rs


def test_returning_or_storing_a_borrowed_parameter_clones_it():
    rs = transpile_source("fn keep(xs: list[int]) -> list[int]:\n    return xs\n")
    assert "return (*xs).clone();" in rs


def test_loops_over_read_only_lists_do_not_clone():
    rs = transpile_source("xs = [1, 2]\nfor x in xs:\n    print(x)\n")
    assert "for x in xs.iter().cloned()" in rs
    rs = transpile_source("xs = [1, 2]\nfor x in xs:\n    append(xs, x)\n")
    assert "for x in xs.clone()" in rs  # the loop sees a snapshot, like Python


def test_a_borrowed_parameter_can_be_passed_on_and_iterated():
    rs = transpile_source("fn inner(xs: list[str]) -> int:\n    return len(xs)\nfn outer(xs: list[str]) -> int:\n    n = 0\n    for x in xs:\n        n += len(x)\n    return n + inner(xs)\n")
    assert "inner(&(*xs))" in rs and "for x in (*xs).iter().cloned()" in rs


# ---- end to end ---------------------------------------------------------------------------------------------------

@needs_rustc
def test_borrowed_calls_keep_value_semantics():
    from zylo.cli import compile_rust
    import subprocess
    from pathlib import Path
    import tempfile
    src = ("fn evil(xs: list[int]) -> int:\n    xs[0] = 100\n    return xs[0]\n"
           "fn peek(xs: list[int]) -> int:\n    return xs[0]\n"
           "data = [1, 2]\nprint(evil(data), peek(data), data)\n")
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "p"
        compile_rust(transpile_source(src), exe)
        assert subprocess.run([str(exe)], capture_output=True, text=True).stdout == "100 1 [1, 2]\n"
