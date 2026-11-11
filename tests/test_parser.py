import pytest

from zylo import ast
from zylo.errors import ZyloError
from zylo.lexer import tokenize
from zylo.parser import parse


def stmt(src: str):
    return parse(src).body[0]


def expr(src: str):
    return stmt(src).expr


# ---- lexer ---------------------------------------------------------------------

def test_hash_inside_string_is_not_comment():
    assert expr('"a # b"').value == "a # b"


def test_comment_only_and_trailing_comment():
    prog = parse("# hola\nx = 1  # fin\n")
    assert len(prog.body) == 1


def test_compound_operator_tokens():
    ops = [t.value for t in tokenize("x += 1") if t.kind == "OP"]
    assert ops == ["+="]


# ---- asignación ----------------------------------------------------------------

def test_assign_plain():
    s = stmt("x = 1")
    assert isinstance(s, ast.Assign) and s.target.id == "x" and s.annotation is None


def test_assign_annotated():
    s = stmt("xs: list[int] = [1, 2]")
    assert s.annotation.name == "list" and s.annotation.args[0].name == "int"
    assert isinstance(s.value, ast.ListLit) and len(s.value.items) == 2


def test_augmented_assign_desugars():
    s = stmt("x += 2")
    assert isinstance(s, ast.Assign)
    assert isinstance(s.value, ast.BinOp) and s.value.op == "+"
    assert s.value.left.id == "x"


def test_index_assign_and_read():
    s = stmt("xs[0] = xs[1]")
    assert isinstance(s.target, ast.Index) and isinstance(s.value, ast.Index)


def test_assign_to_literal_is_error():
    with pytest.raises(ZyloError):
        parse("1 = 2")


# ---- control de flujo -------------------------------------------------------------

def test_if_elif_else():
    s = stmt("if a:\n    x = 1\nelif b:\n    x = 2\nelse:\n    x = 3\n")
    assert isinstance(s, ast.If)
    nested = s.else_[0]
    assert isinstance(nested, ast.If)
    assert len(nested.else_) == 1


def test_if_without_else():
    assert stmt("if a:\n    x = 1\n").else_ == []


def test_while_break_continue():
    s = stmt("while true:\n    break\n    continue\n")
    assert isinstance(s, ast.While)
    assert [type(b) for b in s.body] == [ast.Break, ast.Continue]


def test_for_range():
    s = stmt("for i in range(0, 10):\n    print(i)\n")
    assert isinstance(s, ast.For) and s.var == "i"
    assert isinstance(s.iter, ast.Call) and s.iter.func == "range"


def test_nested_blocks_dedent_twice():
    prog = parse("while a:\n    if b:\n        x = 1\ny = 2\n")
    assert len(prog.body) == 2
    assert isinstance(prog.body[0].body[0], ast.If)


# ---- funciones -------------------------------------------------------------------

def test_fn_with_params_and_return_type():
    s = stmt("fn add(a: int, b: int) -> int:\n    return a + b\n")
    assert isinstance(s, ast.FnDef)
    assert [(p.name, p.type.name) for p in s.params] == [("a", "int"), ("b", "int")]
    assert s.ret.name == "int"
    assert isinstance(s.body[0], ast.Return)


def test_fn_without_return_type():
    s = stmt("fn hi():\n    print(1)\n")
    assert s.ret is None and s.params == []


def test_bare_return():
    s = stmt("fn f():\n    return\n")
    assert s.body[0].value is None


def test_param_requires_type():
    with pytest.raises(ZyloError) as e:
        parse("fn f(a):\n    return\n")
    assert e.value.line == 1


def test_unknown_type():
    with pytest.raises(ZyloError, match="tipo desconocido"):
        parse("fn f(a: banana):\n    return\n")


# ---- operadores lógicos -------------------------------------------------------------

def test_and_binds_tighter_than_or():
    e = expr("a or b and c")
    assert e.op == "or" and e.right.op == "and"


def test_not_binds_looser_than_comparison():
    e = expr("not a == b")
    assert isinstance(e, ast.UnaryOp) and e.op == "not"
    assert e.operand.op == "=="


def test_not_then_and():
    e = expr("not a and b")
    assert e.op == "and" and isinstance(e.left, ast.UnaryOp)


# ---- errores con posición -----------------------------------------------------------

def test_missing_colon():
    with pytest.raises(ZyloError) as e:
        parse("if a\n    x = 1\n")
    assert (e.value.line, e.value.col) == (1, 5)


def test_else_without_if():
    with pytest.raises(ZyloError, match="sin 'if'"):
        parse("else:\n    x = 1\n")


def test_unexpected_indent():
    with pytest.raises(ZyloError, match="indentación inesperada"):
        parse("x = 1\n    y = 2\n")


def test_node_positions():
    s = stmt("x = 1\n")
    assert (s.line, s.col) == (1, 1)
    f = parse("\nfn f():\n    return\n").body[0]
    assert f.line == 2
