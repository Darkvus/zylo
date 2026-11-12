"""Re-exports: `pub from a import x` makes `x` part of a module's public surface."""
import re
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application.compile import load_program
from zylo.domain.errors import ZyloError
from zylo.semantics.checker import check
from zylo.syntax.formatter import format_source

MODEL = '''\
struct Point:
    x: int

enum Color:
    Red
    Blue

fn make(x: int) -> Point:
    return Point(x)

fn _hidden() -> int:
    return 1
'''


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path / "main.zy"


def rust(tmp_path: Path, files: dict[str, str]) -> str:
    return pipeline.transpile(project(tmp_path, files))


def fails(tmp_path: Path, files: dict[str, str], match: str) -> None:
    with pytest.raises(ZyloError, match=match):
        check(load_program(project(tmp_path, files)))


MAIN = "from lib import Point, Color, make\np = make(1)\nc = Color.Red\nprint(p.x)\n"


def test_reexported_names_import_from_the_package_root(tmp_path):
    rust(tmp_path, {"model.zy": MODEL, "lib.zy": "pub from model import Point, Color, make\n", "main.zy": MAIN})


def test_a_reexported_type_keeps_one_identity(tmp_path):
    out = rust(tmp_path, {
        "model.zy": MODEL,
        "lib.zy": "pub from model import Point, make\n",
        "other.zy": "from model import Point\nfn origin() -> Point:\n    return Point(0)\n",
        "main.zy": "from lib import Point, make\nimport other\nxs = [make(1), other.origin()]\nprint(len(xs))\n",
    })
    assert len(re.findall(r"struct \w*Point\b", out)) == 1


def test_chains_of_reexports(tmp_path):
    rust(tmp_path, {
        "model.zy": MODEL,
        "mid.zy": "pub from model import Point, Color, make\n",
        "lib.zy": "pub from mid import Point, Color, make\n",
        "main.zy": MAIN,
    })


def test_reexport_with_alias_and_module_access(tmp_path):
    out = rust(tmp_path, {
        "model.zy": MODEL,
        "lib.zy": "pub from model import make as new_point, Point\n",
        "main.zy": "import lib\np: lib.Point = lib.new_point(2)\nprint(p.x)\n",
    })
    assert "fn " in out


def test_plain_import_is_not_a_reexport(tmp_path):
    fails(tmp_path, {"model.zy": MODEL, "lib.zy": "from model import make\n", "main.zy": "from lib import make\n"},
          "has no function, struct or enum 'make'")


def test_unknown_name(tmp_path):
    fails(tmp_path, {"model.zy": MODEL, "lib.zy": "pub from model import nope\n", "main.zy": "from lib import nope\n"},
          "has no function, struct or enum 'nope'")


def test_private_name_cannot_be_reexported(tmp_path):
    fails(tmp_path, {"model.zy": MODEL, "lib.zy": "pub from model import _hidden\n", "main.zy": "import lib\n"},
          "private")
    fails(tmp_path, {"model.zy": MODEL, "lib.zy": "pub from model import make as _m\n", "main.zy": "import lib\n"},
          "cannot re-export '_m'")


def test_name_conflicts(tmp_path):
    fails(tmp_path, {"model.zy": MODEL, "lib.zy": "pub from model import make\nfn make() -> int:\n    return 1\n",
                     "main.zy": "import lib\n"}, "already defined")
    fails(tmp_path, {"model.zy": MODEL, "other.zy": "fn make() -> int:\n    return 2\n",
                     "lib.zy": "pub from model import make\npub from other import make\n", "main.zy": "import lib\n"},
          "already defined")


def test_reexport_cycle(tmp_path):
    fails(tmp_path, {"a.zy": "pub from b import f\n", "b.zy": "pub from a import f\n", "main.zy": "import a\n"},
          "import cycle")


def test_pub_only_applies_to_from_imports(tmp_path):
    with pytest.raises(ZyloError, match="expected 'from' after 'pub'"):
        pipeline.transpile(project(tmp_path, {"main.zy": "pub fn f():\n    print(1)\n"}))


def test_entry_file_cannot_reexport(tmp_path):
    fails(tmp_path, {"model.zy": MODEL, "main.zy": "pub from model import make\n"}, "entry file")


def test_formatter_keeps_pub():
    src = "pub from model import Point as P, make\n"
    assert format_source(src) == src
