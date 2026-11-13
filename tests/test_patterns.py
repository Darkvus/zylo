"""Nested patterns and integer ranges in `match` (v0.25): parsing, typing, exhaustiveness, codegen, formatting, editor."""
import pytest
from test_formatter_wrap import check as roundtrip
from test_lsp_navigation import Workspace

from zylo.application.compile import transpile_source
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.semantics.checker import check
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

HEAD = ("enum Shape:\n    Circle(float?)\n    Rect(int, int)\n    Named(str, int?)\n    Empty\n\n"
        "s = Shape.Empty\nx = 1\no: int? = none\nr: Result[int, str] = ok(1)\nt = (1, true)\n")


def arms(body: str) -> list[ast.MatchArm]:
    return parse(HEAD + body).body[-1].arms


def ok(body: str) -> None:
    check(parse(HEAD + body))


def fails(body: str, message: str, line: int | None = None) -> None:
    with pytest.raises(ZyloError, match=message) as e:
        check(parse(HEAD + body))
    if line is not None:
        assert e.value.line == line


def match_on(value: str, *patterns: str) -> str:
    """A `match` with one arm per pattern."""
    return f"match {value}:\n" + "".join(f"    {p}:\n        print(1)\n" for p in patterns)


# ---- parsing ---------------------------------------------------------------------------------------------------

def test_nested_patterns_parse_into_a_tree():
    arm = arms("match s:\n    Shape.Circle(some(r)):\n        print(r)\n    _:\n        print(0)\n")[0]
    p = arm.pattern
    assert (p.kind, p.name, p.type.name) == ("enum", "Circle", "Shape")
    assert (p.subs[0].kind, p.subs[0].subs[0].kind, p.subs[0].subs[0].name) == ("some", "bind", "r")


def test_tuple_or_and_range_patterns_parse():
    got = arms("match t:\n    (1..=5, true) | (..0, false):\n        print(1)\n    (5.., _):\n        print(2)\n"
               "    (a, b):\n        print(3)\n")
    first = got[0].pattern
    assert first.kind == "or" and [a.kind for a in first.subs] == ["tuple", "tuple"]
    rng = first.subs[0].subs[0]
    assert (rng.kind, rng.inclusive, rng.lo.value, rng.hi.value) == ("range", True, 1, 5)
    assert first.subs[1].subs[0].lo is None and got[1].pattern.subs[0].hi is None


def test_ranges_with_negative_ends_and_exclusive_ends():
    rng = arms("match x:\n    -5..-1:\n        print(1)\n    _:\n        print(2)\n")[0].pattern
    assert (rng.lo.op, rng.hi.op, rng.inclusive) == ("-", "-", False)


@pytest.mark.parametrize("src,message", [
    ("x = 1\nmatch x:\n    ..:\n        print(1)\n", "needs at least one end"),
    ("x = 1\nmatch x:\n    5..=:\n        print(1)\n", "needs an end"),
    ("x = 1\nmatch x:\n    1.5..2:\n        print(1)\n", "integers only"),
    ("x = (1, 2)\nmatch x:\n    (a,):\n        print(1)\n", "at least two elements"),
    ("x = 1\nmatch x:\n    some(a, b):\n        print(1)\n", "exactly one pattern"),
])
def test_malformed_patterns(src, message):
    with pytest.raises(ZyloError, match=message):
        parse(src)


# ---- typing ----------------------------------------------------------------------------------------------------

def test_nested_bindings_get_their_types():
    m = parse(HEAD + "match s:\n    Shape.Named(n, some(k)):\n        print(n, k)\n    _:\n        print(0)\n").body[-1]
    check_program = check(parse(HEAD + "match s:\n    Shape.Named(n, some(k)):\n        print(n, k)\n    _:\n        print(0)\n"))
    arm = check_program.body[-1].arms[0]
    assert arm.binds == ["n", "k"] and [str(v.ty) for v in arm.vars] == ["str", "int"]
    assert m.arms[0].binds == []  # only the checker fills it in


def test_tuple_scrutinee_and_nested_result_tuple():
    ok(match_on("t", "(1, true)", "(_, false)", "(_, true)"))
    ok("match r:\n    ok(v):\n        print(v)\n    err(_):\n        print(0)\n")
    ok("q: Result[tuple[int, str], str] = ok((1, \"a\"))\nmatch q:\n    ok((a, b)):\n        print(a, b)\n    err(e):\n        print(e)\n")


def test_type_errors_inside_patterns():
    fails("match o:\n    some(\"a\"):\n        print(1)\n    _:\n        print(2)\n", "the value here is int, but the pattern is str", 13)
    fails("match t:\n    (a, b, c):\n        print(1)\n", "3 elements, but the value has 2")
    fails("match x:\n    (a, b):\n        print(1)\n", "tuple pattern cannot match int")
    fails("match s:\n    Shape.Circle(some(r), 1):\n        print(1)\n    _:\n        print(2)\n", "carries 1 value")
    fails("match s:\n    Shape.Empty(1):\n        print(1)\n    _:\n        print(2)\n", "carries 0 value")
    fails("match s:\n    Shape.Circle:\n        print(1)\n    _:\n        print(2)\n", "carries 1 value")
    fails("match s:\n    1..2:\n        print(1)\n    _:\n        print(2)\n", "matches an int, but the value is Shape")


def test_range_errors():
    fails("match x:\n    5..5:\n        print(1)\n    _:\n        print(2)\n", "matches no value", 13)
    fails("match x:\n    5..=3:\n        print(1)\n    _:\n        print(2)\n", "matches no value")
    fails("match x:\n    -1..-5:\n        print(1)\n    _:\n        print(2)\n", "matches no value")


def test_names_in_patterns_are_checked():
    fails("match t:\n    (a, a):\n        print(a)\n", "same name twice")
    fails("match r:\n    ok(a) | err(a):\n        print(a)\n", "same names with the same types")
    fails("q: Result[int, int] = ok(1)\nmatch q:\n    ok(a) | err(b):\n        print(1)\n", "same names")
    fails("match o:\n    some(_ | 1):\n        print(1)\n    none:\n        print(2)\n", "`_` cannot be part of an or-pattern")
    ok("q: Result[int, int] = ok(1)\nmatch q:\n    ok(a) | err(a):\n        print(a)\n")
    ok("match o:\n    some(1 | 2):\n        print(1)\n    some(_) | none:\n        print(2)\n")


def test_a_binding_named_like_a_variant_is_rejected():
    src = "enum Color:\n    Red\n    Blue\n\nc: Color? = none\nmatch c:\n    some(Red):\n        print(1)\n    _:\n        print(2)\n"
    with pytest.raises(ZyloError, match="is a variant of Color: write `Color.Red`"):
        check(parse(src))


# ---- exhaustiveness and reachability ---------------------------------------------------------------------------

def test_nested_variants_must_all_be_covered():
    fails(match_on("s", "Shape.Circle(some(r))", "Shape.Rect(a, b)", "Shape.Named(n, k)", "Shape.Empty"),
          r"does not cover Shape\.Circle\(none\)")
    ok(match_on("s", "Shape.Circle(some(r))", "Shape.Circle(none)", "Shape.Rect(a, b)", "Shape.Named(n, k)", "Shape.Empty"))
    fails(match_on("s", "Shape.Named(\"a\", some(k))", "Shape.Named(_, none)", "Shape.Circle(_)", "Shape.Rect(_, _)",
                   "Shape.Empty"), r"does not cover Shape\.Named\(_, some\(_\)\)")


def test_a_wildcard_nested_covers_every_value_of_its_type():
    ok(match_on("s", "Shape.Circle(_)", "Shape.Rect(_, _)", "Shape.Named(_, _)", "Shape.Empty"))


def test_integer_ranges_cover_the_whole_int_type_without_a_wildcard():
    ok(match_on("x", "..0", "0..=9", "10.."))
    ok(match_on("x", "..=-1", "0", "1.."))
    ok(match_on("x", "..=5", "..10", "5.."))


def test_integer_ranges_report_the_gaps():
    fails(match_on("x", "..0", "5.."), r"does not cover 0\.\.=4")
    fails(match_on("x", "1..=3"), r"does not cover \.\.=0, 4\.\.")
    fails(match_on("x", "0", "2..=3"), r"does not cover \.\.=-1, 1, 4\.\.")
    fails(match_on("o", "some(1..=5)", "none"), r"does not cover some\(\.\.=0\), some\(6\.\.\)")


def test_a_plain_int_or_str_match_still_asks_for_a_wildcard():
    fails(match_on("x", "1", "2"), "needs a final `_` arm")
    fails("w = \"a\"\n" + match_on("w", "\"a\""), "needs a final `_` arm")


def test_tuples_are_covered_column_by_column():
    ok(match_on("t", "(1, true)", "(_, false)", "(_, true)"))
    fails(match_on("t", "(1, true)", "(_, false)"), r"does not cover \(\.\.=0, true\), \(2\.\., true\)")
    ok(match_on("t", "(..=0, _)", "(1.., true)", "(1.., false)"))
    ok(match_on("t", "(1, _) | (_, true)", "(_, false)"))


def test_guarded_arms_never_count_toward_coverage():
    fails("match x:\n    ..0:\n        print(1)\n    0.. if x > 5:\n        print(2)\n", "does not cover 0")
    ok("match x:\n    ..0:\n        print(1)\n    0.. if x > 5:\n        print(2)\n    0..:\n        print(3)\n")
    fails("match o:\n    some(v) if v > 0:\n        print(1)\n    none:\n        print(2)\n", r"does not cover some\(_\)")


def test_unreachable_arms_and_alternatives_are_rejected():
    fails("match x:\n    0..10:\n        print(1)\n    2..5:\n        print(2)\n    _:\n        print(3)\n", "can never run", 15)
    fails("match o:\n    some(_):\n        print(1)\n    some(3):\n        print(2)\n    none:\n        print(3)\n", "can never run", 15)
    fails("match o:\n    some(_) | some(1):\n        print(1)\n    none:\n        print(2)\n", "alternative can never run", 13)
    fails(match_on("x", "..=5", "..0", "_"), "can never run", 15)
    fails("match t:\n    (_, true):\n        print(1)\n    (_, false):\n        print(2)\n    (1, _):\n        print(3)\n",
          "can never run")


def test_a_repeated_pattern_is_matched_twice():
    fails("match o:\n    some(1):\n        print(1)\n    some(1):\n        print(2)\n    _:\n        print(3)\n", "`some\\(1\\)` is matched twice")
    fails("match x:\n    1..5:\n        print(1)\n    1..5:\n        print(2)\n    _:\n        print(3)\n", "matched twice")


def test_a_later_arm_may_overlap_a_guarded_one():
    ok("match x:\n    0..10 if x > 5:\n        print(1)\n    2..5:\n        print(2)\n    _:\n        print(3)\n")
    ok("match x:\n    0..10:\n        print(1)\n    5..20:\n        print(2)\n    _:\n        print(3)\n")


def test_guards_see_nested_bindings():
    ok("match s:\n    Shape.Named(n, some(k)) if k > 0 and n == \"a\":\n        print(n, k)\n    _:\n        print(0)\n")
    fails("match s:\n    Shape.Named(n, some(k)) if k:\n        print(n)\n    _:\n        print(0)\n", "guard must be bool")


def test_exhaustiveness_algorithm_directly():
    from zylo.domain.types import INT, OptionT
    from zylo.semantics.exhaustive import INT_MAX, INT_MIN, Alt, Con, Space

    space = Space(lambda ty: {})
    ints = [Con(("i", INT_MIN, -1)), Con(("i", 0, INT_MAX))]
    assert space.missing([[p] for p in ints], [INT]) is None
    assert space.missing([[ints[0]]], [INT]) == [["0.."]]
    some = lambda p: Con(("v", "some"), (p,))  # noqa: E731
    opt = OptionT(INT)
    rows = [[some(Con(("i", 1, 3)))], [Con(("v", "none"))]]
    assert space.missing(rows, [opt]) == [["some(..=0)"], ["some(4..)"]]
    assert space.useful(rows, [some(Con(("i", 2, 2)))], [opt]) is False
    assert space.useful(rows, [some(Con(("i", 3, 9)))], [opt]) is True
    assert space.useful(rows, [Alt((some(Con(("i", 1, 1))), some(Con(("i", 3, 4)))))], [opt]) is True


# ---- codegen ---------------------------------------------------------------------------------------------------

def test_codegen_nested_patterns_and_ranges():
    rs = transpile_source(HEAD + "match s:\n    Shape.Circle(some(r)):\n        print(r)\n    Shape.Rect(1..=5, h) if h > 0:\n"
                          "        print(h)\n    _:\n        print(0)\n")
    assert "Shape::Circle(Some(r))" in rs and "Shape::Rect(1..=5, h) if (h > 0i64)" in rs


def test_codegen_open_and_exclusive_ranges_are_written_inclusive():
    rs = transpile_source("x = 1\nmatch x:\n    ..0:\n        print(1)\n    0..5:\n        print(2)\n    5..:\n        print(3)\n")
    assert "..=-1 =>" in rs and "0..=4 =>" in rs and "5.. =>" in rs


def test_codegen_string_literals_nested_in_a_pattern_become_guards():
    rs = transpile_source('o: str? = none\nmatch o:\n    some("a" | "b"):\n        print(1)\n    _:\n        print(2)\n')
    assert rs.count("Some(__s0) if __s0 ==") == 2


# ---- formatter -------------------------------------------------------------------------------------------------

SAMPLE = '''\
enum Shape:
    Circle(float?)
    Rect(int, int)

fn f(s: Shape, o: Result[tuple[int, str], str], t: tuple[int, bool]) -> int:
    match s:
        Shape.Circle(some(r)):
            return 1
        Shape.Rect(1..=5, h) if h > 0:
            return 2
        Shape.Rect(..0 | 10.., _):
            return 3
        _:
            return 4
    match o:
        ok((0, "a" | "b")):
            return 1
        ok((n, _)) if n > 1:
            return 2
        err(_):
            return 3
        ok(_):
            return 4
    match t:
        (..=0, true) | (1.., false):
            return 1
        (_, _):
            return 2
'''


def test_formatter_round_trips_nested_patterns_and_ranges():
    assert format_source(SAMPLE) == SAMPLE
    messy = "x = 1\nmatch x:\n    1 .. 5|7..=9:\n        print(1)\n    ..=0 | 10 ..:\n        print(2)\n    _:\n        print(3)\n"
    assert format_source(messy) == "x = 1\nmatch x:\n    1..5 | 7..=9:\n        print(1)\n    ..=0 | 10..:\n        print(2)\n    _:\n        print(3)\n"


@pytest.mark.parametrize("width", [20, 30, 40, 60, 88])
def test_formatter_wraps_long_patterns_and_keeps_the_meaning(width):
    roundtrip(SAMPLE, width)
    long = ("enum E:\n    A(int?, tuple[int, str])\n\nfn g(e: E) -> int:\n    match e:\n"
            "        E.A(some(1 | 2 | 3 | 4 | 5 | 6), (10..=20, \"some long text here\")) if e == e:\n            return 1\n"
            "        E.A(_, _) | E.A(none, (_, \"x\")):\n            return 2\n")
    out = roundtrip(long, width)
    assert all(len(ln) <= max(width, 40) for ln in out.splitlines() if "some long text" not in ln)


def test_a_long_pattern_is_broken_one_part_per_line():
    src = "enum E:\n    A(int?, int?)\n\nfn g(e: E) -> int:\n    match e:\n        E.A(some(1), some(2)):\n            return 1\n        _:\n            return 2\n"
    assert format_source(src, 20) == ("enum E:\n    A(int?, int?)\n\nfn g(e: E) -> int:\n    match e:\n        E.A(\n"
                                      "            some(1),\n            some(2),\n        ):\n            return 1\n        _:\n            return 2\n")


# ---- editor ----------------------------------------------------------------------------------------------------

EDITOR = '''enum Shape:
    Circle(float?)
    Named(str, int?)
    Empty

fn describe(s: Shape) -> str:
    match s:
        Shape.Circle(some(radius)):
            return f"{radius}"
        Shape.Named(name, some(n)) if name == "a":
            return name
        Shape.Named(name, _):
            return name
        Shape.Empty | Shape.Circle(none):
            return "e"
'''


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path, {"doc.zy": EDITOR})


def test_hover_on_bindings_in_nested_patterns(ws):
    assert "(variable) radius: float" in ws.hover("doc.zy", "radius)")
    assert "(variable) radius: float" in ws.hover("doc.zy", "radius}")
    assert "(variable) name: str" in ws.hover("doc.zy", "name, some")
    assert "(variable) n: int" in ws.hover("doc.zy", "n))")


def test_definition_of_a_binding_goes_to_the_pattern(ws):
    line, ch = 7, EDITOR.splitlines()[7].index("radius")
    assert ws.definition("doc.zy", "radius}") == [("doc.zy", line, ch)]
    assert ws.definition("doc.zy", "name\n") == [("doc.zy", 9, EDITOR.splitlines()[9].index("name"))]


def test_a_name_bound_by_every_alternative_is_one_variable(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": "fn f(r: Result[int, int]) -> int:\n    match r:\n        ok(v) | err(v):\n            return v + 1\n"})
    out = w.renamed("doc.zy", "v)", "value")
    assert "ok(value) | err(value):\n            return value + 1" in out["doc.zy"]
    assert "(variable) v: int" in w.hover("doc.zy", "v + 1")


def test_rename_a_binding_in_a_nested_pattern(ws):
    out = ws.renamed("doc.zy", "radius)", "r")
    assert "Shape.Circle(some(r)):\n            return f\"{r}\"" in out["doc.zy"]
    out = ws.renamed("doc.zy", "name, some", "label")
    text = out["doc.zy"]
    assert "Shape.Named(label, some(n)) if label == \"a\":\n            return label" in text
    assert "Shape.Named(name, _):\n            return name" in text  # the other arm has its own `name`


def test_rename_a_variant_in_a_nested_pattern(ws):
    out = ws.renamed("doc.zy", "Circle", "Round")
    assert "Shape.Circle(some" not in out["doc.zy"] and "Shape.Round(some(radius))" in out["doc.zy"]
    assert "Shape.Empty | Shape.Round(none)" in out["doc.zy"]


def test_bindings_of_a_wrapped_pattern_complete_inside_the_arm(tmp_path):
    from zylo.application.completion import complete
    from zylo.application.editor import open_project

    text = ("enum E:\n    A(int?, int?)\n\nfn g(e: E) -> int:\n    match e:\n        E.A(\n            some(left),\n"
            "            some(right),\n        ):\n            return 1\n        _:\n            return 0\n")
    path = tmp_path / "doc.zy"
    path.write_text(text)
    items = {c.label for c in complete(open_project(path), path, 10, 20)}
    assert {"left", "right", "e"} <= items


def test_missing_arm_quick_fix_ignores_arms_that_only_match_part_of_a_variant(tmp_path):
    from zylo.application.actions import code_actions
    from zylo.application.editor import open_project

    text = ("enum E:\n    A(int?)\n    B\n\nfn g(e: E) -> int:\n    match e:\n        E.A(some(1)):\n            return 1\n"
            "        E.B:\n            return 2\n")
    path = tmp_path / "doc.zy"
    path.write_text(text)
    actions = code_actions(open_project(path), path, 7, 1)
    assert actions and "E.A" in actions[0].title


# ---- the algorithm against brute force -------------------------------------------------------------------------

def test_exhaustiveness_and_reachability_agree_with_brute_force():
    """Random pattern lists over `tuple[int?, bool, Result[int, bool]]`, checked against every value that matters."""
    import itertools
    import random

    from zylo.domain.types import BOOL, INT, OptionT, ResultT, TupleT
    from zylo.semantics.exhaustive import INT_MAX, INT_MIN, Alt, Con, Space

    ty = TupleT((OptionT(INT), BOOL, ResultT(INT, BOOL)))
    space = Space(lambda t: {})
    rng = random.Random(23)

    def interval() -> Con:
        lo = rng.choice([INT_MIN, 0, 1, 2, 3, 5])
        hi = rng.choice([INT_MAX, 1, 2, 4, 6])
        return Con(("i", lo, max(lo, hi))) if lo != INT_MIN or hi != 1 else Con(("i", INT_MIN, 1))

    def pat(kind: str, depth: int = 0):
        if rng.random() < 0.3:
            return None
        if kind == "int":
            return Alt((interval(), interval())) if rng.random() < 0.15 else interval()
        if kind == "bool":
            return Con(("b", rng.random() < 0.5))
        if kind == "opt":
            return Con(("v", "none")) if rng.random() < 0.3 else Con(("v", "some"), (pat("int"),))
        if kind == "res":
            return Con(("v", "ok"), (pat("int"),)) if rng.random() < 0.5 else Con(("v", "err"), (pat("bool"),))

    def matches(p, value) -> bool:
        if p is None:
            return True
        if isinstance(p, Alt):
            return any(matches(a, value) for a in p.alts)
        c = p.ctor
        if c[0] == "i":
            return c[1] <= value <= c[2]
        if c[0] == "b":
            return c[1] == value
        tag, payload = value
        return c[1] == tag and all(matches(s, payload) for s in p.subs)

    ints = [INT_MIN, -1, 0, 1, 2, 3, 4, 5, 6, 7, INT_MAX]
    opts = [("none", None)] + [("some", i) for i in ints]
    ress = [("ok", i) for i in ints] + [("err", b) for b in (True, False)]
    values = list(itertools.product(opts, (True, False), ress))

    def row_matches(row, v) -> bool:
        return row[0] is None and row[1] is None and row[2] is None or all(matches(p, x) for p, x in zip(row, v))

    stats = {True: 0, False: 0}
    for _ in range(300):
        rows = [[pat("opt"), pat("bool"), pat("res")] for _ in range(rng.randint(1, 5))]
        # Con tuples need the tuple constructor at the top: rows here are the columns of a tuple pattern
        tuple_rows = [[Con(("t",), tuple(r))] for r in rows]
        exhaustive = all(any(row_matches(r, v) for r in rows) for v in values)
        stats[exhaustive] += 1
        assert (space.missing(tuple_rows, [ty]) is None) == exhaustive, rows
        extra = [pat("opt"), pat("bool"), pat("res")]
        useful = any(row_matches(extra, v) and not any(row_matches(r, v) for r in rows) for v in values)
        assert space.useful(tuple_rows, [Con(("t",), tuple(extra))], [ty]) == useful, (rows, extra)
    assert stats[True] > 5 and stats[False] > 5  # the sample exercises both outcomes
