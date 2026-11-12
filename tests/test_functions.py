"""First-class functions and closures: function types, function values, anonymous and local functions, captures."""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application.compile import transpile_source
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import INT, STR, UNIT, FnT, ListT
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check, check_all
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


def run(src: str, tmp_path: Path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---- types and parsing ---------------------------------------------------------------------------------

def test_function_types_print_like_the_source():
    assert str(FnT((INT, STR), INT)) == "fn(int, str) -> int"
    assert str(FnT((INT,), UNIT)) == "fn(int)"
    assert str(FnT((), INT)) == "fn() -> int"


def test_function_type_in_a_parameter_and_return():
    p = checked("fn f(g: fn(int) -> int, h: fn(str)) -> fn() -> bool:\n    return fn() => true\n")
    assert str(p.body[0].ty) == "fn(fn(int) -> int, fn(str)) -> fn() -> bool"


def test_function_types_nest_in_collections():
    p = checked("xs: list[fn(int) -> int] = []\nd: dict[str, fn(int)] = {}\no: (fn(int) -> int)? = none\n")
    assert [str(s.value.ty) for s in p.body] == ["list[fn(int) -> int]", "dict[str, fn(int)]", "fn(int) -> int?"]


def test_question_mark_after_a_function_type_belongs_to_its_return():
    p = checked("fn f(g: fn(int) -> int?) -> int?:\n    return g(1)\n")
    assert str(p.body[0].ty.params[0]) == "fn(int) -> int?"


def test_anonymous_function_parses():
    lam = parse("f = fn(x: int, y) -> int => x + 1\n").body[0].value
    assert isinstance(lam, ast.Lambda)
    assert [p.name for p in lam.params] == ["x", "y"]
    assert lam.params[1].type is None and lam.ret.name == "int"


def test_anonymous_function_needs_an_arrow():
    with pytest.raises(ZyloError, match="expected '=>'.*named `fn`"):
        parse("f = fn(x: int):\n    return x\n")


def test_local_fn_becomes_a_named_lambda():
    f = parse("fn f():\n    fn g(x: int) -> int:\n        return x\n").body[0]
    assign = f.body[0]
    assert isinstance(assign, ast.Assign) and isinstance(assign.value, ast.Lambda) and assign.value.name == "g"


def test_local_fn_rejects_default_values_and_function_values_reject_named_arguments():
    with pytest.raises(ZyloError, match="local function cannot have default values"):
        parse("fn f():\n    fn g(x: int = 1) -> int:\n        return x\n")
    fails("fn f(g: fn(int) -> int) -> int:\n    return g(x=1)\n", "positional arguments only")


def test_call_of_any_expression_parses():
    e = parse("hs[0](1)(2)\n").body[0].expr
    assert isinstance(e, ast.CallValue) and isinstance(e.callee, ast.CallValue)


# ---- checking ---------------------------------------------------------------------------------------------

def test_top_level_function_as_a_value():
    p = checked("fn double(x: int) -> int:\n    return x * 2\nf = double\ny = f(2)\n")
    assert p.body[1].value.ty == FnT((INT,), INT)
    assert p.body[2].value.ty == INT


def test_calling_a_parameter_and_checking_its_arguments():
    fails("fn f(g: fn(int) -> int) -> int:\n    return g(\"a\")\n", "argument 1 of g: expected int, got str")
    fails("fn f(g: fn(int) -> int) -> int:\n    return g(1, 2)\n", "g takes 1 argument")


def test_calling_a_non_function_variable():
    fails("n = 1\nn(2)\n", "'n' is a int, not a function")
    fails("n = 1\nn(1)(2)\n", "not a function")
    fails("x = (1)(2)\n", "cannot call a value of type int")


def test_mismatched_function_arguments():
    fails("fn f(g: fn(int) -> int):\n    return\nf(fn(x: str) => 1)\n", "parameter 'x' is str but int is expected")
    fails("fn f(g: fn(int) -> int):\n    return\nf(fn(x: int) => \"a\")\n", "expected fn\\(int\\) -> int, got fn\\(int\\) -> str")
    fails("fn f(g: fn(int) -> int):\n    return\nf(fn(x: int, y: int) => x)\n", "takes 2 parameter")


def test_parameter_type_can_only_be_omitted_when_the_context_gives_it():
    fails("f = fn(x) => x\n", "parameter 'x' needs a type")
    checked("fn f(g: fn(int) -> int):\n    return\nf(fn(x) => x)\n")


def test_return_type_is_inferred_or_checked():
    p = checked("f = fn(x: int) => x > 1\n")
    assert p.body[0].value.ty == FnT((INT,), checked("x = true\n").body[0].value.ty)
    fails("f = fn(x: int) -> str => x\n", "declares -> str but its body is int")


def test_function_field_call_and_value():
    src = "struct B:\n    on: fn(int) -> int\n    n: int\n\n    fn go(self) -> int:\n        return self.on(self.n)\n"
    p = checked(src + "b = B(fn(x: int) => x + 1, 2)\ny = b.on(5)\nz = b.on\n")
    assert p.body[2].value.field_call
    assert p.body[3].value.ty == FnT((INT,), INT)


def test_field_and_method_cannot_share_a_name():
    fails("struct B:\n    go: fn(int)\n\n    fn go(self):\n        return\n", "both a field and a method")


def test_field_that_is_not_a_function_is_not_callable():
    fails("struct B:\n    n: int\nb = B(1)\nb.n(2)\n", "field of B, not a method")


def test_functions_cannot_be_compared_or_printed():
    fails("fn f():\n    return\ng = f\nprint(g == g)\n", "functions cannot be compared")
    fails("fn f():\n    return\nprint(f)\n", "functions cannot be printed")
    fails("fn f():\n    return\nprint([f])\n", "functions cannot be printed")
    fails("fn f():\n    return\ns = f\"{f}\"\n", "functions cannot be printed")
    fails("struct S:\n    g: fn()\ns = S(fn() => print(1))\nprint(s)\n", "functions cannot be printed")
    fails("struct S:\n    g: fn()\ns = S(fn() => print(1))\nprint(s == s)\n", "functions cannot be compared")
    fails("fn f():\n    return\nxs = [f]\nprint(f in xs)\n", "functions cannot be compared")


def test_builtin_and_extern_functions_cannot_be_values():
    fails("f = len\n", "built-in function")
    fails('extern fn e(x: int) -> int = "x"\nf = e\n', "extern fn")


def test_function_in_a_cyclic_struct_is_fine():
    checked("struct Node:\n    next_: fn(int) -> Node\n")


# ---- closures: capture and mutation -----------------------------------------------------------------------

def test_closure_records_what_it_captures():
    p = checked("n = 1\nm = 2\nf = fn(x: int) => x + n\n")
    lam = p.body[2].value
    assert [o.name for o, _ in lam.captures] == ["n"]
    assert lam.captures[0][1].captured and lam.captures[0][1] is not lam.captures[0][0]


def test_captured_variables_cannot_be_modified():
    fails("fn f():\n    n = 1\n    fn bump():\n        n = 2\n    bump()\n", "cannot modify 'n'", line=4)
    fails("fn f():\n    xs = [1]\n    g = fn(x: int) => xs.append(x)\n", "cannot modify 'xs'")
    fails("fn f():\n    xs = [1]\n    fn g():\n        xs[0] = 2\n", "cannot modify 'xs'")
    fails("fn f():\n    n = 1\n    fn g():\n        n += 1\n", "cannot modify 'n'")


def test_own_variables_and_parameters_can_be_modified_in_a_closure():
    checked("fn f():\n    fn g(n: int) -> int:\n        total = 0\n        total += n\n        n = n + 1\n        return total + n\n    g(1)\n")


def test_closures_cannot_capture_self():
    fails("struct S:\n    n: int\n\n    fn f(self) -> fn() -> int:\n        return fn() => self.n\n", "cannot capture `self`")


def test_nested_closures_capture_through_each_level():
    p = checked("fn f(a: int) -> fn(int) -> fn(int) -> int:\n    return fn(b: int) => fn(c: int) => a + b + c\n")
    outer = p.body[0].body[0].value
    inner = outer.expr
    assert [o.name for o, _ in outer.captures] == ["a"]
    assert [o.name for o, _ in inner.captures] == ["a", "b"] and inner.captures[0][0] is outer.captures[0][1]


def test_local_fn_cannot_call_itself():
    fails("fn f():\n    fn g(n: int) -> int:\n        return g(n)\n", "undefined function 'g'")


def test_local_fn_must_return_on_every_path():
    fails("fn f():\n    fn g(n: int) -> int:\n        if n > 0:\n            return 1\n", "can finish without a `return`")


def test_variable_function_shadows_a_top_level_function():
    p = checked("fn g() -> int:\n    return 1\nfn f(g: fn() -> str) -> str:\n    return g()\n")
    assert p.body[1].body[0].value.ty == STR


# ---- higher-order list methods ----------------------------------------------------------------------------

def test_higher_order_method_types():
    p = checked("xs = [1, 2, 3]\na = xs.map(fn(x) => str(x))\nb = xs.filter(fn(x) => x > 1)\nc = xs.fold(0.5, fn(s, x) => s + 1.0)\n"
                "d = xs.reduce(fn(a, b) => a + b)\ne = xs.any(fn(x) => x > 2)\nf = xs.all(fn(x) => x > 2)\ng = xs.find(fn(x) => x > 2)\n"
                "xs.sort_by(fn(x) => -x)\n")
    assert [str(s.value.ty) for s in p.body[1:8]] == ["list[str]", "list[int]", "float", "int?", "bool", "bool", "int?"]


def test_higher_order_method_errors():
    fails("xs = [1]\nxs.map(fn(x) => print(x))\n", "must return a value")
    fails("xs = [1]\nxs.filter(fn(x) => x)\n", "expected a function fn\\(int\\) -> bool, got fn\\(int\\) -> int")
    fails("xs = [1]\nxs.map(1)\n", "expected a function")
    fails("xs = [1]\nxs.map(fn(a, b) => a)\n", "takes 2 parameter")
    fails("xs = [1]\nxs.sort_by(fn(x) => [x])\n", "sort_by needs a key")
    fails("xs = [1]\nxs.fold(0, fn(a, b) => \"s\")\n", "got fn\\(int, int\\) -> str")
    fails("xs = [fn(x: int) => x]\nxs.sort()\n", "cannot sort")


def test_every_error_is_reported():
    prog = parse("a = [1]\na.map(1)\nb = fn(x) => x\nc = 1(2)\n")
    assert len(check_all(prog)) == 3


# ---- tooling --------------------------------------------------------------------------------------------------

FORMAT_SAMPLE = '''\
fn make(n:int)->fn(int)->int:
    fn add(x: int) -> int:
        return x+n
    return add

h: (fn(int)->int)? = none
f=fn(a: int,b)->int=>a+1
print(make(1)(2),[f][0](1,2),(fn(x: int)=>x)(3))
'''


def test_formatter_prints_functions_and_is_stable():
    once = format_source(FORMAT_SAMPLE)
    assert once == '''\
fn make(n: int) -> fn(int) -> int:
    fn add(x: int) -> int:
        return x + n
    return add

h: (fn(int) -> int)? = none
f = fn(a: int, b) -> int => a + 1
print(make(1)(2), [f][0](1, 2), (fn(x: int) => x)(3))
'''
    assert format_source(once) == once


# ---- end to end -----------------------------------------------------------------------------------------------

@needs_rustc
def test_closures_copy_what_they_capture(tmp_path: Path):
    src = ("n = 10\nadd = fn(x: int) => x + n\nn = 99\nxs = [1, 2]\nf = fn(i: int) => xs[i]\nxs[0] = 7\n"
           "print(add(1), f(0), n, xs)\n")
    assert run(src, tmp_path) == "11 1 99 [7, 2]\n"


@needs_rustc
def test_functions_in_structs_lists_and_dicts(tmp_path: Path):
    src = '''\
fn double(x: int) -> int:
    return x * 2

struct Route:
    name: str
    handler: fn(int) -> int

    fn call(self, x: int) -> int:
        return self.handler(x) + len(self.name)

routes = {"a": Route("a", double), "b": Route("bb", fn(x: int) => x + 1)}
copy = routes
routes["a"].name = "zzz"
print(routes["a"].call(5), copy["a"].call(5), routes["b"].call(5))
hs: dict[str, fn(int) -> int] = {"d": double}
print(hs["d"](4))
opt: (fn(int) -> int)? = some(double)
if opt is some:
    print(opt(8))
'''
    assert run(src, tmp_path) == "13 11 8\n8\n16\n"


@needs_rustc
def test_borrowed_parameters_work_inside_wrappers_and_closures(tmp_path: Path):
    src = '''\
fn size(s: str) -> int:
    return len(s)

fn apply(f: fn(str) -> int, s: str) -> int:
    return f(s) + f(s)

fn keep(s: str) -> fn() -> str:
    return fn() => s

words = ["ab", "cde"]
print(words.map(size), apply(size, "xyz"), keep("hi")(), apply(fn(s) => len(s) * 2, "ab"))
'''
    assert run(src, tmp_path) == '[2, 3] 6 hi 8\n'


@needs_rustc
def test_structs_holding_functions_still_clone_and_print_other_fields(tmp_path: Path):
    src = '''\
struct S:
    n: int
    g: fn(int) -> int

s = S(1, fn(x: int) => x + 1)
t = s
t.n = 5
print(s.n, t.n, s.g(1), t.g(1))
r: Result[fn(int) -> int, str] = ok(fn(x: int) => x)
print(r.unwrap()(3))
'''
    assert run(src, tmp_path) == "1 5 2 2\n3\n"


@needs_rustc
def test_unit_functions_and_nested_closures(tmp_path: Path):
    src = '''\
fn each(xs: list[int], f: fn(int)):
    for x in xs:
        f(x)

fn adder(a: int) -> fn(int) -> fn(int) -> int:
    return fn(b: int) => fn(c: int) => a * 100 + b * 10 + c

each([1, 2], fn(x) => print("item", x))
print(adder(1)(2)(3))
fn run() -> int:
    total = 0
    fn sq(n: int) -> int:
        k = 0
        k += n * n
        return k
    for i in range(4):
        total += sq(i)
    return total
print(run())
'''
    assert run(src, tmp_path) == "item 1\nitem 2\n123\n14\n"


def test_list_type_of_map_result():
    p = checked("ys = [1].map(fn(x) => [x])\n")
    assert p.body[0].value.ty == ListT(ListT(INT))


# ---- across modules ---------------------------------------------------------------------------------------------

@needs_rustc
def test_function_values_across_modules(tmp_path: Path, capfd):
    from zylo.interface.cli import main  # noqa: PLC0415
    lib = ("fn inc(x: int) -> int:\n    return x + 1\n\n"
           "fn twice(f: fn(int) -> int, x: int) -> int:\n    return f(f(x))\n\n"
           "fn both(x: int) -> int:\n    return twice(inc, x)\n\n"
           "fn shadow(inc: fn(int) -> int) -> int:\n    return inc(10)\n\n"
           "struct Op:\n    run: fn(int) -> int\n")
    main_src = ("import lib\nfrom lib import inc as plus_one, twice\n"
                "print(twice(plus_one, 1), lib.twice(lib.inc, 5), lib.both(0), lib.shadow(fn(x: int) => x * 3))\n"
                "op = lib.Op(plus_one)\nprint(op.run(41))\n")
    (tmp_path / "lib.zy").write_text(lib)
    entry = tmp_path / "main.zy"
    entry.write_text(main_src)
    assert main(["run", str(entry)]) == 0
    assert capfd.readouterr().out == "3 7 2 30\n42\n"


def test_function_values_are_lent_like_other_read_only_parameters():
    p = checked("fn f(g: fn(int) -> int, h: fn(int) -> int) -> int:\n    h = g\n    return g(1) + h(2)\n")
    by_ref = {q.name: q.info.by_ref for q in p.body[0].params}
    assert by_ref == {"g": True, "h": False}


def test_calling_an_expression_takes_positional_arguments_only():
    with pytest.raises(ZyloError, match="positional arguments only"):
        parse("hs[0](x=1)\n")


@needs_rustc
def test_enums_holding_functions_work_with_methods_and_match(tmp_path: Path):
    src = '''\
enum Action:
    Run(fn(int) -> int)
    Stop

    fn apply(self, x: int) -> int:
        match self:
            Action.Run(f):
                return f(x)
            Action.Stop:
                return x

acts = [Action.Run(fn(x: int) => x + 1), Action.Stop]
print(acts[0].apply(1), acts[1].apply(1))
'''
    assert run(src, tmp_path) == "2 1\n"
