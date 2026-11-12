"""The bundled standard library: resolution of `std.*`, packaging of the `.zy` files, and the small modules."""
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application.compile import load_program
from zylo.domain.errors import ZyloError
from zylo.packaging.stdlib import stdlib_dir, stdlib_modules
from zylo.semantics.checker import check

ROOT = Path(__file__).parent.parent
needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path / "main.zy"


def test_the_modules_are_listed():
    assert {"std.json", "std.math", "std.text", "std.os"} <= set(stdlib_modules())


@pytest.mark.parametrize("module", stdlib_modules())
def test_every_stdlib_module_type_checks(tmp_path, module):
    check(load_program(write(tmp_path, {"main.zy": f"import {module}\n"})))


def test_import_forms(tmp_path):
    main = write(tmp_path, {"main.zy": (
        "import std.json\nimport std.math as m\nfrom std.json import Json, parse\n"
        "x = json.parse('[1]')\ny: Json = Json.Null\nprint(m.pi(), parse('1'), x, y)\n")})
    check(load_program(main))


def test_user_modules_cannot_shadow_std(tmp_path):
    main = write(tmp_path, {
        "main.zy": "import std.json as j\nprint(j.answer())\n",
        "std/json.zy": "fn answer() -> int:\n    return 42\n",
    })
    with pytest.raises(ZyloError, match="has no function, struct or enum 'answer'"):
        check(load_program(main))


def test_unknown_std_module(tmp_path):
    with pytest.raises(ZyloError, match="module 'std.nope' not found in the standard library"):
        load_program(write(tmp_path, {"main.zy": "import std.nope\n"}))


def test_a_package_can_use_std(tmp_path):
    """A local library importing `std.json` works, and `std` is allowed to use `extern fn` without any trust."""
    main = write(tmp_path, {
        "main.zy": "import lib\nprint(lib.size('[1,2,3]'))\n",
        "lib.zy": "import std.json as json\n\nfn size(t: str) -> int:\n    return len(json.as_list(json.parse(t).unwrap()).unwrap())\n",
    })
    check(load_program(main))


@needs_rustc
def test_std_runs(tmp_path):
    main = write(tmp_path, {"main.zy": "import std.json as j\nprint(j.stringify(j.parse('{\"a\": [1, true]}').unwrap()))\n"})
    exe = tmp_path / "prog"
    pipeline.build_executable(pipeline.transpile(main), {}, None, exe, True, False)
    assert subprocess.run([str(exe)], capture_output=True, text=True).stdout == '{"a":[1,true]}\n'


def test_the_wheel_ships_the_stdlib(tmp_path):
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "stdlib/*.zy" in pyproject["tool"]["setuptools"]["package-data"]["zylo"]
    if shutil.which("pip") is None:
        pytest.skip("pip not available")
    out = tmp_path / "wheel"
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(out), str(ROOT)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("cannot build a wheel here: " + r.stderr[-200:])
    names = zipfile.ZipFile(next(out.glob("*.whl"))).namelist()
    for module in stdlib_dir().glob("*.zy"):
        assert f"zylo/stdlib/{module.name}" in names
