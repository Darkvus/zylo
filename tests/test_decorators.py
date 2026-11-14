"""Decorators (v0.32): `@target.method(args)` above a top-level `fn` is sugar for a call after it."""
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application.compile import load_program
from zylo.domain import ast
from zylo.domain.errors import BackendError, ZyloError
from zylo.semantics.checker import check
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse, parse_collect

needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
PROGRAMS = Path(__file__).parent / "programs"

HEAD = "import std.web as web\n\n"
REG = "fn reg(path: str, e: web.Endpoint, status: int = 200):\n    pass\n\n"
FN = "fn f(n: int) -> int:\n    return n\n"


def calls(src: str) -> list[ast.ExprStmt]:
    return [s for s in parse(src).body if isinstance(s, ast.ExprStmt)]


def test_a_decorator_expands_after_the_function():
    prog = parse("@app.get(\"/x\")\nfn f():\n    pass\n")
    fn, stmt = prog.body
    assert isinstance(fn, ast.FnDef) and fn.name == "f"
    assert isinstance(stmt, ast.ExprStmt) and stmt.sugar
    call = stmt.expr
    assert isinstance(call, ast.MethodCall) and call.name == "get"
    assert len(call.args) == 2
    assert isinstance(call.args[1], ast.Call) and call.args[1].func == "endpoint"
    assert call.args[1].args[0].id == "f"


def test_the_endpoint_goes_after_the_last_positional_argument_and_before_named_ones():
    (stmt,) = calls("@app.get(\"/x\", summary=\"a\")\nfn f():\n    pass\n")
    assert len(stmt.expr.args) == 2 and isinstance(stmt.expr.args[1], ast.Call)
    assert [n.name for n in stmt.expr.named] == ["summary"]


def test_a_decorator_without_arguments_gets_only_the_endpoint():
    (stmt,) = calls("@app.get()\nfn f():\n    pass\n")
    assert len(stmt.expr.args) == 1


def test_a_plain_name_decorator_is_a_call_of_that_function():
    (stmt,) = calls("@reg(\"/x\")\nfn f():\n    pass\n")
    assert isinstance(stmt.expr, ast.Call) and stmt.expr.func == "reg" and len(stmt.expr.args) == 2


def test_several_decorators_expand_top_to_bottom():
    stmts = calls("@a.one(1)\n@b.two(2)\nfn f():\n    pass\n")
    assert [s.expr.name for s in stmts] == ["one", "two"]


def test_the_decorators_stay_on_the_function():
    fn = parse("@a.one(1)\n@b.two(2)\nfn f():\n    pass\n").body[0]
    assert [d.name for d in fn.decorators] == ["one", "two"]
    assert len(fn.decorators[0].args) == 1  # as written, without endpoint


def test_the_checker_sees_plain_code(tmp_path):
    (tmp_path / "main.zy").write_text(HEAD + REG + "@reg(\"/x\", status=201)\n" + FN)
    check(load_program(tmp_path / "main.zy"))


# ---- formatter -----------------------------------------------------------------------------------------------------

def test_the_formatter_keeps_decorators():
    src = "x = 1\n@app.get( \"/x\",status = 201 )\n@app.post(\"/y\")\nfn f():\n    pass\ny = 2\n"
    out = format_source(src)
    assert out == "x = 1\n\n@app.get(\"/x\", status=201)\n@app.post(\"/y\")\nfn f():\n    pass\n\ny = 2\n"
    assert format_source(out) == out


def test_the_formatter_keeps_comments_between_decorators():
    src = "# routes\n@app.get(\"/x\")\n# second\n@app.post(\"/y\")\nfn f():\n    pass\n"
    assert format_source(src) == src


def test_the_formatter_wraps_a_long_decorator():
    src = "@app.get(\"/a/very/long/path/that/goes/on\", summary=\"a long summary text\", description=\"more text here to push it\")\nfn f():\n    pass\n"
    out = format_source(src)
    assert out.count("@app.get(") == 1 and "\n)\nfn f" in out
    assert format_source(out) == out


# ---- errors ------------------------------------------------------------------------------------------------------

def fails(src: str, match: str, line: int | None = None, tmp_path: Path | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        if tmp_path is None:
            check(parse(src))
        else:
            (tmp_path / "main.zy").write_text(src)
            check(load_program(tmp_path / "main.zy"))
    if line is not None:
        assert e.value.line == line
    return e.value


def test_a_decorator_must_be_followed_by_a_function():
    fails("@app.get(\"/x\")\nx = 1\n", "must be followed by a function", 1)
    fails("x = 0\n@app.get(\"/x\")\n\n@app.post(\"/y\")\nstruct S:\n    a: int\n", "must be followed by a function", 2)


def test_a_decorator_at_the_end_of_the_file():
    fails("@app.get(\"/x\")\n", "must be followed by a function", 1)


def test_a_decorator_must_be_a_call():
    fails("@app.get\nfn f():\n    pass\n", "a decorator is a call", 1)
    fails("@route\nfn f():\n    pass\n", "a decorator is a call", 1)


def test_decorators_are_not_allowed_on_methods():
    fails("struct S:\n    a: int\n    @app.get(\"/x\")\n    fn m(self):\n        pass\n", "not on methods", 3)


def test_decorators_are_not_allowed_on_nested_functions():
    fails("fn outer():\n    @app.get(\"/x\")\n    fn inner():\n        pass\n", "only allowed on top-level", 2)


def test_decorators_are_not_allowed_on_extern_functions():
    fails("@app.get(\"/x\")\nextern fn f() = \"x\"\n", "must be followed by a function", 1)


def test_an_undefined_target_points_at_the_decorator_line(tmp_path):
    e = fails(HEAD + FN + "\n@nope.get(\"/x\")\n" + FN.replace("f(", "g("), "nope", 6, tmp_path)
    assert e.line == 6


def test_a_function_that_cannot_be_an_endpoint_points_at_the_decorator(tmp_path):
    src = HEAD + REG + "@reg(\"/x\")\nfn f(x: set[int]) -> int:\n    return 1\n"
    e = fails(src, "cannot be read from a request", tmp_path=tmp_path)
    assert e.line == 7


def test_collecting_mode_reports_a_bad_decorator_and_goes_on():
    _prog, errors = parse_collect("@app.get(\"/x\")\nx = 1\nfn ok():\n    pass\n")
    assert len(errors) == 1


# ---- end to end ----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def build(tmp_path_factory):
    """Build and run a program; skips when Cargo cannot fetch the crates (offline without a cache)."""
    cache = tmp_path_factory.mktemp("cache")
    mp = pytest.MonkeyPatch()
    mp.setenv("ZYLO_CACHE_DIR", str(cache))
    out_dir = tmp_path_factory.mktemp("bin")

    def do(src: Path) -> str:
        rs, crates, manifest = pipeline.compile_file(src)
        exe = out_dir / "prog"
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
    assert build(PROGRAMS / "decorators.zy") == (PROGRAMS / "decorators.expected").read_text()


def test_the_example_program_type_checks():
    check(load_program(PROGRAMS / "decorators.zy"))


# ---- editor --------------------------------------------------------------------------------------------------------

LSP_SRC = (HEAD + REG + "@reg(\"/x\")\n@reg(\"/y\", status=201)\nfn f(n: int) -> int:\n    \"Doc.\"\n    return n\n")


def test_symbols_list_a_decorated_function_once():
    from zylo.application.editor import document_symbols
    assert [(s.name, s.kind) for s in document_symbols(LSP_SRC)] == [("reg", "function"), ("f", "function")]


def test_hover_works_on_a_decorated_function_and_its_decorator(tmp_path):
    from zylo.application.editor import hover, open_project
    path = tmp_path / "main.zy"
    path.write_text(LSP_SRC)
    project = open_project(path)
    lines = LSP_SRC.split("\n")
    fn_line = next(i for i, ln in enumerate(lines) if ln.startswith("fn f")) + 1
    assert "fn f(n: int) -> int" in hover(project, path, fn_line, 4).markdown
    dec_line = next(i for i, ln in enumerate(lines) if ln.startswith("@reg")) + 1
    assert "reg" in hover(project, path, dec_line, 3).markdown
