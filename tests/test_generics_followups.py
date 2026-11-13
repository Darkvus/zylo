"""Generics follow-ups: generic methods, generic functions as values, numeric literals of `Num` parameters, type arguments
that are lowercase structs of another module, and hover inside generic bodies."""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application import editor
from zylo.application.compile import transpile_source
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import FLOAT, INT, STR, FnT, ListT
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check, check_all
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

BOX = """\
struct Box[T]:
    value: T

    fn get(self) -> T:
        return self.value

    fn map[U](self, f: fn(T) -> U) -> Box[U]:
        return Box(value=f(self.value))

    fn pair[U](self, other: U) -> tuple[T, U]:
        return (self.value, other)

"""

FIRST = """\
fn first[T](xs: list[T]) -> T:
    return xs[0]

"""


def fails(src: str, match: str, line: int | None = None) -> None:
    with pytest.raises(ZyloError, match=match) as info:
        check(parse(src))
    if line is not None:
        assert info.value.line == line


def types_of(src: str) -> list:
    prog = check(parse(src))
    return [s.value.ty for s in prog.body if isinstance(s, ast.Assign)]


def run(src: str, tmp_path: Path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout


# ---- syntax -------------------------------------------------------------------------------------------------

def test_a_method_can_declare_type_parameters():
    prog = parse(BOX)
    method = next(m for m in prog.body[0].methods if m.name == "map")
    assert [p.name for p in method.tparams] == ["U"]


def test_extern_functions_still_cannot_be_generic():
    with pytest.raises(ZyloError, match="cannot be generic"):
        parse('extern fn f[T](x: T) = "x"\n')


def test_generic_method_formats_and_keeps_explicit_arguments():
    src = BOX + "b = Box(value=1)\nprint(b.map[str](fn(x) => str(x)).get())\n"
    once = format_source(src)
    assert "    fn map[U](self, f: fn(T) -> U) -> Box[U]:" in once
    assert "b.map[str](fn(x) => str(x))" in once
    assert format_source(once) == once


def test_a_comma_in_brackets_before_a_call_is_kept_as_type_arguments():
    prog = parse("a = pick[point, int](p, 3)\n")
    call = prog.body[0].value
    assert isinstance(call, ast.CallValue) and isinstance(call.callee.index, ast.TupleLit) and call.callee.index.bare
    assert format_source("a = pick[point,int](p, 3)\n") == "a = pick[point, int](p, 3)\n"
    with pytest.raises(ZyloError):
        parse("a = d[1, 2]\n")  # still an error: it is not followed by a call


def test_generic_function_used_as_a_value_with_builtin_types_parses_as_a_name():
    prog = parse("g = first[int]\nh = util.first[list[str]]\nk = xs[i]\n")
    g, h, k = (s.value for s in prog.body)
    assert isinstance(g, ast.Name) and g.targs[0].name == "int"
    assert isinstance(h, ast.Attr) and h.targs[0].name == "list"
    assert isinstance(k, ast.Index)


# ---- generic methods ----------------------------------------------------------------------------------------

def test_the_type_parameters_of_a_method_are_inferred_or_written():
    t = types_of(BOX + "b = Box(value=21)\na = b.map(fn(x) => str(x))\nc = b.map[float](fn(x) => float(x))\nd = b.pair(true)\n")
    assert str(t[1]) == "Box[str]" and str(t[2]) == "Box[float]" and str(t[3]) == "tuple[int, bool]"


def test_generic_methods_on_an_ordinary_struct_and_an_enum():
    src = ("struct Log:\n    n: int\n\n    fn tag[T: Show](self, x: T) -> str:\n        return f\"{x}{self.n}\"\n\n"
           "enum Opt[T]:\n    Some(T)\n    Empty\n\n    fn map[U](self, f: fn(T) -> U) -> Opt[U]:\n        match self:\n"
           "            Opt.Some(x):\n                return Opt.Some(f(x))\n            Opt.Empty:\n                return Opt.Empty\n\n"
           "a = Log(n=1).tag(2.5)\nb = Opt.Some(3).map(fn(x) => x > 2)\n")
    t = types_of(src)
    assert t[0] == STR and str(t[1]) == "Opt[bool]"


def test_a_method_copy_is_an_ordinary_method_of_the_instance():
    prog = check(parse(BOX + "b = Box(value=1)\nx = b.map(fn(v) => str(v))\n"))
    inst = next(s for s in prog.body if isinstance(s, ast.StructDef) and s.name == "Box[int]")
    names = [m.name for m in inst.methods]
    assert "map[str]" in names
    copy = next(m for m in inst.methods if m.name == "map[str]")
    assert copy.instance and not copy.tparams and copy.ty == FnT((FnT((INT,), STR),), types_of(BOX + "y = Box(value='s')\n")[0])


def test_a_generic_method_needs_a_distinct_name_for_each_parameter():
    fails("struct B[T]:\n    v: T\n\n    fn m[T](self, x: T) -> T:\n        return x\n", "hides the one of the same name", 4)
    fails("struct B:\n    v: int\n\n    fn m[U: Foo](self, x: U) -> U:\n        return x\n", "unknown bound 'Foo'", 4)


def test_generic_method_calls_are_checked():
    base = "struct B:\n    v: int\n\n    fn m[U](self, x: U) -> U:\n        return x\n\n    fn plain(self) -> int:\n        return self.v\n"
    fails(base + "print(B(v=1).m[int, str](1))\n", "takes 1 type argument", 9)
    fails(base + "print(B(v=1).plain[int]())\n", "is not generic", 9)
    fails(base + "print(B(v=1).m(1, 2))\n", "takes 1 argument", 9)
    fails("struct B:\n    v: int\n\n    fn m[U: Ord](self, x: U) -> U:\n        return x\n\nprint(B(v=1).m([1]))\n",
          "cannot be used for U, which is bound by `Ord`", 7)
    fails("struct B:\n    v: int\n\n    fn m[U](self) -> U:\n        return self.v\n", "returning int but the function declares U", 5)


def test_a_parameter_nothing_determines_is_reported():
    fails("struct B:\n    v: int\n\n    fn make[U](self) -> list[U]:\n        return []\n\nprint(B(v=1).make())\n",
          "cannot infer type parameter U of B.make", 7)


def test_a_mistake_in_a_generic_method_is_reported_once_even_if_it_is_never_called():
    src = "struct B[T]:\n    v: T\n\n    fn m[U](self, x: U) -> U:\n        return x + x\n"
    assert [e.line for e in check_all(parse(src))] == [5]
    called = src + "b = B(v=1)\nprint(b.m(1))\nprint(b.m(2.5))\nc = B(v='s')\nprint(c.m(1))\n"
    assert [e.line for e in check_all(parse(called))] == [5]


def test_a_receiver_of_the_callers_own_parameter_does_not_clash_with_the_methods():
    src = BOX + "fn wrap[U](b: Box[U]) -> Box[str]:\n    return b.map(fn(x) => \"a\")\nr = wrap(Box(value=2))\n"
    assert str(types_of(src)[-1]) == "Box[str]"


def test_methods_that_change_self_through_a_generic_method_are_found():
    src = ("struct C:\n    n: int\n\n    fn add[T](self, x: T, f: fn(T) -> int):\n        self.n = self.n + f(x)\n\n"
           "    fn go(self):\n        self.add(3, fn(i) => i)\n\nc = C(n=0)\nc.go()\n")
    prog = check(parse(src))
    go = next(m for m in prog.body[0].methods if m.name == "go")
    assert go.self_mut


# ---- generic functions as values ----------------------------------------------------------------------------

def test_a_generic_function_can_be_written_with_its_type_arguments_as_a_value():
    t = types_of(FIRST + "g = first[int]\nh = [first[str]]\n")
    assert t[0] == FnT((ListT(INT),), INT) and t[1] == ListT(FnT((ListT(STR),), STR))


def test_a_generic_function_takes_its_type_arguments_from_the_expected_type():
    src = (FIRST + "fn use(f: fn(list[float]) -> float) -> float:\n    return f([1.5])\n"
           "fn lift[T, U](xs: list[T], f: fn(T) -> U) -> list[U]:\n    return []\n"
           "a: fn(list[int]) -> int = first\nb = use(first)\nc = lift([[1], [2]], first)\n")
    t = types_of(src)
    assert t[0] == FnT((ListT(INT),), INT) and t[1] == FLOAT and str(t[2]) == "list[int]"


def test_a_generic_function_value_inside_a_generic_function():
    src = FIRST + "fn via[T](xs: list[T]) -> T:\n    f: fn(list[T]) -> T = first\n    return f(xs)\nr = via([1])\n"
    assert types_of(src)[-1] == INT


def test_a_generic_function_used_as_a_value_needs_its_type_arguments_to_be_decided():
    fails(FIRST + "g = first\n", "cannot infer type parameter T of first used as a value", 4)
    fails(FIRST + "g: fn(int) -> int = first\n", "does not fit fn\\(int\\) -> int", 4)
    fails(FIRST + "g = first[int, str]\n", "takes 1 type argument", 4)
    fails("fn big[T: Ord](xs: list[T]) -> T:\n    return xs[0]\nstruct P:\n    x: int\ng: fn(list[P]) -> P = big\n",
          "cannot be used for T, which is bound by `Ord`", 5)
    fails("x = 1\ny = x[int]\n", "is a variable, so it takes no type arguments", 2)


# ---- numeric literals ---------------------------------------------------------------------------------------

def test_an_integer_literal_can_take_the_type_of_a_num_parameter():
    src = ("fn inc[T: Num](a: T) -> T:\n    return a + 1\nfn flip[T: Num](a: T) -> T:\n    return 10 - a\n"
           "fn zero[T: Num](xs: list[T]) -> T:\n    acc: T = 0\n    for x in xs:\n        acc = acc + x\n    return acc\n"
           "fn neg[T: Num](a: T) -> bool:\n    return a < -1\n"
           "a = inc(1)\nb = inc(1.5)\nc = flip(2)\nd = zero([0.5])\ne = neg(2.5)\n")
    assert types_of(src) == [INT, FLOAT, INT, FLOAT, types_of("x = true\n")[0]]


def test_literals_still_need_the_num_bound_and_floats_are_never_taken_for_t():
    fails("fn f[T](a: T) -> T:\n    return a + 1\n", "integer literal can only be combined with a type parameter bounded by `Num`", 2)
    fails("fn f[T: Ord](a: T) -> bool:\n    return a > 0\n", "bounded by `Num`", 2)
    fails("fn f[T: Num](a: T) -> T:\n    return a * 1.5\n", "a float literal is a float", 2)
    fails("fn f(a: float) -> float:\n    return a + 1\n", "types must match", 2)


# ---- type arguments that look like an index -----------------------------------------------------------------

def modules(tmp_path: Path) -> Path:
    (tmp_path / "util.zy").write_text(
        "struct point:\n    x: int\n    y: int\n\nstruct Wrap[T]:\n    v: T\n\n"
        "    fn map[U](self, f: fn(T) -> U) -> Wrap[U]:\n        return Wrap(v=f(self.v))\n\n" + FIRST +
        "fn pick[A, B](a: A, b: B) -> tuple[A, B]:\n    return (a, b)\n\nfn bump[T: Num](x: T) -> T:\n    return x + 1\n")
    main = tmp_path / "main.zy"
    main.write_text("""\
import util
from util import first, point, pick, Wrap, bump

xs = [point(x=1, y=2), point(x=3, y=4)]
print(first[point](xs))
print(util.first[point](xs))
g = first[point]
print(g(xs))
h = util.first[point]
print(h(xs))
print(pick[point, int](xs[0], 3))
print(util.pick[point, str](xs[1], "s"))
k: fn(list[point]) -> point = first
print(k(xs))
print(Wrap(v=xs[0]).map(fn(p) => p.x + 1).v)
table = [first[int]]
print(table[0]([8]))
idx = [10, 20]
n = 1
print(idx[n])
print(bump(2), bump(2.5))
""")
    return main


@needs_rustc
def test_lowercase_struct_type_arguments_from_another_module(tmp_path):
    main = modules(tmp_path)
    exe = tmp_path / "prog"
    compile_rust(pipeline.transpile(main), exe)
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
    assert out == ["point(x=1, y=2)"] * 4 + ["(point(x=1, y=2), 3)", '(point(x=3, y=4), "s")', "point(x=1, y=2)", "2", "8", "20",
                                             "3 3.5"]


def test_a_function_name_with_brackets_that_hold_no_type_is_explained(tmp_path):
    (tmp_path / "util.zy").write_text(FIRST)
    main = tmp_path / "main.zy"
    main.write_text("from util import first\nxs = [1]\ny = first[xs + 1](xs)\n")
    with pytest.raises(ZyloError, match="must hold type arguments"):
        pipeline.transpile(main)


def test_indexing_a_variable_that_holds_functions_is_unchanged(tmp_path):
    main = tmp_path / "main.zy"
    main.write_text("fn one(x: int) -> int:\n    return x\nfirst = [one]\nprint(first[0](5))\n")
    exe = tmp_path / "prog"
    compile_rust(pipeline.transpile(main), exe)
    assert subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout == "5\n"


# ---- running ------------------------------------------------------------------------------------------------

@needs_rustc
def test_run_generic_methods_values_and_literals(tmp_path):
    src = BOX + FIRST + """\
fn double[T: Num](x: T) -> T:
    return x * 2

fn twice[T](x: T, f: fn(T) -> T) -> T:
    return f(f(x))

b = Box(value=21)
print(b.map(fn(x) => x * 2).get(), b.map(fn(x) => f"<{x}>").get(), b.map[float](fn(x) => float(x) / 2.0).get())
print(b.pair("a"))
g = first[int]
print(g([4, 5]), twice(3, double), twice(1.5, double))
"""
    assert run(src, tmp_path) == '42 <21> 10.5\n(21, "a")\n4 12 6.0\n'


# ---- the editor ---------------------------------------------------------------------------------------------

HOVER = """\
struct Box[T]:
    value: T

    fn map[U](self, f: fn(T) -> U) -> Box[U]:
        mapped = f(self.value)
        return Box(value=mapped)

fn total[T: Num](xs: list[T]) -> T:
    acc: T = 0
    for x in xs:
        acc = acc + x
    return acc

fn unused[A: Ord](a: A) -> A:
    return a

print(total([1, 2]), total([0.5]))
b = Box(value=1)
print(b.map(fn(x) => str(x)).value)
"""


def hover_text(tmp_path, line: int, word: str) -> str:
    f = tmp_path / "m.zy"
    f.write_text(HOVER)
    proj = editor.open_project(f)
    col = HOVER.splitlines()[line - 1].index(word) + 1
    h = editor.hover(proj, f, line, col)
    assert h is not None
    return h.markdown


def test_hover_inside_a_generic_body_lists_the_instantiations(tmp_path):
    text = hover_text(tmp_path, 9, "acc")
    assert "(variable) acc: T" in text
    assert "`T: Num`, a type parameter of `total`: instantiated with `int`, `float`" in text


def test_hover_without_instantiations_shows_the_bound(tmp_path):
    text = hover_text(tmp_path, 15, "a")
    assert "`A: Ord`, a type parameter of `unused`: not instantiated anywhere in this program" in text


def test_hover_in_a_generic_method_names_the_parameters_it_mentions(tmp_path):
    text = hover_text(tmp_path, 5, "mapped")
    assert "(variable) mapped: U" in text and "`U`, a type parameter of `map`: instantiated with `str`" in text
    assert "`T`" not in text
    field = hover_text(tmp_path, 2, "value")
    assert "`T`, a type parameter of `Box`: instantiated with `int`, `str`" in field


def test_hover_on_a_generic_signature_shows_its_parameters(tmp_path):
    text = hover_text(tmp_path, 4, "map")
    assert "fn Box.map[U](self, f: fn(T) -> U) -> Box[U]" in text and "`U`" in text and "`T`" in text


def test_a_generic_method_can_be_renamed_with_its_calls(tmp_path):
    f = tmp_path / "m.zy"
    f.write_text(HOVER)
    proj = editor.open_project(f)
    edits = editor.rename(proj, f, 4, 8, "convert")
    assert sorted(e.place.line for e in edits) == [4, 19]
