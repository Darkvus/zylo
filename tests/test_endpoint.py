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
    fails(tmp_path, "fn f() -> int:\n    return 1\ng = fn() => endpoint(f)\nh = endpoint(f, f)\n", "takes 1 argument", 4)


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
