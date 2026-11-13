"""Struct patterns, `name @ pattern`, a bare name as the catch-all arm, nested or-pattern redundancy and the editor around them."""
import pytest
from test_formatter_wrap import check as roundtrip
from test_lsp_navigation import Workspace

from zylo.application.actions import code_actions
from zylo.application.completion import complete
from zylo.application.editor import open_project
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.semantics.checker import check
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

HEAD = ("struct Point:\n    x: int\n    y: int\n\nstruct Line:\n    a: Point\n    b: Point\n    name: str\n\n"
        "struct Box[T]:\n    value: T\n    label: str\n\n"
        "enum Shape:\n    Circle(int)\n    Square(Point)\n    Empty\n\n"
        "p = Point(1, 2)\nl = Line(p, p, \"n\")\nb = Box(1, \"b\")\ns = Shape.Empty\nx = 1\nw = \"w\"\no: int? = none\n")


N = HEAD.count("\n")  # the arm of a `match` written first in a test is on line N + 2


def last_match(prog):
    return next(n for n in reversed(prog.body) if isinstance(n, ast.Match))


def ok(body: str) -> None:
    check(parse(HEAD + body))


def fails(body: str, message: str, line: int | None = None) -> None:
    with pytest.raises(ZyloError, match=message) as e:
        check(parse(HEAD + body))
    if line is not None:
        assert e.value.line == line


def arms(value: str, *patterns: str) -> str:
    return f"match {value}:\n" + "".join(f"    {p}:\n        print(1)\n" for p in patterns)


# ---- parser ----------------------------------------------------------------------------------------------------

def pattern(text: str):
    return parse(f"match p:\n    {text}:\n        print(1)\n").body[0].arms[0].pattern


def test_struct_pattern_with_fields_and_shorthand():
    p = pattern("Point(x=0, y)")
    assert (p.kind, p.name, p.fields) == ("struct", "Point", ["x", "y"])
    assert [s.kind for s in p.subs] == ["lit", "bind"]


def test_struct_pattern_forms():
    assert pattern("Point()").fields == []
    assert pattern("Box[int](value=1)").type.args[0].name == "int"
    assert pattern("geo.Point(x=1)").type.name == "geo.Point"
    assert pattern("Line(a=Point(x), name=n @ (\"a\" | \"b\"))").subs[1].kind == "at"


def test_at_pattern():
    p = pattern("n @ 1..=9")
    assert (p.kind, p.name, p.subs[0].kind) == ("at", "n", "range")
    assert pattern("n @ (1 | 2)").subs[0].kind == "or"
    assert pattern("a @ 1 | 2").kind == "or"  # `@` binds tighter than `|`


def test_a_bare_name_is_a_binding_at_the_top_of_an_arm():
    assert pattern("other").kind == "bind"


@pytest.mark.parametrize("text,message", [
    ("Point(1)", "lists its fields as `field=pattern`"),
    ("Point(x=1, x=2)", "appears twice"),
    ("Point(x=1, y.z)", "lists its fields"),
])
def test_malformed_struct_patterns(text, message):
    with pytest.raises(ZyloError, match=message):
        pattern(text)


# ---- checker ---------------------------------------------------------------------------------------------------

def test_struct_patterns_bind_with_the_field_types():
    prog = check(parse(HEAD + "match l:\n    Line(a=Point(x), name=\"q\" | \"r\"):\n        print(x)\n    Line(b=Point(y=k), name=n):\n        print(k, n)\n"))
    first, second = last_match(prog).arms
    assert first.binds == ["x"] and second.binds == ["k", "n"]
    assert [str(v.ty) for v in second.vars] == ["int", "str"]
    assert first.pattern.enum == "Line" and first.pattern.slots[2].kind == "or" and first.pattern.slots[1] is None


def test_partial_struct_patterns_need_a_catch_all():
    fails(arms("p", "Point(x=0)"), "does not cover Point\\(x=\\.\\.=-1\\), Point\\(x=1\\.\\.\\)", N + 1)
    ok(arms("p", "Point(x=0)", "Point()"))
    ok(arms("p", "Point(x=0)", "Point(x)"))
    ok(arms("p", "Point(x=0)", "other"))


def test_all_field_combinations_cover_a_struct_of_enums():
    src = "struct S:\n    a: bool\n    b: bool\n\nv = S(true, false)\n"
    check(parse(src + arms("v", "S(a=true, b=true)", "S(a=true, b=false)", "S(a=false)")))
    with pytest.raises(ZyloError, match="S\\(a=false, b=true\\)"):
        check(parse(src + arms("v", "S(a=true)", "S(a=false, b=false)")))


def test_struct_pattern_errors():
    fails(arms("p", "Point(z=1)", "other"), "struct Point has no field 'z' \\(has: x, y\\)", N + 2)
    fails(arms("p", "Point(x=\"a\")", "other"), "the value here is int, but the pattern is str")
    fails(arms("p", "Line(name=\"a\")", "other"), "this `match` is on Point, not Line")
    fails(arms("x", "Point(x=1)", "_"), "`Point\\(...\\)` is a struct pattern, but the value is int")
    fails(arms("p", "Shape.Circle(r)", "_"), "this `match` is on Point, not Shape|is a variant pattern, but the value is Point")
    fails(arms("p", "Point(x=a, y=a)", "_"), "cannot bind the same name twice")


def test_struct_patterns_on_generic_structs():
    ok(arms("b", "Box(value=0)", "Box[int](label=\"z\")", "Box(value, label)"))
    fails(arms("b", "Line(name=n)", "_"), "this `match` is on Box\\[int\\], not Line")


def test_the_enum_must_still_be_named_for_a_variant():
    fails(arms("s", "Circle(r)", "_"), "write the enum before the variant: `Shape.Circle`", N + 2)
    fails(arms("s", "Circle", "_"), "`Circle` is a variant of Shape")


def test_at_binds_the_whole_value():
    prog = check(parse(HEAD + "match s:\n    c @ Shape.Circle(_):\n        print(c)\n    Shape.Square(Point(x=n @ 1..=3)):\n"
                       "        print(n)\n    _:\n        print(0)\n"))
    arm = last_match(prog).arms[0]
    assert arm.binds == ["c"] and str(arm.vars[0].ty) == "Shape"
    assert str(last_match(prog).arms[1].vars[0].ty) == "int"


def test_at_cannot_bind_inside():
    fails(arms("s", "c @ Shape.Circle(r)", "_"), "after `c @` cannot bind names \\(r\\)", N + 2)
    fails(arms("x", "a @ b", "_"), "cannot bind names")
    fails(arms("x", "n @ 1..=5 | 7", "_"), "must bind the same names")


def test_at_with_a_range_is_exact_for_exhaustiveness():
    ok(arms("x", "n @ ..=0", "n @ 1.."))
    fails(arms("x", "n @ 1..=5", "m @ 3..=4"), "can never run", N + 4)


def test_a_bare_name_catches_everything_that_is_left():
    ok("match x:\n    1:\n        print(1)\n    other:\n        print(other)\n")
    ok("match s:\n    Shape.Circle(_):\n        print(1)\n    rest:\n        print(rest)\n")
    ok("match w:\n    \"a\":\n        print(1)\n    other:\n        print(other)\n")
    fails("match x:\n    other:\n        print(other)\n    1:\n        print(1)\n", "can never run", N + 4)
    ok("match x:\n    n if n > 3:\n        print(n)\n    other:\n        print(other)\n")
    fails("match x:\n    n if n > 3:\n        print(n)\n", "needs a final `_` arm")


def test_a_variant_name_is_never_a_catch_all():
    fails("match s:\n    Empty:\n        print(1)\n    _:\n        print(2)\n", "`Empty` is a variant of Shape: write `Shape.Empty`", N + 2)


def test_the_catch_all_name_cannot_hide_in_an_or_pattern():
    fails("match x:\n    1 | other:\n        print(other)\n", "must bind the same names")
    fails("match x:\n    other | 1:\n        print(other)\n", "must bind the same names")


def test_a_nested_or_pattern_with_a_repeated_alternative_is_an_error():
    fails("match o:\n    some(1 | 1):\n        print(1)\n    _:\n        print(2)\n", "`1` is matched twice: an earlier alternative is the same", N + 2)
    fails("match o:\n    some(1..=5 | 2..=3):\n        print(1)\n    _:\n        print(2)\n", "this alternative can never run", N + 2)
    fails(arms("p", "Point(x=1 | 2 | 1)", "_"), "matched twice", N + 2)
    fails("t = (1, 2)\nmatch t:\n    (1 | 2 | 1, _):\n        print(1)\n    _:\n        print(2)\n", "matched twice")
    ok("match o:\n    some(1 | 2..=3):\n        print(1)\n    _:\n        print(2)\n")


def test_the_arms_own_or_pattern_is_still_checked_against_earlier_arms():
    fails("match x:\n    1 | 2:\n        print(1)\n    2 | 3:\n        print(2)\n    _:\n        print(3)\n", "`2` is matched twice", N + 4)


# ---- the loop variable ---------------------------------------------------------------------------------------------

LOOP = "struct P:\n    x: int\n\nps = [P(1)]\n"


def test_a_lost_loop_variable_mutation_is_an_error():
    with pytest.raises(ZyloError, match="loop variable 'q' is modified but the changes are lost") as e:
        check(parse(LOOP + "for q in ps:\n    q.x = 9\n"))
    assert e.value.line == 5


def test_lost_loop_mutation_through_methods_and_lists():
    src = "struct B:\n    xs: list[int]\n\n    fn add(self, n: int):\n        append(self.xs, n)\n\nbs = [B([1])]\n"
    with pytest.raises(ZyloError, match="loop variable 'b'"):
        check(parse(src + "for b in bs:\n    b.add(1)\n"))
    with pytest.raises(ZyloError, match="loop variable 'xs'"):
        check(parse("xss = [[1]]\nfor xs in xss:\n    append(xs, 2)\n"))


def test_a_loop_variable_that_is_also_read_stays_legal():
    check(parse(LOOP + "out = [P(0)]\nfor q in ps:\n    q.x = 9\n    append(out, q)\n"))
    check(parse(LOOP + "for q in ps:\n    q.x = 9\n    print(q.x)\n"))
    check(parse(LOOP + "for q in ps:\n    print(q.x)\n    q.x = 9\n"))
    check(parse("for i in range(3):\n    i = i + 1\n"))
    check(parse(LOOP + "for q in ps:\n    q = P(2)\n"))


def test_the_index_loop_the_message_suggests_compiles():
    check(parse(LOOP + "for i in range(len(ps)):\n    ps[i].x = 9\n"))


# ---- formatter -------------------------------------------------------------------------------------------------

SAMPLE = HEAD + '''
fn f(l: Line, s: Shape, o: int?) -> int:
    match l:
        Line(a=Point(x=0, y), name="a"):
            return y
        Line(name=n @ ("a" | "b")):
            return 1
        Line(b=Point(), name):
            return 2
    match s:
        c @ Shape.Circle(_):
            return 3
        Shape.Square(Point(x=1..=9, y=q)) if q > 1:
            return q
        other:
            return 5
    match o:
        some(n @ 1..=5):
            return n
        _:
            return 0
'''


@pytest.mark.parametrize("width", [30, 50, 80, 100])
def test_struct_and_at_patterns_round_trip_at_any_width(width):
    roundtrip(SAMPLE, width)


def test_equal_names_are_written_as_the_shorthand():
    out = format_source("match p:\n    Point(x=x, y=0):\n        print(x)\n    _:\n        print(0)\n")
    assert "Point(x, y=0):" in out


def test_a_long_struct_pattern_breaks_one_field_per_line():
    long = ("match l:\n    Line(a=Point(x=0, y=100000), b=Point(x=200000, y=0), name=\"a long enough name to wrap\"):\n"
            "        print(1)\n    _:\n        print(2)\n")
    out = roundtrip(long, 60)
    assert "Line(\n        a=Point(x=0, y=100000),\n" in out
    assert out.count("\n") > long.count("\n")


def test_a_long_at_pattern_keeps_the_name_in_front():
    long = "match o:\n    big @ (1000000000 | 2000000000 | 3000000000 | 4000000000 | 5000000000 | 6000000000):\n        print(big)\n    _:\n        print(0)\n"
    out = roundtrip(long, 50)
    assert "big @ (\n" in out


# ---- exhaustiveness against brute force --------------------------------------------------------------------------

def test_struct_exhaustiveness_agrees_with_brute_force():
    import itertools
    import random

    from zylo.domain.types import BOOL, INT, StructT
    from zylo.semantics.exhaustive import INT_MAX, INT_MIN, Con, Space

    ty = StructT("S")
    space = Space(lambda t: {}, lambda t: {"a": BOOL, "b": INT})
    rng = random.Random(7)
    for _ in range(300):
        rows = []
        for _ in range(rng.randint(0, 4)):
            a = rng.choice([None, Con(("b", True)), Con(("b", False))])
            lo = rng.choice([INT_MIN, -2, 0, 3])
            hi = rng.choice([INT_MAX, -1, 2, 5])
            b = rng.choice([None, Con(("i", lo, hi))]) if lo <= hi else None
            rows.append([Con(("st",), (a, b))])
        covered = all(any(_matches(r[0], av, bv) for r in rows)
                      for av, bv in itertools.product([True, False], [INT_MIN, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, INT_MAX]))
        assert (space.missing(rows, [ty]) is None) == covered, rows


def _matches(con, av, bv) -> bool:
    a, b = con.subs
    return (a is None or a.ctor[1] == av) and (b is None or b.ctor[1] <= bv <= b.ctor[2])


# ---- editor ----------------------------------------------------------------------------------------------------

EDITOR = '''struct Point:
    x: int
    y: int

fn describe(p: Point, n: int) -> str:
    match p:
        Point(x=0, y):
            return f"{y}"
        Point(x, y=0) if x > 1:
            return f"{x}"
        whole @ Point(x=1):
            return f"{whole.y}"
        other:
            return f"{other.x}"
'''


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path, {"doc.zy": EDITOR})


def test_hover_on_struct_pattern_names(ws):
    assert "(variable) y: int" in ws.hover("doc.zy", "y}")
    assert "(variable) whole: Point" in ws.hover("doc.zy", "whole.y")
    assert "(variable) other: Point" in ws.hover("doc.zy", "other.x")
    assert "(field) Point.x: int" in ws.hover("doc.zy", "x=0")


def test_definition_of_a_field_label_and_a_struct_pattern_binding(ws):
    assert ws.definition("doc.zy", "x=0") == [("doc.zy", 1, 4)]
    assert ws.definition("doc.zy", "y}") == [("doc.zy", 6, EDITOR.splitlines()[6].index("y)"))]
    assert ws.definition("doc.zy", "whole.y") == [("doc.zy", 10, EDITOR.splitlines()[10].index("whole"))]
    assert ws.definition("doc.zy", "Point(x=0") == [("doc.zy", 0, 7)]


def test_rename_a_variable_bound_by_a_shorthand_field_keeps_the_field(ws):
    out = ws.renamed("doc.zy", "y}", "height")["doc.zy"]
    assert "Point(x=0, y=height):\n            return f\"{height}\"" in out
    out = ws.renamed("doc.zy", "x > 1", "left")["doc.zy"]
    assert "Point(x=left, y=0) if left > 1:\n            return f\"{left}\"" in out


def test_rename_a_field_rewrites_labels_and_shorthands(ws):
    out = ws.renamed("doc.zy", "x: int", "col")["doc.zy"]
    assert "Point(col=0, y):" in out and "Point(col=x, y=0) if x > 1" in out and "whole @ Point(col=1)" in out
    assert "other.col" in out


def test_rename_the_at_binding_and_the_catch_all(ws):
    assert "whole @ Point" not in ws.renamed("doc.zy", "whole.y", "all")["doc.zy"]
    out = ws.renamed("doc.zy", "other.x", "rest")["doc.zy"]
    assert "        rest:\n            return f\"{rest.x}\"" in out


def test_rename_the_struct_in_a_pattern(ws):
    out = ws.renamed("doc.zy", "Point(x=0", "Pt")["doc.zy"]
    assert "Pt(x=0, y):" in out and "whole @ Pt(x=1):" in out and "p: Pt" in out


# ---- completion -------------------------------------------------------------------------------------------------

COMPLETE = '''struct Point:
    x: int
    y: int

enum Shape:
    Circle(int)
    Empty

fn f(p: Point, s: Shape, o: int?, r: Result[int, str], b: bool) -> int:
    match s:
        Shape.Empty:
            return 0
        Shape.Circle(r):
            return r
    return 0
'''


def labels(tmp_path, header: str, typed: str, above: tuple[str, ...] = ()) -> dict:
    lines = COMPLETE.split("\n")
    at = next(i for i, ln in enumerate(lines) if ln.strip() == "Shape.Empty:")
    lines[at - 1:at + 1] = [header, *above, typed]
    at += len(above)
    path = tmp_path / "doc.zy"
    path.write_text("\n".join(lines))
    return {c.label: c for c in complete(open_project(path), path, at + 1, len(typed) + 1)}


def test_an_arm_offers_the_variants_of_the_matched_enum(tmp_path):
    got = labels(tmp_path, "    match s:", "        ")
    assert {"Shape.Circle", "Shape.Empty", "_"} <= got.keys()
    assert got["Shape.Circle"].kind == "variant" and "int" in got["Shape.Circle"].detail
    assert "Shape.Circle" in labels(tmp_path, "    match s:", "        Shape.Empty | ")
    assert "Shape.Circle" in labels(tmp_path, "    match s:", "        c @ ")


def test_an_arm_offers_what_fits_the_type(tmp_path):
    assert {"Point", "_"} <= labels(tmp_path, "    match p:", "        ").keys()
    assert {"some", "none"} <= labels(tmp_path, "    match o:", "        ").keys()
    assert {"ok", "err"} <= labels(tmp_path, "    match r:", "        ").keys()
    assert {"true", "false"} <= labels(tmp_path, "    match b:", "        ").keys()


def test_the_body_of_an_arm_does_not_offer_patterns(tmp_path):
    assert "Shape.Circle" not in labels(tmp_path, "    match s:", "            ", ("        Shape.Empty:",))
    assert "Shape.Circle" in labels(tmp_path, "    match s:", "        ", ("        Shape.Empty:", "            return 0"))


def test_variants_complete_after_the_dot_inside_an_arm(tmp_path):
    got = labels(tmp_path, "    match s:", "        Shape.")
    assert set(got) == {"Circle", "Empty"}


def test_struct_pattern_fields_complete_as_labels_and_as_names(tmp_path):
    got = labels(tmp_path, "    match p:", "        Point(")
    assert {"x=", "y="} <= got.keys()
    assert {"x", "y"} <= got.keys() and got["x"].kind == "field"
    got = labels(tmp_path, "    match p:", "        Point(x=0, ")
    assert "y=" in got and "x=" not in got


def test_a_struct_field_is_not_a_name_in_an_ordinary_call(tmp_path):
    lines = COMPLETE.split("\n")
    lines.insert(14, "    q = Point(")
    path = tmp_path / "doc.zy"
    path.write_text("\n".join(lines))
    got = {c.label for c in complete(open_project(path), path, 15, len("    q = Point(") + 1)}
    assert "x=" in got and "y" not in got - {"y="} - {"p", "s", "o", "r", "b"}


def test_the_missing_arms_fix_counts_the_catch_all_and_at_patterns(tmp_path):
    text = ("enum E:\n    A(int)\n    B\n\nfn g(e: E) -> int:\n    match e:\n        v @ E.A(_):\n            return 1\n"
            "        E.B:\n            return 2\n")
    path = tmp_path / "doc.zy"
    path.write_text(text)
    assert code_actions(open_project(path), path, 6, 1) == []
    path.write_text(text.replace("        E.B:\n            return 2\n", "        rest:\n            return 2\n"))
    assert code_actions(open_project(path), path, 6, 1) == []
