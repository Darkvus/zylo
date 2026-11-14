"""The `endpoint(f)` built-in (v0.31): checker errors, the generated code across modules, and the data model.

The end-to-end behaviour (handlers called with fake requests, 422/400/404 bodies, operations and schemas) is
`tests/programs/endpoint.zy`, compared with `endpoint.expected`. `std.web` builds on `std.http`, so these programs
are built with Cargo (like `test_http.py`) and the tests skip themselves when the crates cannot be fetched.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application.compile import load_program
from zylo.domain.errors import BackendError, ZyloError
from zylo.semantics.checker import check, check_all
from zylo.syntax.parser import parse

needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
PROGRAMS = Path(__file__).parent / "programs"

HEAD = "import std.web as web\nfrom std.http import Request, Response\n\n"


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path / "main.zy"


def fails(tmp_path: Path, src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        check(load_program(project(tmp_path, {"main.zy": HEAD + src})))
    if line is not None:
        assert e.value.line == line + HEAD.count("\n")
    return e.value


def test_a_string_cannot_be_compiled_without_std_web():
    with pytest.raises(ZyloError, match="needs the module std.web"):
        check(parse("fn f() -> int:\n    return 1\ne = endpoint(f)\n"))


@pytest.mark.parametrize("ty", ["tuple[int, int]", "set[int]", "Cell[int]", "fn() -> int", "list[int]?", "Result[int, str]"])
def test_a_parameter_type_that_cannot_come_from_a_request(tmp_path, ty):
    fails(tmp_path, f"fn f(x: {ty}) -> int:\n    return 1\ne = endpoint(f)\n", "cannot be read from a request")


def test_the_error_names_the_parameter_and_the_function(tmp_path):
    e = fails(tmp_path, "fn f(a: int, pair: tuple[int, int]) -> int:\n    return a\ne = endpoint(f)\n", "endpoint\\(f\\): parameter 'pair'")
    assert "tuple[int, int]" in str(e)


def test_two_bodies_are_rejected(tmp_path):
    src = "struct A:\n    x: int\nfn f(a: A, b: A) -> int:\n    return 1\ne = endpoint(f)\n"
    fails(tmp_path, src, "second JSON body", 3)


def test_a_body_must_have_a_json_form(tmp_path):
    src = "struct A:\n    cb: fn() -> int\nfn f(a: A) -> int:\n    return 1\ne = endpoint(f)\n"
    fails(tmp_path, src, "endpoint cannot handle parameter 'a': A.cb")


def test_the_return_type_must_have_a_json_form(tmp_path):
    fails(tmp_path, "fn f() -> tuple[int, fn() -> int]:\n    return (1, fn() => 1)\ne = endpoint(f)\n", "endpoint cannot handle the response model")


def test_the_error_of_a_result_must_be_http_error(tmp_path):
    fails(tmp_path, "fn f() -> Result[int, str]:\n    return ok(1)\ne = endpoint(f)\n", "must be HttpError")


def test_the_argument_must_be_a_top_level_function(tmp_path):
    fails(tmp_path, "e = endpoint(fn(x: int) => x)\n", "name of a top-level function", 1)
    fails(tmp_path, "e = endpoint(3)\n", "name of a top-level function", 1)
    fails(tmp_path, "fn f() -> int:\n    return 1\ng = fn() => endpoint(f)\nh = endpoint(f, f)\n", "endpoint takes the name of a function", 4)


def test_generic_and_extern_functions_are_rejected(tmp_path):
    fails(tmp_path, "fn f[T](x: T) -> T:\n    return x\ne = endpoint(f)\n", "generic function")
    fails(tmp_path, 'extern fn f(x: int) -> int = "{0}"\ne = endpoint(f)\n', "extern fn")


def test_the_name_is_reserved(tmp_path):
    fails(tmp_path, "fn endpoint(x: int) -> int:\n    return x\n", "built-in function", 1)


def test_errors_are_reported_once_and_the_checker_is_repeatable(tmp_path):
    p = project(tmp_path, {"main.zy": HEAD + "fn f(a: set[int]) -> int:\n    return 1\ne = endpoint(f)\nk = endpoint(f)\n"})
    errors = check_all(load_program(p))
    assert len(errors) == 2 and all("parameter 'a'" in str(e) for e in errors)


def test_the_expansion_is_a_call_of_a_generated_function(tmp_path):
    prog = load_program(project(tmp_path, {"main.zy": HEAD + "fn f(x: int) -> int:\n    return x\ne = endpoint(f)\nd = endpoint(f)\n"}))
    check(prog)
    names = [getattr(s, "name", "") for s in prog.body]
    assert sum(n.startswith("_zw_ep_f_") for n in names) == 1  # one generated endpoint per function
    assert sum(n.startswith("_zw_h_f_") for n in names) == 1


# ---- across modules ----------------------------------------------------------------------------------------------

LIB = '''\
import std.web as web

struct Item:
    "An item of the library."
    n: int

fn get_item(n: int) -> Result[Item, web.HttpError]:
    "Read one item."
    if n < 0:
        return err(web.HttpError(400, "negative"))
    return ok(Item(n))
'''

OTHER = '''\
struct Item:
    id: int
'''

MAIN = '''\
import std.web as web
from std.http import Request
import lib
from lib import get_item
from other import Item as Other

fn make(body: Other) -> Other:
    return body

fn call(e: web.Endpoint, n: str) -> str:
    q: dict[str, str] = {}
    h: dict[str, str] = {}
    p: dict[str, str] = {}
    p["n"] = n
    r = e.handler(Request("GET", "/", q, h, "", p))
    return str(r.status) + " " + r.body

print(call(endpoint(get_item), "5"))
print(call(endpoint(lib.get_item), "-1"))
print(call(endpoint(get_item), "x"))
print(endpoint(get_item).operation.summary)
print(len(endpoint(get_item).schemas), len(endpoint(make).schemas))
'''


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    """Build and run a program; skips the module when Cargo cannot fetch the crates (offline, no cache)."""
    cache = tmp_path_factory.mktemp("cache")
    mp = pytest.MonkeyPatch()
    mp.setenv("ZYLO_CACHE_DIR", str(cache))
    out_dir = tmp_path_factory.mktemp("bin")
    count = 0

    def do(src: Path) -> str:
        nonlocal count
        count += 1
        rs, crates, manifest = pipeline.compile_file(src)
        exe = out_dir / f"prog{count}"
        try:
            pipeline.build_executable(rs, crates, manifest, exe, True, False)
        except BackendError as e:
            if any(w in str(e) for w in ("download", "network", "resolve", "registry", "index", "offline", "spurious")):
                pytest.skip("cargo cannot fetch the crates here (offline without a cache)")
            raise
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout

    yield do
    mp.undo()


@needs_cargo
def test_the_example_program(build):
    assert build(PROGRAMS / "endpoint.zy") == (PROGRAMS / "endpoint.expected").read_text()


@needs_cargo
def test_endpoints_of_functions_from_other_modules(build, tmp_path):
    main = project(tmp_path, {"main.zy": MAIN, "lib.zy": LIB, "other.zy": OTHER})
    assert build(main).splitlines() == [
        '200 {"n":5}',
        '400 {"detail":"negative"}',
        '422 {"detail":[{"loc":["path","n"],"msg":"Input should be a valid integer","type":"int_parsing"}]}',
        "Read one item.",
        "1 1",
    ]


@needs_cargo
def test_depends_status_operation_id_and_error_statuses(build):
    """Dependencies run once per request; `err` short-circuits; `status=` and the listed error statuses reach the spec."""
    assert build(PROGRAMS / "endpoint_depends.zy") == (PROGRAMS / "endpoint_depends.expected").read_text()


@needs_cargo
def test_dependencies_with_request_parameters(build):
    """Pagination and auth dependencies: merged parameters, one 422 listing everything, body in a dependency."""
    assert build(PROGRAMS / "endpoint_depends_params.zy") == (PROGRAMS / "endpoint_depends_params.expected").read_text()


# ---- depends(g) ---------------------------------------------------------------------------------------------------

DEP = "fn dep() -> int:\n    return 1\n"


def test_depends_checks(tmp_path):
    fails(tmp_path, DEP + "fn f(x: str = depends(dep)) -> int:\n    return 1\n", "parameter 'x' is str but dep returns int")
    fails(tmp_path, DEP + "fn f(x: int = depends(nope)) -> int:\n    return 1\n", "not a top-level function")
    fails(tmp_path, "fn f[T](x: T) -> T:\n    return x\nfn g(x: int = depends(f)) -> int:\n    return 1\n", "generic function")
    fails(tmp_path, "fn dep() -> Result[int, web.HttpError]:\n    return ok(1)\nfn f(x: str = depends(dep)) -> int:\n    return 1\n",
          "declare it Result\\[int, std.web.HttpError\\] or int")
    fails(tmp_path, "fn dep():\n    pass\nfn f(x: int = depends(dep)) -> int:\n    return 1\n", "returns nothing")
    fails(tmp_path, "x = depends(3)\n", "can only be the default of a parameter")
    fails(tmp_path, DEP + "fn f(x: int = depends(dep)) -> int:\n    return 1\nf()\n", "injected by `endpoint`")


def test_a_dependency_parameter_must_be_readable_from_a_request(tmp_path):
    src = "fn dep(pair: tuple[int, int]) -> int:\n    return 1\nfn f(x: int = depends(dep)) -> int:\n    return x\ne = endpoint(f)\n"
    e = fails(tmp_path, src, "parameter 'pair' of dep has the type tuple\\[int, int\\], which cannot be read from a request")
    assert "supported" in str(e)


def test_parameters_with_one_name_must_have_one_type(tmp_path):
    src = "fn dep(n: str) -> str:\n    return n\nfn f(n: int, x: str = depends(dep)) -> int:\n    return n\ne = endpoint(f)\n"
    fails(tmp_path, src, "parameter 'n' is int in f but str in dep")


def test_a_dependency_may_not_add_a_second_body(tmp_path):
    src = ("struct A:\n    x: int\nfn dep(a: A) -> int:\n    return a.x\n"
           "fn f(b: A, x: int = depends(dep)) -> int:\n    return x\ne = endpoint(f)\n")
    fails(tmp_path, src, "parameter 'a' of dep is a JSON body, and f already reads one")
    src = ("struct A:\n    x: int\nfn d1(a: A) -> int:\n    return a.x\nfn d2(a: A) -> int:\n    return a.x\n"
           "fn f(x: int = depends(d1), y: int = depends(d2)) -> int:\n    return x\ne = endpoint(f)\n")
    fails(tmp_path, src, "parameter 'a' of d2 is a JSON body, and d1 already reads one")


def test_an_enum_with_data_is_still_a_body_and_a_plain_enum_is_a_scalar(tmp_path):
    ok = "enum E:\n    A\n    B\nfn f(e: E, k: E? = none) -> int:\n    return 1\ne = endpoint(f)\n"
    check(load_program(project(tmp_path, {"main.zy": HEAD + ok})))
    src = "enum E:\n    A\n    B(int)\nfn f(e: E?) -> int:\n    return 1\ne = endpoint(f)\n"
    fails(tmp_path, src, "cannot be read from a request")


def test_a_dependency_cycle_is_an_error(tmp_path):
    src = ("fn a(x: int = depends(b)) -> int:\n    return x\nfn b(y: int = depends(a)) -> int:\n    return y\n"
           "fn f(z: int = depends(a)) -> int:\n    return z\ne = endpoint(f)\n")
    fails(tmp_path, src, "dependency cycle: a -> b -> a")


def test_status_must_be_a_success_literal(tmp_path):
    fails(tmp_path, DEP + "e = endpoint(dep, status=404)\n", "from 200 to 299")
    fails(tmp_path, DEP + "e = endpoint(dep, code=201)\n", "optionally status")


def test_a_decorator_success_status_is_also_the_endpoints():
    prog = parse("@app.post('/', status=201)\nfn f() -> int:\n    return 1\n")
    call = prog.body[1].expr
    assert call.named[0].name == "status" and call.args[1].named[0].value.value == 201
    other = parse("@app.post('/', status=404)\nfn f() -> int:\n    return 1\n").body[1].expr
    assert other.args[1].named == []  # only a success status literal is copied


# ---- query(...) and path(...) (v0.35) ----------------------------------------------------------------------------

@needs_cargo
def test_query_and_path_constraints(build):
    """Defaults, constraints with FastAPI's 422 types and ctx, descriptions and the schemas of the parameters."""
    assert build(PROGRAMS / "endpoint_query.zy") == (PROGRAMS / "endpoint_query.expected").read_text()


def test_query_marker_forms_type_check(tmp_path):
    src = ("fn f(a: int = query(10, ge=1, le=100, description=\"x\"), b: int = query(ge=-5), c: float? = query(none, gt=-0.5),"
           " d: str = query(\"x\", min_length=1, max_length=3)) -> int:\n    return a\ne = endpoint(f)\n")
    # a required parameter after a defaulted one is the usual error, so put the required one first
    src = src.replace("a: int = query(10, ge=1, le=100, description=\"x\"), b: int = query(ge=-5)", "b: int = query(ge=-5), a: int = query(10, ge=1, le=100, description=\"x\")")
    check(load_program(project(tmp_path, {"main.zy": HEAD + src})))


def test_a_query_default_can_be_used_when_calling_the_function(tmp_path):
    src = "fn f(n: int = query(7, ge=1)) -> int:\n    return n\nx = f()\n"
    check(load_program(project(tmp_path, {"main.zy": HEAD + src})))
    fails(tmp_path, "fn f(n: int = query(ge=1)) -> int:\n    return n\nx = f()\n", "missing argument 'n'")


@pytest.mark.parametrize("params, message", [
    ("a: str = query(ge=1)", "ge constrains an int or float, but parameter 'a' is str"),
    ("a: bool = query(le=1)", "le constrains an int or float"),
    ("a: int = query(min_length=1)", "min_length constrains a str, but parameter 'a' is int"),
    ("a: list[int] = query(max_length=2)", "max_length constrains a str"),
    ("a: int = query(ge=1.5)", "ge of the int parameter 'a' must be a whole number"),
    ("a: int = query(ge=lo)", "ge must be a number literal"),
    ("a: int = query(ge=1 + 1)", "ge must be a number literal"),
    ("a: str = query(min_length=-1)", "min_length must be a whole number literal, 0 or more"),
    ("a: str = query(description=name)", "description must be a string literal"),
    ("a: int = query(ge=1, ge=2)", "'ge' is given more than once"),
    ("a: int = query(1, 2)", "only positional argument"),
    ("a: int = query(eq=1)", "unknown argument 'eq'"),
    ("a: int = path(5)", "a path parameter is always required"),
    ("a: str = query(5)", "the default of parameter 'a' is int but it is declared str"),
])
def test_query_marker_errors(tmp_path, params, message):
    fails(tmp_path, f"lo = 1\nname = \"n\"\nfn f({params}) -> int:\n    return 1\n", message)


def test_a_marker_is_not_an_ordinary_expression_nor_for_non_scalars(tmp_path):
    fails(tmp_path, "x = query(1)\n", "can only be the default of a parameter")
    fails(tmp_path, "struct A:\n    n: int = query(1)\n", "goes in the default of a function parameter")
    fails(tmp_path, "struct A:\n    n: int\n    fn m(self, k: int = query(1)):\n        pass\n", "not on methods")
    fails(tmp_path, "struct A:\n    n: int\nfn f(a: A = query()) -> int:\n    return 1\ne = endpoint(f)\n", "describes an int, float, bool, str or enum")


def test_constraints_conflict_between_an_endpoint_and_its_dependency(tmp_path):
    dep = "fn dep(n: int = query(1, ge=1, le=10)) -> int:\n    return n\n"
    fails(tmp_path, dep + "fn f(n: int = query(1, ge=2), x: int = depends(dep)) -> int:\n    return n\ne = endpoint(f)\n",
          "parameter 'n' has ge=1 in dep but ge=2 in f|parameter 'n' has ge=2 in f but ge=1 in dep")
    # the same value, or different constraints, merge
    ok = dep + "fn f(n: int = query(1, ge=1, lt=20), x: int = depends(dep)) -> int:\n    return n\ne = endpoint(f)\n"
    check(load_program(project(tmp_path, {"main.zy": HEAD + ok})))


def test_query_and_path_are_ordinary_names_for_user_functions(tmp_path):
    src = "fn query(a: int) -> int:\n    return a\nfn path(a: int) -> int:\n    return a + query(1)\nx = path(2)\n"
    check(load_program(project(tmp_path, {"main.zy": src})))
