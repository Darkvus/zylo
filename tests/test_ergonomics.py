"""v0.17 ergonomics: narrowing of fields, multi-line literals and std.json accessors (checker and parser level)."""
import pytest

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import STR
from zylo.semantics.checker import check
from zylo.syntax.lexer import tokenize
from zylo.syntax.parser import parse

HEAD = "struct P:\n    name: str?\n    items: list[int]\n    n: int\n\n"


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    return e.value


# ---- narrowing of fields -------------------------------------------------------------------

def test_field_is_some_narrows_in_the_then_branch():
    prog = checked(HEAD + "fn f(p: P) -> int:\n    if p.name is some:\n        return len(p.name)\n    return 0\n")
    ret = prog.body[1].body[0].then[0]
    assert ret.value.args[0].ty == STR and ret.value.args[0].unwrap == "unwrap"


def test_field_is_none_early_exit_narrows_the_rest_of_the_block():
    checked(HEAD + "fn f(p: P) -> int:\n    if p.name is none:\n        return 0\n    return len(p.name)\n")


def test_field_narrowing_ends_with_the_branch():
    fails(HEAD + "fn f(p: P) -> int:\n    if p.name is some:\n        pass_ = 1\n    return len(p.name)\n", "len expects")


def test_result_field_narrows_both_ways():
    src = "struct R:\n    r: Result[int, str]\n\nfn f(x: R) -> str:\n    if x.r is ok:\n        return str(x.r + 1)\n    else:\n        return x.r\n"
    checked(src)


def test_nested_and_self_paths_narrow():
    src = ("struct B:\n    t: str?\n\nstruct A:\n    b: B?\n    fn f(self) -> int:\n        if self.b is some:\n"
           "            if self.b.t is some:\n                return len(self.b.t)\n        return 0\n")
    checked(src)


@pytest.mark.parametrize("body", [
    "p.name = none",
    "p.name = some(\"x\")",
    "p = P(name=none, items=[], n=1)",
])
def test_writing_a_narrowed_field_is_an_error(body):
    fails(HEAD + f"fn f(p: P):\n    if p.name is some:\n        {body}\n", "narrowed by an `is` check")


def test_a_mutating_method_on_the_owner_is_an_error():
    src = HEAD.replace("n: int\n", "n: int\n\n    fn clear(self):\n        self.name = none\n") + \
        "fn f(p: P):\n    if p.name is some:\n        p.clear()\n"
    fails(src, "narrowed by an `is` check")


def test_writing_a_sibling_field_is_fine():
    checked(HEAD + "fn f(p: P):\n    if p.name is some:\n        p.items.append(1)\n        p.n = 2\n")


def test_early_exit_narrowing_blocks_later_writes():
    fails(HEAD + "fn f(p: P):\n    if p.name is none:\n        return\n    p.name = none\n", "narrowed by an `is` check")


def test_closures_do_not_see_field_narrowing():
    fails(HEAD + "fn f(p: P) -> int:\n    if p.name is some:\n        g = fn(k: int) => len(p.name) + k\n        return g(1)\n    return 0\n", "len expects")


def test_and_chains_still_do_not_narrow():
    fails(HEAD + "fn f(p: P) -> int:\n    if p.name is some and p.n > 0:\n        return len(p.name)\n    return 0\n", "len expects")


# ---- multi-line literals -------------------------------------------------------------------

def test_a_literal_over_several_lines_is_one_logical_line():
    toks = tokenize("xs = [\n    1,\n    2,\n]\ny = 3\n")
    assert [t.kind for t in toks].count("NEWLINE") == 2
    assert not any(t.kind == "INDENT" for t in toks)


def test_multiline_literals_and_calls_parse_with_trailing_commas():
    src = "d = {\n    'a': [\n        1,\n    ],\n}\nt = (\n    1,\n    2,\n)\nprint(\n    d,\n    t,\n)\n"
    prog = parse(src)
    assert isinstance(prog.body[0].value, ast.DictLit) and isinstance(prog.body[1].value, ast.TupleLit)


def test_comments_and_blank_lines_inside_brackets():
    prog = parse("xs = [\n    1,  # one\n\n    # skipped\n    2,\n]\n")
    assert len(prog.body[0].value.items) == 2


def test_an_unclosed_bracket_is_reported_at_its_line():
    with pytest.raises(ZyloError, match="expected '\\)'") as e:
        parse("print(1\nx = 2\n")
    assert e.value.line == 1


def test_multiline_literals_run_end_to_end(tmp_path):
    import shutil
    import subprocess

    if shutil.which("rustc") is None:
        pytest.skip("rustc not installed")
    from zylo.application.compile import transpile
    from zylo.infrastructure.rustc import compile_rust

    src = tmp_path / "m.zy"
    src.write_text(
        "fn total(xs: list[int], d: dict[str, int]) -> int:\n    return sum(xs) + len(d)\n\n"
        "t = (\n    1,\n    \"x\",\n)\n"
        "print(total([\n    1,\n    2,\n], {\n    \"a\": 1,\n    \"b\": 2,\n}), t)\n"
    )
    exe = tmp_path / "m"
    compile_rust(transpile(src), exe)
    assert subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout == '5 (1, "x")\n'
