import pytest

from lumo.codegen import generate
from lumo.errors import LumoError
from lumo.lexer import tokenize
from lumo.parser import parse


def test_lexer_basic():
    kinds = [t.kind for t in tokenize("print(1 + 2)")]
    assert kinds == ["IDENT", "OP", "INT", "OP", "INT", "OP", "NEWLINE", "EOF"]


def test_precedence():
    rs = generate(parse("print(1 + 2 * 3)"))
    assert "(1i64 + (2i64 * 3i64))" in rs


def test_parens_and_unary():
    rs = generate(parse("print(-(1 + 2) * 3)"))
    assert "((-(1i64 + 2i64)) * 3i64)" in rs


def test_string():
    assert '"hola"' in generate(parse('print("hola")'))


def test_syntax_error_has_position():
    with pytest.raises(LumoError) as e:
        parse("print(1 +")
    assert e.value.line == 1
