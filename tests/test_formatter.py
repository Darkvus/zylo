"""The formatter: it must not change what a program means, must be idempotent, and must keep comments."""
import dataclasses
import re
from pathlib import Path

import pytest

from zylo.application.format import find_sources, format_paths
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.interface.cli import main
from zylo.syntax.formatter import _scan_comments, float_text, format_source, quote
from zylo.syntax.parser import parse

ROOT = Path(__file__).parent.parent
SKIP_FIELDS = ast._ANNOTATIONS | {"line", "col", "file", "ty"}


def dump(node):
    """The structure of a syntax tree, without positions or checker annotations."""
    if isinstance(node, ast.Node):
        return (type(node).__name__, tuple((f.name, dump(getattr(node, f.name))) for f in dataclasses.fields(node)
                                           if f.name not in SKIP_FIELDS))
    if isinstance(node, list):
        return [dump(x) for x in node]
    if isinstance(node, tuple):
        return tuple(dump(x) for x in node)
    return node


def doc_blocks():
    blocks = []
    for doc in ("README.md", "docs/guide.md", "docs/rust-interop.md"):
        text = (ROOT / doc).read_text()
        blocks += [(f"{doc}#{i}", code) for i, code in enumerate(re.findall(r"```zylo\n(.*?)```", text, re.S), 1)]
    return blocks


CORPUS = [(str(p.relative_to(ROOT)), p.read_text()) for pattern in ("examples/**/*.zy", "tests/programs/*.zy", "benchmarks/*.zy", "zylo/stdlib/*.zy")
          for p in sorted(ROOT.glob(pattern))] + doc_blocks()


# ---- the three guarantees, over every program in the repository ---------------------------------------

@pytest.mark.parametrize("name,src", CORPUS, ids=[n for n, _ in CORPUS])
def test_formatting_does_not_change_the_meaning(name, src):
    assert dump(parse(format_source(src))) == dump(parse(src))


@pytest.mark.parametrize("name,src", CORPUS, ids=[n for n, _ in CORPUS])
def test_formatting_twice_changes_nothing(name, src):
    once = format_source(src)
    assert format_source(once) == once


@pytest.mark.parametrize("name,src", CORPUS, ids=[n for n, _ in CORPUS])
def test_no_comment_is_lost(name, src):
    before = sorted(text for _, text, _ in _scan_comments(src))
    after = sorted(text for _, text, _ in _scan_comments(format_source(src)))
    assert before == after


def test_the_repository_programs_are_already_formatted():
    unformatted = [n for n, src in CORPUS if not n.startswith("README") and "docs/" not in n and format_source(src) != src]
    assert unformatted == [], "run `zylo fmt examples tests/programs benchmarks`"


# ---- the style -------------------------------------------------------------------------------------------

def test_spacing_and_parentheses():
    assert format_source("x=1+2*3\ny=(1+2)*3\nz = a-(b-c)\nw=(a-b)-c\n") == "x = 1 + 2 * 3\ny = (1 + 2) * 3\nz = a - (b - c)\nw = a - b - c\n"
    assert format_source("a = not x == y\nb = (not x) == y\nc = x and not y\nd = (x or y) and z\n") == \
        "a = not x == y\nb = (not x) == y\nc = x and not y\nd = (x or y) and z\n"
    assert format_source("x = -(a + b)\ny = - -a\nz = (a + b).c\n") == "x = -(a + b)\ny = --a\nz = (a + b).c\n"


def test_augmented_assignment_and_elif_are_kept():
    src = "x += 1\ny = y + 1\nxs[0] -= 2\nif a:\n    pass_it = 1\nelif b:\n    pass_it = 2\nelse:\n    if c:\n        pass_it = 3\n"
    assert format_source(src) == src


def test_else_with_a_nested_if_is_not_turned_into_elif():
    src = "if a:\n    x = 1\nelse:\n    if b:\n        x = 2\n"
    assert format_source(src) == src


def test_definitions_are_surrounded_by_blank_lines():
    src = "x = 1\nfn f():\n    return\ny = 2\nstruct S:\n    a: int\n    fn m(self):\n        return\n    fn n(self):\n        return\nz = 3\n"
    assert format_source(src) == ("x = 1\n\nfn f():\n    return\n\ny = 2\n\nstruct S:\n    a: int\n\n    fn m(self):\n        return\n\n"
                                  "    fn n(self):\n        return\n\nz = 3\n")


def test_blank_lines_are_kept_but_never_more_than_one():
    assert format_source("x = 1\n\n\n\ny = 2\nz = 3\n") == "x = 1\n\ny = 2\nz = 3\n"
    assert format_source("\n\nx = 1\n") == "x = 1\n"
    assert format_source("") == "" and format_source("\n\n") == ""


def test_comments_stay_with_their_statements():
    src = "# top\nx = 1  # beside\n\n# above f\nfn f():\n    # inside\n    return  # done\n"
    assert format_source(src) == "# top\nx = 1  # beside\n\n# above f\nfn f():\n    # inside\n    return  # done\n"


def test_comment_after_the_last_statement_and_in_an_empty_file():
    assert format_source("x = 1\n# the end\n") == "x = 1\n# the end\n"
    assert format_source("# only a comment\n") == "# only a comment\n"


def test_a_hash_inside_a_string_is_not_a_comment():
    assert format_source('x = "a # b"  # real\n') == 'x = "a # b"  # real\n'


def test_a_comment_on_an_else_line_is_kept():
    out = format_source("if a:\n    x = 1\nelse:  # otherwise\n    x = 2\n")
    assert "# otherwise" in out and dump(parse(out)) == dump(parse("if a:\n    x = 1\nelse:\n    x = 2\n"))


def test_tuples_in_returns_and_assignments_are_bare():
    assert format_source("return_it = (1, 2)\na, b = (b, a)\nfn f():\n    return (1, 2)\nx = [(1, 2)]\n") == \
        "return_it = 1, 2\na, b = b, a\n\nfn f():\n    return 1, 2\n\nx = [(1, 2)]\n"


def test_strings_floats_and_fstrings():
    assert format_source("x = 'it'\ny = 'say \"hi\"'\nz = 2.50\n") == 'x = "it"\ny = "say \\"hi\\""\nz = 2.5\n'
    assert quote("a\nb\t\"c\" \\") == '"a\\nb\\t\\"c\\" \\\\"'
    assert quote("it's", "'") == "'it\\'s'"
    assert float_text(2.0) == "2.0" and float_text(0.1) == "0.1" and "e" not in float_text(1e22)
    assert format_source("x = f'a {b} {{c}} {n:>5.2f}'\n") == 'x = f"a {b} {{c}} {n:>5.2f}"\n'
    assert format_source("x = f\"{d['k']}\"\n") == "x = f\"{d['k']}\"\n"


def test_types_and_signatures():
    src = "fn f(a:list[int],b:dict[str,tuple[int,str]],c:Result[int,str]?)->int?:\n    return none\nextern fn g(x:int)->int=\"{0}\"\n"
    assert format_source(src) == ("fn f(a: list[int], b: dict[str, tuple[int, str]], c: Result[int, str]?) -> int?:\n    return none\n\n"
                                  "extern fn g(x: int) -> int = \"{0}\"\n")


def test_imports_slices_and_checks():
    src = "import a.b as c\nfrom d import e as f,g\nx=xs[1:]\ny=xs[:-1]\nz=xs[:]\nw=v is some\nu=v?\n"
    assert format_source(src) == "import a.b as c\nfrom d import e as f, g\nx = xs[1:]\ny = xs[:-1]\nz = xs[:]\nw = v is some\nu = v?\n"


def test_a_syntax_error_is_raised():
    with pytest.raises(ZyloError):
        format_source("x = (\n")


# ---- files and the command -----------------------------------------------------------------------------------

def test_find_sources_expands_directories_and_skips_environments(tmp_path):
    (tmp_path / "a.zy").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.zy").write_text("x = 1\n")
    (tmp_path / ".zylo-env" / "packages").mkdir(parents=True)
    (tmp_path / ".zylo-env" / "packages" / "c.zy").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("x")
    assert [p.name for p in find_sources([tmp_path])] == ["a.zy", "b.zy"]


def test_format_paths_rewrites_check_reports_and_errors_are_collected(tmp_path):
    good, messy, bad = tmp_path / "good.zy", tmp_path / "messy.zy", tmp_path / "bad.zy"
    good.write_text("x = 1\n")
    messy.write_text("x=1\n")
    bad.write_text("x = (\n")
    r = format_paths([tmp_path], check=True)
    assert [p.name for p in r.changed] == ["messy.zy"] and [p.name for p in r.unchanged] == ["good.zy"]
    assert len(r.errors) == 1 and r.errors[0].file.endswith("bad.zy")
    assert messy.read_text() == "x=1\n"  # --check wrote nothing
    format_paths([tmp_path])
    assert messy.read_text() == "x = 1\n"


def test_the_cli(tmp_path, capsys):
    f = tmp_path / "p.zy"
    f.write_text("x=1\n")
    assert main(["fmt", "--check", str(f)]) == 1
    assert "would reformat" in capsys.readouterr().out
    assert main(["fmt", str(f)]) == 0 and f.read_text() == "x = 1\n"
    assert main(["fmt", "--check", str(f)]) == 0
    assert main(["fmt", "--stdout", str(f)]) == 0 and capsys.readouterr().out.endswith("x = 1\n")
    f.write_text("x = (\n")
    assert main(["fmt", str(f)]) == 1 and "error" in capsys.readouterr().err
    assert main(["fmt", "--stdout", str(tmp_path)]) == 2
