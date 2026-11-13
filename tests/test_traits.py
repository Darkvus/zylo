"""Traits: user-defined bounds (`trait Shape:`, `struct Circle(Shape):`, `T: Shape`) and default type arguments."""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application import editor
from zylo.application.compile import diagnose_source, transpile_source
from zylo.application.completion import complete
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.infrastructure.rustc import compile_rust
from zylo.interface.lsp import SYMBOL_KINDS
from zylo.semantics.checker import check
from zylo.semantics.navigation import RenameError
from zylo.syntax.formatter import format_source
from zylo.syntax.lexer import KEYWORDS
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

SHAPE = """\
trait Shape:
    "Something with an area."
    fn area(self) -> float
    fn scale(self, by: float) -> Self

struct Circle(Shape):
    r: float

    fn area(self) -> float:
        return 3.0 * self.r * self.r

    fn scale(self, by: float) -> Circle:
        return Circle(r=self.r * by)

struct Sq:
    s: float

"""

TOTAL = """\
fn total[T: Shape](xs: list[T]) -> float:
    acc = 0.0
    for x in xs:
        acc += x.area()
    return acc

"""


def fails(src: str, match: str, line: int | None = None) -> None:
    with pytest.raises(ZyloError, match=match) as info:
        check(parse(src))
    if line is not None:
        assert info.value.line == line


def run(src: str, tmp_path: Path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout


# ---- syntax -----------------------------------------------------------------------------------------------

def test_trait_is_a_keyword_and_parses_into_signatures():
    assert "trait" in KEYWORDS
    trait = parse(SHAPE).body[0]
    assert isinstance(trait, ast.TraitDef) and trait.doc == "Something with an area."
    assert [m.name for m in trait.methods] == ["area", "scale"]
    assert all(m.has_self and m.body == [] for m in trait.methods)


def test_structs_and_enums_list_their_traits():
    prog = parse("struct A(X, m.Y):\n    a: int\nenum E[T](X):\n    V(T)\n")
    a, e = prog.body
    assert [t.name for t in a.traits] == ["X", "m.Y"] and [t.name for t in e.traits] == ["X"]
    assert [p.name for p in e.tparams] == ["T"]


def test_bounds_can_be_traits_possibly_qualified():
    f = parse("fn f[T: Shape + shapes.Named + Show](x: T):\n    pass\n").body[0]
    assert f.tparams[0].bounds == ["Shape", "shapes.Named", "Show"]


def test_type_parameters_can_have_defaults():
    s = parse("struct B[A, T: Num = int, U = list[str]]:\n    a: A\n").body[0]
    assert [p.default.name if p.default else None for p in s.tparams] == [None, "int", "list"]


@pytest.mark.parametrize("src,match", [
    ("trait T:\n    fn f(self) -> int:\n        return 1\n", "only a signature"),
    ("trait T:\n    x: int\n", "can only contain method signatures"),
    ("trait T[A]:\n    fn f(self) -> int\n", "takes no type parameters"),
    ("struct B[A = int, T]:\n    a: A\n", "has no default but follows"),
])
def test_syntax_errors(src, match):
    with pytest.raises(ZyloError, match=match):
        parse(src)


def test_formatter_keeps_traits_bounds_and_defaults():
    src = SHAPE + TOTAL + "struct Box[T: Show = int](Shape, other.Named):\n    v: T\n"
    once = format_source(src)
    assert "trait Shape:" in once and "    fn scale(self, by: float) -> Self\n" in once
    assert "struct Box[T: Show = int](Shape, other.Named):" in once
    assert "fn total[T: Shape](xs: list[T]) -> float:" in once
    assert format_source(once) == once


# ---- declaring and implementing -------------------------------------------------------------------------------

def test_a_correct_program_checks():
    check(parse(SHAPE + TOTAL + "print(total([Circle(r=1.0)]))\n"))


def test_methods_may_be_called_through_the_bound_with_named_arguments_and_self_types():
    prog = parse(SHAPE + "fn grow[T: Shape](x: T) -> T:\n    return x.scale(by=2.0)\nc = grow(Circle(r=1.0))\n")
    check(prog)
    assert str(next(s for s in prog.body if isinstance(s, ast.Assign)).value.ty) == "Circle"


def test_missing_method_is_reported_at_the_trait_with_the_signature_to_add():
    src = SHAPE.replace("    fn scale(self, by: float) -> Circle:\n        return Circle(r=self.r * by)\n", "")
    fails(src, r"'Circle' implements trait 'Shape' but has no method 'scale'; add `fn scale\(self, by: float\) -> Circle:`", 6)


@pytest.mark.parametrize("old,new,found", [
    ("-> Circle:", "-> float:", "-> float"),
    ("by: float) -> Circle", "by: int) -> Circle", "by: int"),
    ("by: float) -> Circle", "k: float) -> Circle", "k: float"),
])
def test_a_wrong_signature_says_what_was_expected(old, new, found):
    src = SHAPE.replace(old, new)
    errors = diagnose_source(src, Path("x.zy"))
    msg = next(e for e in errors if "does not match trait" in str(e))
    assert msg.line == 12
    assert "expected `fn scale(self, by: float) -> Circle`" in str(msg) and found in str(msg)


def test_a_generic_method_cannot_implement_a_trait_method():
    src = SHAPE.replace("fn area(self) -> float:\n        return 3.0 * self.r * self.r",
                        "fn area[U](self, u: U) -> float:\n        return 1.0")
    fails(src, "is generic, so it cannot implement trait 'Shape'")


def test_unknown_trait_and_a_struct_used_as_a_trait():
    fails(SHAPE.replace("Circle(Shape)", "Circle(Shap)"), r"unknown trait 'Shap' \(declared: Shape\)", 6)
    fails(SHAPE.replace("Circle(Shape)", "Circle(Sq)"), "'Sq' is a struct, not a trait", 6)


def test_listing_a_trait_twice_is_a_duplicate_implementation():
    fails(SHAPE.replace("Circle(Shape)", "Circle(Shape, Shape)"), "'Circle' implements trait 'Shape' twice", 6)


@pytest.mark.parametrize("body,match", [
    ('"doc"\n', "needs at least one method"),
    ("fn a() -> int\n", "must take `self`"),
    ("fn a(self, x: int = 1) -> int\n", "cannot have a default value"),
    ("fn a[T](self, x: T) -> int\n", "cannot be generic"),
    ("fn a(self) -> int\n    fn a(self) -> int\n", "already declared in trait"),
])
def test_trait_declaration_errors(body, match):
    src = "trait E:\n    " + body
    fails(src, match)


def test_trait_names_do_not_clash():
    fails("trait E:\n    fn a(self) -> int\nstruct E:\n    x: int\n", "already defined|already a struct")
    fails("trait Eq:\n    fn a(self) -> int\n", "reserved name")


def test_a_trait_is_not_a_type():
    fails(SHAPE + "fn f(x: Shape) -> float:\n    return 1.0\n", r"'Shape' is a trait, not a type: use it as a bound", 18)


def test_traits_work_on_enums_and_generic_structs():
    src = """\
trait Named:
    fn name(self) -> str

enum Level(Named):
    Easy

    fn name(self) -> str:
        return "easy"

struct Team[T](Named):
    members: list[T]

    fn name(self) -> str:
        return "team"

fn show[T: Named](x: T) -> str:
    return x.name()

a = show(Level.Easy)
b = show(Team(members=[1]))
"""
    check(parse(src))
    fails(src.replace("    fn name(self) -> str:\n        return \"team\"", "    fn name(self) -> int:\n        return 3"),
          "does not match trait 'Named'")


# ---- bounds ------------------------------------------------------------------------------------------------------

def test_a_type_that_lacks_the_trait_is_rejected_where_the_generic_is_used():
    fails(SHAPE + TOTAL + "total([Sq(s=1.0)])\n", r"Sq does not implement trait Shape, which bounds T "
          r"\(list the trait in its definition: `struct Sq\(Shape\):`\)", 24)
    fails(SHAPE + TOTAL + "total([3])\n", "int does not implement trait Shape.*only a struct or enum")


def test_unknown_bounds_and_structs_as_bounds():
    fails(SHAPE + "fn f[T: Nope](x: T):\n    pass\n", r"unknown bound 'Nope' on type parameter 'T' \(available: Eq, Ord, Num, Show, Key, Shape\)")
    fails(SHAPE + "fn f[T: Sq](x: T):\n    pass\n", "'Sq' is a struct, not a trait")
    fails(SHAPE + "struct W[T: Zzz]:\n    v: T\n", "unknown bound 'Zzz'")


def test_a_method_the_bounds_do_not_declare_is_an_error():
    fails(SHAPE + "fn f[T: Shape](x: T) -> float:\n    return x.volume()\n", r"type parameter T has no method 'volume' \(its traits: Shape\)")
    fails(SHAPE + "fn f[T](x: T) -> float:\n    return x.area()\n", r"declare it with a bound: T: Shape")
    fails(SHAPE + "fn f[T: Shape](x: T) -> float:\n    return x.scale(1.0, 2.0).area()\n", r"T\.scale takes 1 argument")


def test_combined_bounds_give_all_capabilities():
    check(parse(SHAPE + "fn f[T: Shape + Show](x: T) -> str:\n    return f\"{x} {x.area()}\"\n"))
    fails(SHAPE + "fn f[T: Shape](x: T) -> str:\n    return f\"{x}\"\n", "Show")


def test_a_bounded_parameter_can_be_passed_on_to_another_generic():
    check(parse(SHAPE + TOTAL + "fn twice[T: Shape](x: T) -> float:\n    return total([x, x])\n"))
    fails(SHAPE + TOTAL + "fn twice[T](x: T) -> float:\n    return total([x, x])\n", "does not implement trait Shape")


def test_conflicting_method_declarations_in_two_bounds_are_ambiguous():
    src = SHAPE + "trait Other:\n    fn area(self) -> int\n\nfn f[T: Shape + Other](x: T):\n    x.area()\n"
    fails(src, "declared differently by")


def test_methods_of_generic_structs_may_have_trait_bounds():
    src = SHAPE + "struct Bag[T]:\n    items: list[T]\n\n    fn sum_area[U: Shape](self, u: U) -> float:\n        return u.area()\n"
    check(parse(src + "b = Bag(items=[1])\nz = b.sum_area(Circle(r=1.0))\n"))


# ---- default type arguments -----------------------------------------------------------------------------

DEFAULTS = """\
struct Counter[T = int]:
    items: list[T] = []

    fn size(self) -> int:
        return len(self.items)

fn zero[T: Num = int]() -> T:
    return 0

"""


def test_defaults_fill_missing_type_arguments_but_inference_wins():
    prog = parse(DEFAULTS + "a: Counter = Counter()\nb = Counter(items=['x'])\nc = Counter[float]()\nd = zero()\ne = zero[float]()\n")
    check(prog)
    types = [str(s.value.ty) for s in prog.body if isinstance(s, ast.Assign)]
    assert types == ["Counter[int]", "Counter[str]", "Counter[float]", "int", "float"]
    assert str(next(s for s in prog.body if isinstance(s, ast.Assign)).annotation.name) == "Counter"


def test_default_errors():
    fails("struct B[T, U = list[T]]:\n    v: T\n", "cannot mention a type parameter", 1)
    fails("struct B[T: Num = str]:\n    v: T\n", "does not satisfy its bound Num", 1)
    fails("struct B[A, T = int]:\n    v: A\nx: B[int, int, int] = B(v=1)\n", r"takes 1 to 2 type argument\(s\)")
    fails("struct B[A, T = int]:\n    v: A\nx: B = B(v=1)\n", r"takes 1 to 2 type argument\(s\)")
    fails("struct B[T]:\n    v: T\n\n    fn m[U = int](self, u: U) -> U:\n        return u\n", "not for the methods")


# ---- modules and re-exports ---------------------------------------------------------------------------------

LIB = SHAPE.replace('struct Sq:\n    s: float\n', "") + TOTAL


def write_project(tmp_path: Path) -> Path:
    (tmp_path / "shapes.zy").write_text(LIB)
    (tmp_path / "api.zy").write_text("pub from shapes import Shape, Circle, total\n")
    (tmp_path / "main.zy").write_text("""\
import api
import shapes as sh
from api import Shape, Circle

struct Sq(Shape):
    s: float

    fn area(self) -> float:
        return self.s * self.s

    fn scale(self, by: float) -> Sq:
        return Sq(s=self.s * by)

fn grow[T: api.Shape + Show](x: T, by: float) -> T:
    return x.scale(by)

print(api.total([Circle(r=1.0), Circle(r=2.0)]))
print(sh.total([Sq(s=3.0)]))
print(grow(Sq(s=1.0), 3.0), grow(Circle(r=1.0), 2.0))
print(to_json(Sq(s=1.5)))
""")
    return tmp_path / "main.zy"


@needs_rustc
def test_traits_across_modules_and_reexports(tmp_path):
    main = write_project(tmp_path)
    exe = tmp_path / "prog"
    compile_rust(pipeline.transpile(main), exe)
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
    assert out[:3] == ["15.0", "9.0", "Sq(s=3.0) Circle(r=2.0)"]
    assert out[3].startswith("Obj(") and '"s"' in out[3]  # typed JSON does not care about traits


def test_a_trait_missing_from_a_module_is_a_clear_error(tmp_path):
    (tmp_path / "shapes.zy").write_text(LIB)
    main = tmp_path / "main.zy"
    main.write_text("from shapes import Shape\nimport shapes\nfn f[T: shapes.Nope](x: T):\n    pass\n")
    with pytest.raises(ZyloError, match="module 'shapes' has no trait 'Nope'"):
        pipeline.transpile(main)
    main.write_text("import shapes\nfn f[T: nowhere.Shape](x: T):\n    pass\n")
    with pytest.raises(ZyloError, match="unknown module 'nowhere' in bound"):
        pipeline.transpile(main)


def test_a_library_module_may_hold_traits_and_a_trait_cannot_be_called_like_a_builtin_bound(tmp_path):
    (tmp_path / "bad.zy").write_text("trait Show:\n    fn a(self) -> int\n")
    main = tmp_path / "main.zy"
    main.write_text("import bad\n")
    with pytest.raises(ZyloError, match="reserved name"):
        pipeline.transpile(main)


# ---- running -----------------------------------------------------------------------------------------------

@needs_rustc
def test_run_a_program_with_traits_and_defaults(tmp_path):
    src = SHAPE + TOTAL + DEFAULTS + """\
fn grow[T: Shape](x: T) -> T:
    return x.scale(2.0)

print(total([Circle(r=1.0), grow(Circle(r=1.0))]))
c: Counter = Counter()
print(c.size(), Counter[str](items=["a"]).size(), zero(), zero[float]())
"""
    assert run(src, tmp_path) == "15.0\n0 1 0 0.0\n"


# ---- the editor ------------------------------------------------------------------------------------------------

def project(tmp_path: Path, files: dict[str, str]):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return editor.open_project(tmp_path / "main.zy")


def where(text: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """1-based line and column of the nth occurrence of a text."""
    found = -1
    for i, line in enumerate(text.splitlines(), start=1):
        col = line.find(needle)
        while col >= 0:
            found += 1
            if found == nth:
                return i, col + 1
            col = line.find(needle, col + 1)
    raise AssertionError(needle)


MAIN = """\
from shapes import Shape, Circle

struct Sq(Shape):
    s: float

    fn area(self) -> float:
        return self.s * self.s

    fn scale(self, by: float) -> Sq:
        return Sq(s=self.s * by)

fn double[T: Shape](x: T) -> float:
    return x.scale(2.0).area()

print(double(Sq(s=1.0)), Circle(r=1.0).area())
"""


def test_outline_lists_traits_with_their_methods():
    symbols = {s.name: s for s in editor.document_symbols(SHAPE)}
    assert symbols["Shape"].kind == "trait" and [c.name for c in symbols["Shape"].children] == ["area", "scale"]
    assert symbols["Shape"].children[0].detail == "fn area(self) -> float"
    assert "trait" in SYMBOL_KINDS


def test_hover_and_definition_on_traits_and_through_bounds(tmp_path):
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": MAIN})
    main = tmp_path / "main.zy"
    line, col = where(MAIN, "Shape", 1)  # in `struct Sq(Shape)`
    card = editor.hover(proj, main, line, col).markdown
    assert "trait Shape" in card and "fn area(self) -> float" in card and "Something with an area." in card
    assert editor.definition(proj, main, line, col)[0].path == (tmp_path / "shapes.zy").resolve()
    line, col = where(MAIN, "area", 1)  # `x.scale(2.0).area()` in the generic: a method of a type parameter
    assert "fn Shape.area(self) -> float" in editor.hover(proj, main, line, col).markdown
    target = editor.definition(proj, main, line, col)[0]
    assert target.path == (tmp_path / "shapes.zy").resolve() and target.line == 3
    line, col = where(MAIN, "Shape", 2)  # the bound `T: Shape`
    assert "trait Shape" in editor.hover(proj, main, line, col).markdown
    line, col = where(MAIN, "Sq", 0)
    assert "struct Sq(Shape)" in editor.hover(proj, main, line, col).markdown


def test_renaming_a_trait_method_renames_the_family(tmp_path):
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": MAIN})
    line, col = where(LIB, "area(self)")  # in the trait
    edits = editor.rename(proj, tmp_path / "shapes.zy", line, col, "surface")
    places = {(e.place.path.name, e.place.line) for e in edits}
    assert {("shapes.zy", 3), ("shapes.zy", 9), ("shapes.zy", 19)} <= places  # trait, Circle, total's `x.area()`
    assert {("main.zy", 6), ("main.zy", 13), ("main.zy", 15)} <= places  # Sq, the generic's call, a concrete call
    assert all(e.new_text == "surface" for e in edits)
    # starting from an implementation gives the same family
    line, col = where(MAIN, "area", 0)
    assert {(e.place.path.name, e.place.line) for e in editor.rename(proj, tmp_path / "main.zy", line, col, "surface")} == places


def test_renaming_a_trait_renames_its_uses(tmp_path):
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": MAIN})
    line, col = where(LIB, "Shape")
    edits = editor.rename(proj, tmp_path / "shapes.zy", line, col, "Figure")
    places = {(e.place.path.name, e.place.line) for e in edits}
    assert {("shapes.zy", 1), ("shapes.zy", 6), ("shapes.zy", 16), ("main.zy", 1), ("main.zy", 3), ("main.zy", 12)} <= places
    with pytest.raises(RenameError, match="built-in"):
        editor.rename(proj, tmp_path / "shapes.zy", line, col, "Show")


def test_renaming_a_method_to_a_name_a_struct_already_has_is_refused(tmp_path):
    main = MAIN.replace("    fn scale(self, by: float) -> Sq:", "    fn surface(self) -> float:\n        return 1.0\n\n    fn scale(self, by: float) -> Sq:")
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": main})
    line, col = where(LIB, "area(self)")
    with pytest.raises(RenameError, match="already has a member named 'surface'"):
        editor.rename(proj, tmp_path / "shapes.zy", line, col, "surface")


def test_renaming_a_parameter_of_a_trait_method_is_refused(tmp_path):
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": MAIN})
    line, col = where(LIB, "by: float")
    with pytest.raises(RenameError, match="part of a trait's method signature"):
        editor.rename(proj, tmp_path / "shapes.zy", line, col, "factor")


def labels(items) -> set[str]:
    return {c.label for c in items}


def test_completion_after_a_dot_on_a_bounded_value_offers_the_trait_methods(tmp_path):
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": MAIN})
    src = MAIN.replace("    return x.scale(2.0).area()", "    return x.")
    (tmp_path / "main.zy").write_text(src)
    proj = editor.open_project(tmp_path / "main.zy")
    line, col = where(src, "x.", 0)
    items = complete(proj, tmp_path / "main.zy", line + 0, col + 2 + len("    return ") - 1)
    assert labels(items) == {"area", "scale"}
    assert next(c for c in items if c.label == "scale").detail == "(by: float) -> Self"


def test_completion_of_traits_in_bounds_and_base_lists(tmp_path):
    src = "from shapes import Shape, Circle\n\nstruct Sq(\n    s: float\n\nfn f[T: Shape + \n"
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": src})
    main = tmp_path / "main.zy"
    items = complete(proj, main, 3, len("struct Sq(") + 1)
    assert labels(items) == {"Shape"} and items[0].kind == "trait"
    items = complete(proj, main, 6, len("fn f[T: Shape + ") + 1)
    assert {"Shape", "Show", "Eq", "Ord", "Num", "Key"} <= labels(items) and "Circle" not in labels(items)
    items = complete(proj, main, 6, len("fn f[T: ") + 1)
    assert "Circle" not in labels(items)


def test_a_trait_is_not_offered_where_a_type_is_expected(tmp_path):
    src = "from shapes import Shape, Circle\n\nfn f(x: \n"
    proj = project(tmp_path, {"shapes.zy": LIB, "main.zy": src})
    items = complete(proj, tmp_path / "main.zy", 3, len("fn f(x: ") + 1)
    assert "Circle" in labels(items) and "Shape" not in labels(items)
