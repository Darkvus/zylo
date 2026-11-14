"""Module-level `const`: shared state readable from every function (v0.33)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_lsp_navigation import Workspace
from zylo.application import compile as pipeline
from zylo.application.compile import transpile_source
from zylo.application.editor import document_symbols
from zylo.application.format import format_text
from zylo.domain.errors import ZyloError
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

DOC = '''struct Pet:
    id: int

const limit = 3
const store: Cell[list[Pet]] = cell([])

fn count() -> int:
    return len(store.get()) + limit
'''


def fails(src: str, match: str) -> None:
    with pytest.raises(ZyloError, match=match):
        check(parse(src))


def run(src: str, tmp_path: Path) -> str:
    exe = tmp_path / "prog"
    compile_rust(transpile_source(src), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout


def run_project(tmp_path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    exe = tmp_path / "prog"
    compile_rust(pipeline.transpile(tmp_path / "main.zy"), exe)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout


def project_fails(tmp_path: Path, files: dict[str, str], match: str) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    with pytest.raises(ZyloError, match=match):
        pipeline.transpile(tmp_path / "main.zy")


# ---- language ----

def test_a_const_is_a_thread_local_and_reads_clone_it():
    rust = transpile_source(DOC)
    assert "thread_local!" in rust and "__ZG_store" in rust and "__ZG_limit.with(" in rust


@needs_rustc
def test_functions_share_state_through_a_cell(tmp_path):
    src = DOC + '''
fn add(p: Pet):
    xs = store.get()
    xs.append(p)
    store.set(xs)

add(Pet(1))
add(Pet(2))
print(count())
'''
    assert run(src, tmp_path) == "5\n"


@needs_rustc
def test_collections_structs_and_earlier_constants(tmp_path):
    src = '''struct P:
    x: int
    y: int = 7

enum Mode:
    Fast
    Slow(int)

const base = 10
const total = base * 2 + 1
const names = ["a", "b"]
const origin = P(1)
const mode = Mode.Slow(3)
const table = {"k": [1, 2]}
const maybe: int? = none

fn show() -> str:
    m = 0
    match mode:
        Mode.Slow(n):
            m = n
        Mode.Fast:
            pass
    return f"{total} {names[1]} {origin.y} {m} {table['k'][1]} {maybe is none}"

print(show())
'''
    assert run(src, tmp_path) == "21 b 7 3 2 true\n"


@needs_rustc
def test_a_parameter_hides_a_constant(tmp_path):
    src = "const x = 1\nfn f(x: int) -> int:\n    x = x + 1\n    return x\nprint(f(4))\nprint(x)\n"
    assert run(src, tmp_path) == "5\n1\n"


@needs_rustc
def test_a_closure_reads_a_constant(tmp_path):
    src = "const k = 3\nfn apply(xs: list[int]) -> list[int]:\n    return xs.map(fn(n) => n * k)\nprint(apply([1, 2]))\n"
    assert run(src, tmp_path) == "[3, 6]\n"


def test_cannot_reassign_a_constant():
    fails("const x = 1\nfn f():\n    x = 2\n", "cannot (assign to|modify) the constant 'x'")
    fails("const x = 1\nx = 2\n", "cannot (assign to|modify) the constant 'x'")


def test_cannot_modify_a_constant_in_place():
    fails("const xs = [1]\nfn f():\n    xs.append(2)\n", "cannot modify the constant 'xs'")
    fails("struct P:\n    a: int\nconst p = P(1)\nfn f():\n    p.a = 2\n", "cannot modify the constant 'p'")


def test_a_value_cannot_call_functions():
    fails("fn g() -> int:\n    return 1\nconst y = g()\n", "cannot call 'g'")
    fails("const y = [1].len()\n", "cannot call a method")
    fails("const y = f\"a\"\n", "cannot be the value")
    fails("const y = fn(n: int) => n\n", "cannot be the value")


def test_a_value_only_uses_constants_defined_above():
    fails("const a = b\nconst b = 1\n", "cannot use 'b'")
    fails("n = 1\nconst a = n\n", "cannot use 'n'")


def test_annotation_and_duplicates_and_nesting():
    fails("const a: str = 1\n", "is str but int")
    fails("const a = 1\nconst a = 2\n", "already defined")
    with pytest.raises(ZyloError, match="only allowed at the top level"):
        parse("if true:\n    const a = 1\n")
    fails("const a = []\n", "cannot infer")


# ---- modules ----

SHARED = '''struct Item:
    n: int

const counter = cell(0)
const base = 10
const _secret = 1

fn bump() -> int:
    counter.update(fn(x) => x + base)
    return counter.get()
'''


@needs_rustc
def test_constants_are_importable(tmp_path):
    main = '''import shared
from shared import counter, base as b

print(shared.bump())
print(shared.bump())
print(counter.get())
print(shared.base + b)
'''
    assert run_project(tmp_path, {"shared.zy": SHARED, "main.zy": main}) == "10\n20\n20\n20\n"


def test_private_and_reassigned_imported_constants(tmp_path):
    project_fails(tmp_path, {"shared.zy": SHARED, "main.zy": "from shared import _secret\n"}, "private")
    project_fails(tmp_path, {"shared.zy": SHARED, "main.zy": "import shared\nfn f():\n    shared.base = 2\n"}, "cannot modify the constant 'base'")
    project_fails(tmp_path, {"shared.zy": SHARED, "main.zy": "from shared import base\nfn f():\n    base = 2\n"},
                  "cannot assign to the constant 'base'")


def test_library_constants_do_not_clash_with_the_entry_names(tmp_path):
    main = "import shared\nbase = 5\nprint(base + shared.base)\n"
    assert "thread_local" in pipeline.transpile(_write(tmp_path, {"shared.zy": SHARED, "main.zy": main}))


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path / "main.zy"


# ---- tooling ----

def test_formatter_keeps_const():
    src = "const   a  =  1\nconst b: Cell[list[int]] = cell([])\n"
    assert format_text(src) == "const a = 1\nconst b: Cell[list[int]] = cell([])\n"
    assert format_text(format_text(src)) == format_text(src)


def test_symbols_list_a_const_as_a_constant():
    syms = {s.name: s.kind for s in document_symbols(DOC)}
    assert syms["limit"] == "constant" and syms["store"] == "constant"


def test_hover_and_definition_inside_a_function(tmp_path):
    ws = Workspace(tmp_path, {"doc.zy": DOC})
    assert "limit: int" in ws.hover("doc.zy", "limit", 1)
    assert "Cell[list[Pet]]" in ws.hover("doc.zy", "store", 1)
    assert ws.definition("doc.zy", "limit", 1) == [("doc.zy", 3, 6)]
