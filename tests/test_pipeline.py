import pytest

from zylo.checker import check
from zylo.codegen import generate
from zylo.errors import ZyloError
from zylo.lexer import tokenize
from zylo.parser import parse


def test_lexer_basic():
    kinds = [t.kind for t in tokenize("print(1 + 2)")]
    assert kinds == ["IDENT", "OP", "INT", "OP", "INT", "OP", "NEWLINE", "EOF"]


def test_precedence():
    rs = generate(check(parse("print(1 + 2 * 3)")))
    assert "(1i64 + (2i64 * 3i64))" in rs


def test_parens_and_unary():
    rs = generate(check(parse("print(-(1 + 2) * 3)")))
    assert "((-(1i64 + 2i64)) * 3i64)" in rs


def test_string():
    assert '"hola"' in generate(check(parse('print("hola")')))


def test_syntax_error_has_position():
    with pytest.raises(ZyloError) as e:
        parse("print(1 +")
    assert e.value.line == 1
