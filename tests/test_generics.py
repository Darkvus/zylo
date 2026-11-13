"""Generics: functions, structs and enums with type parameters, expanded per type by the checker."""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application import editor
from zylo.application.compile import diagnose_source, transpile_source
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import INT, STR, EnumT, ListT, ParamT, StructT
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check, check_all
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

LIB = """\
struct Box[T]:
    value: T

    fn get(self) -> T:
        return self.value

    fn set(self, v: T):
        self.value = v

enum Opt[T]:
    Some(T)
    Empty

    fn or_else(self, fallback: T) -> T:
        match self:
            Opt.Some(x):
                return x
            Opt.Empty:
                return fallback

fn first[T](xs: list[T]) -> T:
    return xs[0]

fn largest[T: Ord](xs: list[T]) -> T:
    best = xs[0]
    for x in xs:
        if x > best:
            best = x
    return best

fn map_all[T, U](xs: list[T], f: fn(T) -> U) -> list[U]:
    out: list[U] = []
    for x in xs:
        append(out, f(x))
    return out

"""


def fails(src: str, match: str, line: int | None = None) -> None:
    with pytest.raises(ZyloError, match=match) as info:
        check(parse(src))
    if line is not None:
        assert info.value.line == line


def tys(prog: ast.Program) -> list:
    """The types of the top-level assignments, in order (copies of generics come after the program's own statements)."""
    return [s.value.ty for s in prog.body if isinstance(s, ast.Assign)]


def run(src: str, tmp_path: Path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout


# ---- syntax ---------------------------------------------------------------------------------------------

def test_parse_definitions():
    prog = parse("fn f[T, U: Ord + Show](x: T) -> T:\n    return x\nstruct S[A]:\n    a: A\nenum E[A]:\n    V(A)\n")
    f, s, e = prog.body
    assert [(p.name, p.bounds) for p in f.tparams] == [("T", []), ("U", ["Ord", "Show"])]
    assert [p.name for p in s.tparams] == ["A"] and [p.name for p in e.tparams] == ["A"]


def test_parse_explicit_type_arguments():
    prog = parse("a = first[int](xs)\nb = Box[list[str]](value=[])\nc = Opt[int].Some(1)\nd = lib.first[Point](xs)\n")
    a, b, c, d = (s.value for s in prog.body)
    assert isinstance(a, ast.Call) and [t.name for t in a.targs] == ["int"]
    assert isinstance(b, ast.Call) and b.targs[0].name == "list" and b.targs[0].args[0].name == "str"
    assert isinstance(c, ast.MethodCall) and c.value.targs[0].name == "int"
    assert isinstance(d, ast.MethodCall) and d.targs[0].name == "Point"


def test_indexing_a_variable_is_still_indexing():
    prog = parse("a = table[key](x)\nb = table[0](x)\nc = handlers[name_of(x)](x)\nd = xs[i]\n")
    assert all(isinstance(s.value, (ast.CallValue, ast.Index)) for s in prog.body)


def test_type_arguments_of_a_generic_of_this_file_may_be_lowercase():
    prog = parse("fn first[T](xs: list[T]) -> T:\n    return xs[0]\na = first[point](xs)\n")
    assert isinstance(prog.body[1].value, ast.Call)


def test_parse_errors():
    with pytest.raises(ZyloError, match="own type parameters"):
        parse("struct S[T]:\n    a: T\n\n    fn m[U](self, u: U):\n        return\n")
    with pytest.raises(ZyloError, match="duplicate type parameter"):
        parse("fn f[T, T](x: T):\n    return\n")
    with pytest.raises(ZyloError):
        parse("fn f[](x: int):\n    return\n")


def test_formatter_round_trip():
    src = LIB + "x = Box[int](value=1)\ny = Opt[int].Some(2)\nz = first[int]([1])\n"
    once = format_source(src)
    assert once == src.rstrip("\n") + "\n" or format_source(once) == once
    assert "fn largest[T: Ord](xs: list[T]) -> T:" in once
    assert "x = Box[int](value=1)" in once and "y = Opt[int].Some(2)" in once


# ---- types and inference ----------------------------------------------------------------------------------

def test_inference_from_arguments_and_types():
    prog = check(parse(LIB + "a = first([1, 2])\nb = first([\"x\"])\nc = Box(value=2.5)\nd = Opt.Some(1)\ne = largest([\"a\"])\n"))
    a, b, c, d, e = tys(prog)
    assert (a, b, e) == (INT, STR, STR)
    assert c == StructT("Box[float]") and c.base == "Box" and str(c) == "Box[float]"
    assert d == EnumT("Opt[int]") and d.args == (INT,)


def test_inference_from_the_expected_type():
    src = LIB + "e: Opt[str] = Opt.Empty\nb: Box[list[int]] = Box(value=[])\nfn none_of[T]() -> Opt[T]:\n    return Opt.Empty\nn: Opt[int] = none_of()\n"
    prog = check(parse(src))
    e, b, n = tys(prog)
    assert e == EnumT("Opt[str]") and str(b) == "Box[list[int]]" and n == EnumT("Opt[int]")


def test_none_and_empty_list_arguments_take_their_type_from_the_others():
    src = "fn or_default[T](x: T?, d: T) -> T:\n    return d\nfn pad[T](xs: list[T], extra: list[T]) -> int:\n    return 1\na = or_default(none, 3)\nb = pad([1], [])\n"
    check(parse(src))


def test_function_arguments_infer_the_result_type():
    prog = check(parse(LIB + "a = map_all([1, 2], fn(x: int) => str(x))\nb = map_all([1], fn(x) => x > 0)\n"))
    a, b = tys(prog)
    assert a == ListT(STR) and str(b) == "list[bool]"


def test_explicit_instantiation_overrides_nothing_but_checks_arguments():
    prog = check(parse(LIB + "a = first[float]([1.5])\nb = Box[int](value=1)\nc = Opt[str].Empty\n"))
    a, b, c = tys(prog)
    assert (str(a), str(b), str(c)) == ("float", "Box[int]", "Opt[str]")
    fails(LIB + "a = first[int]([\"x\"])\n", "expected list\\[int\\], got list\\[str\\]")


def test_instances_are_ordinary_definitions_and_the_template_is_not_emitted():
    rust = transpile_source(LIB + "a = Box(value=1)\nb = Box(value=\"s\")\nc = first([1])\n")
    assert "struct Box_5b_int_5d_" in rust and "struct Box_5b_str_5d_" in rust
    assert "fn first_5b_int_5d_" in rust
    assert "struct Box " not in rust and "fn first(" not in rust


def test_checking_twice_is_stable():
    prog = parse(LIB + "a = Box(value=1)\na.set(2)\nb = first([1])\n")
    assert check_all(prog) == []
    count = len(prog.body)
    assert check_all(prog) == []
    assert len(prog.body) == count


def test_type_parameter_of_a_template_is_a_param_type():
    prog = parse(LIB)
    assert check_all(prog) == []
    first = next(s for s in prog.body if isinstance(s, ast.FnDef) and s.name == "first")
    assert first.ty.ret == ParamT("T")


# ---- errors -----------------------------------------------------------------------------------------------

def test_wrong_number_of_type_arguments():
    fails("fn f[A, B](a: A, b: B) -> A:\n    return a\nx = f[int](1, 2)\n", "takes 2 type argument\\(s\\)", 3)
    fails(LIB + "x: Box[int, str] = Box(value=1)\n", "takes 1 type argument", None)
    fails(LIB + "x: Box = Box(value=1)\n", "takes 1 type argument", None)


def test_non_generic_with_type_arguments():
    fails("fn f(x: int) -> int:\n    return x\ny = f[int](1)\n", "not generic", 3)
    fails("struct P:\n    a: int\nx: P[int] = P(a=1)\n", "not generic", 3)


def test_uninferable_type_parameter():
    fails("fn make[T]() -> list[T]:\n    return []\nx = make()\n", "cannot infer type parameter T of make", 3)
    fails(LIB + "x = Opt.Empty\n", "cannot infer type parameter T", None)


def test_mismatched_arguments():
    fails("fn same[T](a: T, b: T) -> T:\n    return a\nx = same(1, \"a\")\n", "argument 2 of same: expected int, got str", 3)
    fails(LIB + "x = Box(value=1, other=2)\n", "has no field 'other'")
    fails(LIB + "x = first(1)\n", "expected list\\[T\\], got int")


def test_a_template_is_checked_for_every_type():
    fails("fn f[T](a: T, b: T) -> bool:\n    return a > b\n", "`>` is not defined for T", 2)
    fails("fn f[T](a: T):\n    print(a)\n", "needs the `Show` bound", 2)
    fails("fn f[T](a: T, b: T) -> bool:\n    return a == b\n", "needs the `Eq` bound", 2)
    fails("fn f[T](a: T) -> T:\n    return 1\n", "returning int but the function declares T", 2)
    fails("fn f[T](a: T) -> T:\n    return a + a\n", "`\\+` is not defined for T", 2)
    fails("fn f[K](d: dict[K, int]):\n    return\n", "dict keys must be")
    fails("struct S[T]:\n    a: T\n\n    fn m(self) -> int:\n        return self.a\n", "returning T", 5)


def test_bounds_allow_what_they_promise():
    check(parse("fn f[T: Ord](a: T, b: T) -> bool:\n    return a >= b\n"))
    check(parse("fn f[T: Eq](a: T, b: T) -> bool:\n    return a != b\n"))
    check(parse("fn f[T: Show](a: T) -> str:\n    return f\"<{a}>\"\n"))
    check(parse("fn f[T: Num](a: T, b: T) -> T:\n    return -(a * b) + a % b\n"))
    check(parse("fn f[K: Key](d: dict[K, int], k: K) -> bool:\n    return k in d\n"))
    check(parse("fn f[T: Ord](xs: list[T]) -> list[T]:\n    return sorted(xs)\n"))
    check(parse("fn f[T: Num](a: T) -> float:\n    return float(a)\n"))


def test_type_arguments_must_satisfy_bounds():
    base = "fn f[T: {b}](a: T) -> T:\n    return a\nstruct P:\n    a: int\n"
    fails(base.format(b="Num") + "x = f(\"a\")\n", "str cannot be used for T, which is bound by `Num`", 5)
    fails(base.format(b="Ord") + "x = f(P(a=1))\n", "which is bound by `Ord`")
    fails(base.format(b="Key") + "x = f(1.5)\n", "which is bound by `Key`")
    fails(base.format(b="Eq") + "x = f(fn(n: int) => n)\n", "which is bound by `Eq`")
    check(parse(base.format(b="Num") + "x = f(1)\ny = f(2.5)\n"))


def test_unknown_bound_and_duplicate_names():
    fails("fn f[T: Foo](a: T) -> T:\n    return a\n", "unknown bound 'Foo'", 1)
    fails("struct A[T]:\n    a: T\nstruct A:\n    b: int\n", "already")
    fails("fn f[T](a: T) -> T:\n    return a\nfn f(a: int) -> int:\n    return a\n", "already defined")


def test_a_generic_function_is_not_a_value_and_a_generic_enum_is_not_a_call():
    fails("fn f[T](a: T) -> T:\n    return a\ng = f\n", "generic function and cannot be used as a value", 3)
    fails(LIB + "x = Opt(1)\n", "'Opt' is an enum")


def test_struct_that_contains_itself_is_rejected_but_a_list_is_fine():
    fails("struct N[T]:\n    next: N[T]\n", "contains itself")
    check(parse("struct N[T]:\n    next: list[N[T]]\n    v: T\nx: N[int] = N(next=[], v=1)\n"))


def test_unbounded_expansion_is_stopped():
    src = "struct W[T]:\n    v: T\nfn f[T](x: T, n: int) -> int:\n    if n == 0:\n        return 0\n    return f(W(v=x), n - 1)\ny = f(1, 3)\n"
    fails(src, "generic expansion too deep", 6)


def test_a_mistake_in_a_template_is_reported_once():
    src = "fn f[T](a: T) -> int:\n    return a + 1\nx = f(1)\ny = f(\"s\")\nz = f(2.5)\n"
    errors = check_all(parse(src))
    assert len(errors) == 1 and errors[0].line == 2


def test_unused_templates_are_still_checked():
    errors = check_all(parse("fn f[T](a: T) -> int:\n    return a\nstruct S[T]:\n    a: T\n\n    fn m(self) -> str:\n        return 1\n"))
    assert [e.line for e in errors] == [2, 7]


def test_match_on_a_generic_enum_is_exhaustive():
    fails(LIB + "o = Opt.Some(1)\nmatch o:\n    Opt.Some(x):\n        print(x)\n", "does not cover Opt\\[int\\].Empty")
    check(parse(LIB + "o = Opt.Some(1)\nmatch o:\n    Opt[int].Some(x):\n        print(x)\n    Opt.Empty:\n        print(0)\n"))


def test_pattern_on_another_enum_is_rejected():
    fails(LIB + "enum Other:\n    A\no = Other.A\nmatch o:\n    Opt.Some(x):\n        print(x)\n    Opt.Empty:\n        print(0)\n",
          "variant pattern|this `match` is on")


# ---- running ------------------------------------------------------------------------------------------------

@needs_rustc
def test_run_generics(tmp_path):
    out = run(LIB + """\
b = Box(value=3)
b.set(b.get() + 1)
print(b, Box(value="s"), Box(value=[1, 2]))
print(first([7, 8]), first(["a"]), largest([2, 9, 4]), largest(["b", "a"]), largest([0.5, 1.5]))
print(map_all([1, 2], fn(x: int) => x * 10), map_all(["a"], fn(s) => s + s))
e: Opt[str] = Opt.Empty
print(e.or_else("dflt"), Opt.Some(5).or_else(0))
nested = Box(value=Box(value=1))
print(nested.get().get())
""", tmp_path)
    assert out == ('Box(value=4) Box(value="s") Box(value=[1, 2])\n7 a 9 b 1.5\n[10, 20] ["aa"]\ndflt 5\n1\n')


@needs_rustc
def test_run_across_modules_with_reexports(tmp_path):
    (tmp_path / "pairs.zy").write_text(
        "struct Pair[A, B]:\n    left: A\n    right: B\n\nfn swap[A, B](p: Pair[A, B]) -> Pair[B, A]:\n"
        "    return Pair(left=p.right, right=p.left)\n")
    (tmp_path / "util.zy").write_text(LIB.replace("fn first", "fn first") + "pub from pairs import Pair, swap\n")
    (tmp_path / "main.zy").write_text("""\
import util
import std.json
from util import Box, Opt, Pair, swap, first

struct Point:
    x: int

p = Pair(left=1, right=Point(x=2))
print(swap(p))
q: util.Box[str] = util.Box[str](value="m")
print(q.get(), util.first([Point(x=3)]), first([4]))
o = util.Opt.Some(2)
match o:
    util.Opt[int].Some(v):
        print("some", v)
    Opt.Empty:
        print("empty")
j = to_json(p)
print(json.stringify(j))
print(from_json(Pair[int, Point], j))
print(json.stringify(json_schema_defs(Pair[int, Point])))
""")
    exe = tmp_path / "prog"
    compile_rust(pipeline.transpile(tmp_path / "main.zy"), exe)
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
    assert out[0] == 'Pair(left=Point(x=2), right=1)'
    assert out[1] == "m Point(x=3) 4"
    assert out[2] == "some 2"
    assert out[3] == '{"left":1,"right":{"x":2}}'
    assert out[4] == "Ok(Pair(left=1, right=Point(x=2)))"
    assert '"Pair_int_Point"' in out[5]


# ---- editor ---------------------------------------------------------------------------------------------------

def test_editor_shows_type_parameters(tmp_path):
    f = tmp_path / "main.zy"
    f.write_text(LIB + "b = Box(value=1)\nprint(first([1]))\n")
    symbols = {s.name: s for s in editor.document_symbols(f.read_text())}
    assert symbols["first"].detail == "fn first[T](xs: list[T]) -> T"
    assert symbols["largest"].detail.startswith("fn largest[T: Ord](")
    proj = editor.open_project(f)
    line = f.read_text().splitlines().index("fn first[T](xs: list[T]) -> T:") + 1
    assert "fn first[T](xs: list[T]) -> T" in editor.hover(proj, f, line, 5).markdown
    assert "struct Box[T]" in editor.hover(proj, f, 1, 8).markdown


def test_editor_renames_through_type_arguments_and_patterns(tmp_path):
    f = tmp_path / "main.zy"
    src = LIB + "o: Opt[int] = Opt[int].Some(1)\nmatch o:\n    Opt[int].Some(x):\n        print(x)\n    Opt.Empty:\n        print(0)\nb = Box[int](value=1)\nprint(b.value)\n"
    f.write_text(src)
    proj = editor.open_project(f)
    opt_line = src.splitlines().index("enum Opt[T]:") + 1
    edits = editor.rename(proj, f, opt_line, 6, "Maybe")
    lines = sorted(e.place.line for e in edits)
    text = src.splitlines()
    assert all("Opt" in text[n - 1] for n in lines)
    assert sum(text[n - 1].count("Opt") for n in set(lines)) == len(lines)
    field_edits = editor.rename(proj, f, 2, 5, "item")  # the field `value` of Box
    assert len(field_edits) >= 4  # declaration, `self.value` twice, the named argument and `b.value`


def test_diagnostics_for_generics_in_the_editor():
    errors = diagnose_source("fn f[T](a: T) -> int:\n    return a\n", Path("x.zy"))
    assert [e.line for e in errors] == [2]
