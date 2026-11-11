import pytest

from zylo import ast
from zylo.checker import check
from zylo.errors import ZyloError
from zylo.lexer import tokenize
from zylo.parser import parse
from zylo.types import BOOL, FLOAT, INT, STR, UNIT, DictT, ListT, OptionT, SetT, TupleT


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


def ty(src: str, var: str = "x"):
    p = checked(src)
    return next(s.var.ty for s in ast.walk(p) if isinstance(s, ast.Assign) and getattr(s.target, "id", None) == var and s.declares)


# ---- strings: escapes, quotes, f-strings --------------------------------------------------------------

def test_escapes_and_single_quotes():
    assert parse('x = "a\\tb\\n\\\\\\""\n').body[0].value.value == 'a\tb\n\\"'
    assert parse("x = 'it\\'s'\n").body[0].value.value == "it's"
    assert parse('x = "a # not a comment"\n').body[0].value.value == "a # not a comment"


def test_unknown_escape_and_unterminated_string():
    with pytest.raises(ZyloError, match="unknown escape sequence"):
        tokenize('x = "a\\qb"\n')
    with pytest.raises(ZyloError, match="unterminated string"):
        tokenize('x = "abc\n')
    with pytest.raises(ZyloError, match="unterminated string"):
        tokenize('x = "abc\\"\n')


def test_fstring_parts_and_specs():
    f = parse('x = f"a {1 + 2} b {y:.2f} {{c}}"\n').body[0].value
    assert isinstance(f, ast.FString)
    assert f.parts[0] == "a " and isinstance(f.parts[1].value, ast.BinOp)
    assert f.parts[2] == " b " and f.parts[3].spec == ".2f"
    assert f.parts[4] == " {c}"


def test_fstring_expressions_with_brackets_quotes_and_colons():
    f = parse('x = f"{d[\'k\']} {xs[1:2]} {g(1, 2)}"\n').body[0].value
    assert [type(p.value).__name__ for p in f.parts if isinstance(p, ast.FormatPart)] == ["Index", "Slice", "Call"]


@pytest.mark.parametrize("src,msg", [
    ('x = f"{a"\n', "unterminated '{'"),
    ('x = f"a}b"\n', "single '}'"),
    ('x = f"{}"\n', "empty expression"),
    ('x = f"{a b}"\n', "unexpected"),
])
def test_fstring_syntax_errors(src, msg):
    with pytest.raises(ZyloError, match=msg):
        parse(src)


def test_fstring_types_and_spec_checks():
    assert ty('n = 1\nx = f"{n} {n + 1:>4} {2.5:.1f} {[1]} {n:05d} {n:x}"\n') == STR
    fails('x = f"{\'s\':d}"\n', "format type `d` needs int, not str")
    fails('x = f"{1:f}"\n', "format type `f` needs float")
    fails('x = f"{1:@}"\n', "unsupported format specification")
    fails('x = f"{print(1)}"\n', "produces no value")


# ---- tuples ---------------------------------------------------------------------------------------------------

def test_tuple_types_indexing_and_comparison():
    assert ty('x = (1, "a", 2.5)\n') == TupleT((INT, STR, FLOAT))
    assert ty('t = (1, "a")\nx = t[1]\n') == STR
    assert ty('x = (1, 2) == (1, 2)\n') == BOOL
    assert ty('fn f() -> tuple[int, str]:\n    return 1, "a"\nx = f()\n') == TupleT((INT, STR))


def test_tuple_errors():
    with pytest.raises(ZyloError, match="at least 2 elements"):
        parse("x = (1,)\n")
    with pytest.raises(ZyloError, match="at least 2 element types"):
        parse("x: tuple[int] = y\n")
    fails("t = (1, 2)\nx = t[2]\n", "indexed with a literal 0..1")
    fails("t = (1, 2)\ni = 0\nx = t[i]\n", "indexed with a literal")
    fails("t = (1, 2)\nt[0] = 5\n", "tuples are immutable")


def test_unpacking_declares_reassigns_and_checks():
    p = checked("a, b = 1, \"x\"\na, b = 2, \"y\"\n")
    assert p.body[0].declares == [True, True] and p.body[1].declares == [False, False]
    assert p.body[0].vars[0] is p.body[1].vars[0] and p.body[0].vars[0].mutable
    fails("a, b = (1, 2, 3)\n", "cannot unpack 3 values into 2 variables", line=1)
    fails("a, b = 5\n", "cannot unpack int into 2 variables")
    fails("a, a = 1, 2\n", "appears twice")
    fails("a = 1\na, b = \"x\", 2\n", "'a' is int but str is assigned")
    with pytest.raises(ZyloError, match="plain variable names"):
        parse("a[0], b = 1, 2\n")


def test_for_loop_unpacking():
    p = checked("pairs: list[tuple[int, str]] = [(1, \"a\")]\nfor n, s in pairs:\n    print(n + 1, s + \"!\")\n")
    assert [i.ty for i in p.body[1].infos] == [INT, STR]
    fails("for a, b in [1, 2]:\n    print(a)\n", "cannot unpack int into 2 variables")
    fails("for a, a in [(1, 2)]:\n    print(a)\n", "duplicate loop variable")


# ---- dict and set ------------------------------------------------------------------------------------------------

def test_dict_and_set_types_and_literals():
    assert ty('x = {"a": 1}\n') == DictT(STR, INT)
    assert ty('x = {1, 2}\n') == SetT(INT)
    assert ty('x: dict[str, list[int]] = {}\n') == DictT(STR, ListT(INT))
    assert ty('x: set[str] = {}\n') == SetT(STR)
    assert ty('d = {"a": 1}\nx = d["a"]\n') == INT
    assert ty('x = {(1, "a"): True}\n'.replace("True", "true")) == DictT(TupleT((INT, STR)), BOOL)


def test_dict_set_errors():
    fails("x = {}\n", "cannot infer the type of `{}`")
    fails('x = {"a": 1, 2: 3}\n', "mixes str and int keys")
    fails('x = {"a": 1, "b": "z"}\n', "mixes int and str values")
    fails("x = {1.5: 1}\n", "dict keys must be int, str, bool or tuples")
    fails("x = {1.5, 2.5}\n", "set elements must be")
    fails("x = {1, \"a\"}\n", "set mixes int and str")
    fails("x: dict[float, int] = {}\n", "dict keys must be")
    fails('d = {"a": 1}\nx = d[1]\n', "this dict has str keys, not int")
    fails('d = {"a": 1}\nd["b"] = "x"\n', "assigning str to a value of dict")


def test_in_operator():
    assert ty('x = 1 in [1, 2]\n') == BOOL
    assert ty('x = "a" not in {"a": 1}\n') == BOOL
    assert ty('x = "ab" in "cabd"\n') == BOOL
    assert ty('x = 1 in {1, 2}\n') == BOOL
    fails('x = "a" in [1]\n', "needs a int on the left")
    fails("x = 1 in 5\n", "needs a list, str, set or dict on the right")
    assert parse("x = a not in b\n").body[0].value.op == "not in"
    e = parse("x = not a in b\n").body[0].value
    assert e.op == "not" and e.operand.op == "in"


def test_iteration_element_types_and_len():
    p = checked('d = {"a": 1}\nfor k in d:\n    print(k + "!")\nfor c in "hi":\n    print(c + "!")\nfor v in {1, 2}:\n    print(v + 1)\nn = len(d) + len("x") + len({1})\n')
    assert [s.info.ty for s in p.body if isinstance(s, ast.For)] == [STR, STR, INT]
    fails("for x in 5:\n    print(x)\n", "can only iterate over range")


def test_mutation_marking_for_collections():
    p = checked('d = {"a": 1}\nd["b"] = 2\nxs = [1]\nxs.append(2)\nys = [1]\nprint(ys.count(1))\ns: set[int] = {}\ns.add(1)\n')
    muts = {s.target.id: s.var.mutable for s in p.body if isinstance(s, ast.Assign) and isinstance(s.target, ast.Name)}
    assert muts == {"d": True, "xs": True, "ys": False, "s": True}


# ---- slices and string indexing ---------------------------------------------------------------------------------------

def test_slices_and_string_indexing_types():
    assert ty("xs = [1, 2, 3]\nx = xs[1:]\n") == ListT(INT)
    assert ty('s = "abc"\nx = s[:2]\n') == STR
    assert ty('s = "abc"\nx = s[0]\n') == STR
    assert ty('xs = [1]\ni = 1\nx = xs[i:i + 1]\n') == ListT(INT)
    fails("x = 5\ny = x[1:2]\n", "cannot slice int")
    fails('xs = [1]\ny = xs["a":]\n', "expected int")


# ---- methods -----------------------------------------------------------------------------------------------------------------

def test_str_method_types():
    src = 's = "a b"\n'
    assert ty(src + 'x = s.split()\n') == ListT(STR)
    assert ty(src + 'x = s.split(",")\n') == ListT(STR)
    assert ty(src + 'x = ",".join(["a"])\n') == STR
    assert ty(src + 'x = s.find("b")\n') == OptionT(INT)
    assert ty(src + 'x = s.upper().strip().replace("a", "b")\n') == STR
    assert ty(src + 'x = s.startswith("a")\n') == BOOL
    assert ty(src + 'x = s.count("a")\n') == INT
    assert ty(src + 'x = s.chars()\n') == ListT(STR)


def test_list_dict_set_method_types():
    assert ty("xs = [1]\nx = xs.pop()\n") == OptionT(INT)
    assert ty("xs = [1]\nx = xs.index(1)\n") == OptionT(INT)
    assert ty("xs = [1]\nx = xs.remove(1)\n") == BOOL
    assert ty('d = {"a": 1}\nx = d.get("a")\n') == OptionT(INT)
    assert ty('d = {"a": 1}\nx = d.get("a", 0)\n') == INT
    assert ty('d = {"a": 1}\nx = d.items()\n') == ListT(TupleT((STR, INT)))
    assert ty('d = {"a": 1}\nx = d.keys()\n') == ListT(STR)
    assert ty("s = {1}\nx = s.union({2})\n") == SetT(INT)
    assert ty("s = {1}\nx = s.remove(1)\n") == BOOL


def test_method_errors():
    fails('s = "a"\nx = s.fly()\n', "has no method 'fly' \\(available: strip")
    fails('s = "a"\nx = s.split(1)\n', "argument 1 of split: expected str, got int")
    fails('s = "a"\nx = s.upper(1)\n', "takes 0 argument")
    fails("xs = [1]\nxs.append(\"a\")\n", "argument 1 of append: expected int, got str")
    fails("xs = [1.5]\nxs.extend([1])\n", "argument 1 of extend")
    fails("xs = [(1.5, 2)]\nxs.sort()\n", "cannot sort a list of")
    fails("xs: list[int?] = []\nx = xs.pop()\n", "pop\\(\\) on a list of Option")
    fails('d = {"a": 1}\nx = d.get()\n', "takes 1 or 2 argument")
    fails("s = {1}\ns.add(\"a\")\n", "argument 1 of add")
    fails("x = [1].fly()\n", "has no method 'fly' \\(available: append")


# ---- builtin functions -----------------------------------------------------------------------------------------------------------

def test_builtin_function_types():
    assert ty("x = sum([1, 2])\n") == INT
    assert ty("x = sum([1.5])\n") == FLOAT
    assert ty("x = min([3, 1])\n") == INT
    assert ty("x = max(1, 2)\n") == INT
    assert ty('x = max("a", "b")\n') == STR
    assert ty("x = abs(-2.5)\n") == FLOAT
    assert ty("x = sorted([3, 1])\n") == ListT(INT)
    assert ty("x = reversed([\"a\"])\n") == ListT(STR)
    assert ty('x = enumerate(["a"])\n') == ListT(TupleT((INT, STR)))
    assert ty('x = zip([1], ["a"])\n') == ListT(TupleT((INT, STR)))
    assert ty("x = sqrt(4.0)\n") == FLOAT and ty("x = round(2.5)\n") == INT and ty("x = pow(2.0, 3.0)\n") == FLOAT


def test_builtin_function_errors():
    fails('x = sum(["a"])\n', "sum expects a list of int or float")
    fails("x = min(1, 2.5)\n", "min compares two")
    fails("x = min()\n", "min takes 1 or 2")
    fails("x = sqrt(4)\n", "sqrt expects float, not int")
    fails("x = abs(\"a\")\n", "abs expects int or float")
    fails("x = zip([1], 2)\n", "zip expects two lists")
    fails("x = sorted([1.5, 2.5][0])\n", "sorted expects a list")


def test_user_functions_may_shadow_new_builtins_but_not_core_ones():
    checked("fn sum(xs: list[int]) -> int:\n    return 7\nx = sum([1])\n")
    checked("fn max(a: int) -> int:\n    return a\nx = max(1)\n")
    fails("fn len(x: int) -> int:\n    return x\n", "is a built-in function")
    fails("fn print(x: int):\n    return\n", "is a built-in function")


def test_print_takes_any_number_of_values():
    checked('print()\nprint(1)\nprint(1, "a", [2])\n')
    fails("print(print(1))\n", "produces no value")
