import shutil
from pathlib import Path

import pytest

from zylo import cli
from zylo.cli import load_program, main
from zylo.checker import check
from zylo.errors import ZyloError

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

GEOMETRY = '''\
struct Point:
    x: float
    y: float

    fn dist2(self) -> float:
        return self.x * self.x + self.y * self.y

    fn move(self, dx: float):
        self.x += dx

fn area(w: float, h: float) -> float:
    return w * h

fn _secret() -> int:
    return 42
'''


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path / "main.zy"


def compile_ok(tmp_path: Path, files: dict[str, str]) -> str:
    return cli.transpile(project(tmp_path, files))


def fails(tmp_path: Path, files: dict[str, str], match: str) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        check(load_program(project(tmp_path, files)))
    return e.value


def run(tmp_path: Path, files: dict[str, str], capfd) -> tuple[int, str]:
    entry = project(tmp_path, files)
    code = main(["run", str(entry)])
    return code, capfd.readouterr().out


# ---- parsing --------------------------------------------------------------------------------

def test_parse_import_forms():
    from zylo.parser import parse
    p = parse("import a.b.c\nimport a as x\nfrom a.b import f, g as h\n")
    assert (p.body[0].module, p.body[0].alias) == ("a.b.c", None)
    assert (p.body[1].module, p.body[1].alias) == ("a", "x")
    assert p.body[2].module == "a.b" and p.body[2].names == [("f", None), ("g", "h")]


def test_parse_qualified_type():
    from zylo.parser import parse
    t = parse("p: geometry.Point = q\n").body[0].annotation
    assert t.name == "geometry.Point"
    t = parse("xs: list[geometry.Point] = []\n").body[0].annotation
    assert t.args[0].name == "geometry.Point"


# ---- success cases ------------------------------------------------------------------------------

@needs_rustc
def test_import_module_and_call(tmp_path, capfd):
    code, out = run(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nprint(geometry.area(3.0, 4.0))\n"}, capfd)
    assert (code, out) == (0, "12.0\n")


@needs_rustc
def test_from_import_alias_and_struct(tmp_path, capfd):
    main_src = ("from geometry import area as get_area, Point\n"
                "p = Point(3.0, 4.0)\nprint(p.dist2())\np.move(1.0)\nprint(p)\nprint(get_area(2.0, 5.0))\n")
    code, out = run(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": main_src}, capfd)
    assert out == "25.0\nPoint(x=4.0, y=4.0)\n10.0\n" and code == 0


@needs_rustc
def test_qualified_struct_constructor_and_type(tmp_path, capfd):
    main_src = ("import geometry as g\n"
                "fn norm(p: g.Point) -> float:\n    return p.dist2()\n"
                "pts: list[g.Point] = [g.Point(3.0, 4.0)]\nprint(norm(pts[0]))\n")
    code, out = run(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": main_src}, capfd)
    assert (code, out) == (0, "25.0\n")


@needs_rustc
def test_nested_modules_and_transitive_imports(tmp_path, capfd):
    files = {
        "utils/text.zy": "from helper import twice\nfn shout(s: str) -> str:\n    return twice(s)\n",  # sibling import
        "utils/helper.zy": "fn twice(s: str) -> str:\n    return s + s\n",
        "main.zy": "import utils.text\nprint(text.shout(\"ab\"))\n",
    }
    code, out = run(tmp_path, files, capfd)
    assert (code, out) == (0, "abab\n")


@needs_rustc
def test_same_name_in_two_modules_does_not_clash(tmp_path, capfd):
    files = {
        "a.zy": "fn helper() -> int:\n    return 1\n",
        "b.zy": "fn helper() -> int:\n    return 2\n",
        "main.zy": "import a\nimport b\nfn helper() -> int:\n    return 3\nprint(a.helper() + b.helper() * 10 + helper() * 100)\n",
    }
    code, out = run(tmp_path, files, capfd)
    assert (code, out) == (0, "321\n")


@needs_rustc
def test_module_with_private_helpers_and_mutating_methods(tmp_path, capfd):
    lib = ("struct Counter:\n    n: int\n\n    fn bump(self):\n        self.n += _step()\n\n"
           "    fn twice(self):\n        self.bump()\n        self.bump()\n\n"
           "fn _step() -> int:\n    return 5\n")
    code, out = run(tmp_path, {"lib.zy": lib, "main.zy": "from lib import Counter\nc = Counter(0)\nc.twice()\nprint(c.n)\n"}, capfd)
    assert (code, out) == (0, "10\n")


@needs_rustc
def test_modules_share_option_and_result_types(tmp_path, capfd):
    lib = ("fn safe(a: int, b: int) -> Result[int, str]:\n    if b == 0:\n        return err(\"zero\")\n    return ok(a / b)\n")
    main_src = "import lib\nprint(lib.safe(6, 3))\nprint(lib.safe(1, 0))\n"
    code, out = run(tmp_path, {"lib.zy": lib, "main.zy": main_src}, capfd)
    assert out == "Ok(2)\nErr(\"zero\")\n"


def test_generated_rust_uses_valid_identifiers(tmp_path):
    rs = compile_ok(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nprint(geometry.area(1.0, 2.0))\n"})
    assert "fn geometry__area(" in rs and "·" not in rs


def test_struct_debug_keeps_original_name(tmp_path):
    rs = compile_ok(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "from geometry import Point\nprint(Point(1.0, 2.0))\n"})
    assert 'write!(f, "Point(' in rs and "struct geometry__Point" in rs


# ---- errors -------------------------------------------------------------------------------------------

def test_module_not_found(tmp_path):
    e = fails(tmp_path, {"main.zy": "import nothing\n"}, "module 'nothing' not found")
    assert e.line == 1 and e.file.endswith("main.zy")
    assert "no environment is active" in e.msg


def test_name_not_found_in_module(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nx = geometry.volume(1.0)\n"},
          "module 'geometry' has no function or struct 'volume'")
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "from geometry import volume\n"},
          "module 'geometry' has no function or struct 'volume'")


def test_private_names_are_not_importable(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nx = geometry._secret()\n"}, "'_secret' is private to module 'geometry'")
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "from geometry import _secret\n"}, "is private")


def test_import_cycle(tmp_path):
    e = fails(tmp_path, {"a.zy": "import b\nfn f() -> int:\n    return 1\n", "b.zy": "import a\nfn g() -> int:\n    return 2\n",
                         "main.zy": "import a\n"}, "import cycle: a -> b -> a")
    assert e.file is not None


def test_library_cannot_have_top_level_statements(tmp_path):
    e = fails(tmp_path, {"lib.zy": "fn f() -> int:\n    return 1\nprint(1)\n", "main.zy": "import lib\n"},
              "library modules can only contain")
    assert e.file.endswith("lib.zy") and e.line == 3


def test_module_used_as_a_value(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nx = geometry\n"}, "is a module, not a value")
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nx = geometry.PI\n"}, "has no values")


def test_variable_cannot_hide_an_imported_module(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\ngeometry = 1\n"}, "also the name of an imported module")


def test_name_clashes(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "from geometry import area\nfn area() -> int:\n    return 1\n"}, "already defined")
    fails(tmp_path, {"a.zy": "fn f() -> int:\n    return 1\n", "b.zy": "fn f() -> int:\n    return 2\n",
                     "main.zy": "from a import f\nfrom b import f\n"}, "already defined")


def test_library_cannot_define_reserved_names(tmp_path):
    fails(tmp_path, {"lib.zy": "fn print() -> int:\n    return 1\n", "main.zy": "import lib\n"}, "reserved name")


def test_wrong_kind_in_type_position(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "import geometry\nx: geometry.area = 1\n"}, "is a fn, not a struct")
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "x: nope.Point = 1\n"}, "unknown module 'nope'")


def test_imports_only_at_top_level(tmp_path):
    fails(tmp_path, {"geometry.zy": GEOMETRY, "main.zy": "fn f():\n    import geometry\n"}, "imports can only appear at the top level")


def test_type_error_inside_a_library_points_at_the_library(tmp_path):
    lib = "fn f() -> int:\n    return \"oops\"\n"
    e = fails(tmp_path, {"lib.zy": lib, "main.zy": "import lib\nprint(lib.f())\n"}, "returning str but the function declares int")
    assert e.file.endswith("lib.zy") and e.line == 2


def test_syntax_error_inside_a_library_points_at_the_library(tmp_path):
    e = fails(tmp_path, {"lib.zy": "fn f(:\n", "main.zy": "import lib\n"}, "expected")
    assert e.file.endswith("lib.zy") and e.line == 1


def test_strings_cannot_use_imports():
    with pytest.raises(ZyloError, match="imports need a source file"):
        cli.transpile_source("import x\n")


def test_cli_shows_the_library_snippet(tmp_path, capsys):
    entry = project(tmp_path, {"lib.zy": "fn f() -> int:\n    return \"oops\"\n", "main.zy": "import lib\nprint(lib.f())\n"})
    assert main(["check", str(entry)]) == 1
    err = capsys.readouterr().err
    assert "lib.zy:2:" in err and 'return "oops"' in err and "main.zy" not in err


def test_local_module_resolution_is_relative_to_the_importing_file(tmp_path):
    files = {"sub/inner.zy": "fn g() -> int:\n    return 1\n", "sub/outer.zy": "import inner\nfn f() -> int:\n    return inner.g()\n",
             "main.zy": "import sub.outer\nprint(outer.f())\n"}
    compile_ok(tmp_path, files)
