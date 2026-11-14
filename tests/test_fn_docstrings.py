"""Function doc strings: a leading string literal in a `fn` body or method is its description (v0.30)."""
import pytest

from zylo.application.compile import transpile_source
from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

SRC = '''fn add(a: int, b: int) -> int:
    "Adds two numbers."
    return a + b

struct P:
    x: int

    fn get(self) -> int:
        "The x."
        return self.x

fn main():
    print(add(1, 2))
'''


def test_parser_stores_doc_and_drops_statement():
    prog = parse(SRC)
    fn = prog.body[0]
    assert fn.doc == "Adds two numbers." and fn.doc_line == 2
    assert len(fn.body) == 1 and isinstance(fn.body[0], ast.Return)
    assert prog.body[1].methods[0].doc == "The x."


def test_only_the_first_string_is_the_doc():
    fn = parse('fn f() -> int:\n    "a"\n    "b"\n    return 1\n').body[0]
    assert fn.doc == "a" and len(fn.body) == 2


def test_string_not_first_is_not_a_doc():
    fn = parse('fn f() -> int:\n    x = 1\n    "b"\n    return x\n').body[0]
    assert fn.doc is None


def test_doc_only_body_is_valid():
    fn = parse('fn f():\n    "nothing"\n').body[0]
    assert fn.doc == "nothing" and fn.body == []


def test_doc_only_body_still_needs_a_return_value():
    with pytest.raises(ZyloError):
        transpile_source('fn f() -> int:\n    "nothing"\n\nfn main():\n    print(f())\n')


def test_formatter_keeps_doc():
    assert format_source(SRC) == SRC
    messy = 'fn f():\n    "doc"\n\n    print(1)\n'
    assert format_source(messy) == messy


def test_codegen_emits_doc_comment():
    rust = transpile_source(SRC)
    assert "/// Adds two numbers.\nfn add(" in rust
    assert "    /// The x.\n    fn get(" in rust
    assert 'String::from("Adds' not in rust


def test_codegen_multiline_doc():
    rust = transpile_source('fn f():\n    "one\\ntwo"\n\nfn main():\n    f()\n')
    assert "/// one\n/// two\nfn f(" in rust
