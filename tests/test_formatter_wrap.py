"""Line wrapping in the formatter: it must keep the meaning, be idempotent, keep comments and obey the line length."""
import pytest
from test_formatter import CORPUS, dump

from zylo.application.format import configured_line_length, format_paths
from zylo.domain.errors import ZyloError
from zylo.interface.cli import main
from zylo.syntax.formatter import _scan_comments, format_source
from zylo.syntax.parser import parse

WIDTHS = [20, 40, 60, 88, 120]

LONG = '''\
from std.json import parse_value, stringify_value, pretty_value, get_string_field, get_int_field
struct P:
    tags: list[str] = ["aaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbb", "cccccccccccccccccccc", "ddddddddddddddd"]
    fn long_method(self, first_argument: int, second_argument: str, third_argument: list[int] = [1, 2, 3]) -> str:
        return "x"
fn compute(first_argument: int, second_argument: str, third_argument: list[int], fourth: bool) -> int:
    if first_argument > 100 and second_argument == "something long here" or third_argument.len() > 5 and fourth:
        return first_argument * 2 + second_argument.len() * 3 + third_argument.len() * 4 + first_argument * 5
    while first_argument < 10 and not (second_argument == "aaaaaaaaaaaaaaaaaaaaaaaaaaaa" or fourth == false or true):
        x = 1  # trailing
    result = builder.start().with_name("something").with_value(some_value_here).with_other(another_value).finish()  # keep me
    a, b = first_argument + second_argument.len() + third_argument.len(), first_argument * 2 + second_argument.len()
    return first_argument, second_argument, third_argument.len(), fourth, first_argument, second_argument
d = {"alpha": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "beta": {"x": 1, "y": 2, "z": 3, "w": 4}, "gamma": "a long string value"}
x = some_function_name(argument_one, argument_two, keyword_one=value_one, keyword_two=value_two_long)
f = fn(a: int, b: int) => a * b + a * a + b * b + a * b * a + b * a * b + a + b + a + b + a + b + a + b
z = value_one_is_long_enough[index_expression_that_is_long + another_long_index_expression_here + 1]
y = not some_object.method_one().method_two().method_three(argument_number_one).method_four()
v = some_value is none or other_value_that_is_long is some and third_value_that_is_long is none or xxxxxx
fn main():
    match shape:
        Shape.Circle(radius) | Shape.Square(side) | Shape.Triangle(base) | Shape.Rectangle(w) | Shape.Hexagon(s):
            print("x")
        Shape.Circle(radius) | Shape.Square(side) | Shape.Triangle(base) | Shape.Rectangle(w) if radius > 100 and side < 5:
            print("y")
        Shape.A | Shape.B if radius > 100 and side < 5 and base == 3 and radius > 100 and side < 5 and base == 3:
            print("z")
extern fn very_long_extern_function_name(first_parameter: int, second_parameter: str, third: int) -> int = "rust::call({0})"
'''


def check(src: str, width: int) -> str:
    once = format_source(src, width)
    assert dump(parse(once)) == dump(parse(src)), "the meaning changed"
    assert format_source(once, width) == once, "not idempotent"
    assert sorted(t for _, t, _ in _scan_comments(once)) == sorted(t for _, t, _ in _scan_comments(src))
    return once


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("name,src", CORPUS, ids=[n for n, _ in CORPUS])
def test_every_repository_program_keeps_its_meaning_and_is_idempotent_at_any_width(name, src, width):
    check(src, width)


@pytest.mark.parametrize("width", WIDTHS)
def test_the_long_sample_round_trips_at_any_width(width):
    check(LONG, width)


def test_lines_obey_the_limit_unless_a_string_is_in_the_way():
    out = format_source(LONG, 60)
    assert [ln for ln in out.splitlines() if len(ln) > 60] == []


def test_short_lines_are_left_alone_and_the_default_is_88():
    src = "x = foo(1, 2, 3)\n"
    assert format_source(src, 12) != src
    assert format_source(src) == src
    line = "x = f(" + ", ".join(f"arg{i}" for i in range(12)) + ")"
    assert len(line) <= 88
    assert format_source(line + "\n") == line + "\n"


def test_calls_and_literals_go_one_item_per_line_with_a_trailing_comma():
    assert format_source("x = foo(alpha, beta, key=gamma)\n", 20) == "x = foo(\n    alpha,\n    beta,\n    key=gamma,\n)\n"
    assert format_source("x = [alpha, beta]\n", 12) == "x = [\n    alpha,\n    beta,\n]\n"
    assert format_source("x = {alpha: 1, beta: 2}\n", 12) == "x = {\n    alpha: 1,\n    beta: 2,\n}\n"
    assert format_source("x = {alpha, beta}\n", 12) == "x = {\n    alpha,\n    beta,\n}\n"
    assert format_source("x = (alpha, beta)\n", 12) == "x = (\n    alpha,\n    beta,\n)\n"


def test_nested_items_wrap_only_as_much_as_needed():
    out = format_source("x = foo([1, 2, 3], bar(alpha, beta, gamma, delta, epsilon))\n", 46)
    assert out == "x = foo(\n    [1, 2, 3],\n    bar(alpha, beta, gamma, delta, epsilon),\n)\n"


def test_bare_tuples_get_parentheses_when_they_wrap():
    assert format_source("return alpha, beta, gamma\n", 20) == "return (\n    alpha,\n    beta,\n    gamma,\n)\n"
    assert format_source("a, b = alpha, beta, gamma\n", 20) == "a, b = (\n    alpha,\n    beta,\n    gamma,\n)\n"


def test_boolean_and_arithmetic_expressions_break_inside_parentheses():
    assert format_source("if alpha and beta or gamma:\n    pass_it = 1\n", 20) == \
        "if (\n    alpha and beta\n    or gamma\n):\n    pass_it = 1\n"
    assert format_source("x = alpha + beta * gamma - delta\n", 20) == "x = (\n    alpha\n    + beta * gamma\n    - delta\n)\n"
    assert format_source("while not (alpha and beta and gamma):\n    pass_it = 1\n", 30) == \
        "while not (\n    alpha and beta and gamma\n):\n    pass_it = 1\n"


def test_method_chains_go_one_call_per_line():
    out = format_source("x = builder.start().with_name(1).with_value(2).finish()\n", 40)
    assert out == "x = (\n    builder.start()\n    .with_name(1)\n    .with_value(2)\n    .finish()\n)\n"


def test_a_chain_whose_call_is_too_long_breaks_that_call_instead():
    out = format_source('doc = json.parse("a very long literal text that does not fit anywhere").unwrap()\n', 40)
    assert out == 'doc = json.parse(\n    "a very long literal text that does not fit anywhere",\n).unwrap()\n'


def test_long_indexes_wrap_inside_the_brackets():
    assert format_source("x = values[alpha_index + beta_index]\n", 30) == "x = values[\n    alpha_index + beta_index\n]\n"


def test_struct_constructors_with_named_arguments_wrap():
    out = format_source("p = Person(name=\"Ana Maria\", age=30, city=\"Madrid\")\n", 30)
    assert out == 'p = Person(\n    name="Ana Maria",\n    age=30,\n    city="Madrid",\n)\n'


def test_signatures_wrap_one_parameter_per_line():
    src = "fn area(width: int, height: int = 2) -> int:\n    return width * height\n"
    assert format_source(src, 30) == "fn area(\n    width: int,\n    height: int = 2,\n) -> int:\n    return width * height\n"
    method = "struct S:\n    n: int\n\n    fn add(self, other: int) -> int:\n        return other\n"
    assert "    fn add(\n        self,\n        other: int,\n    ) -> int:\n" in format_source(method, 30)
    extern = 'extern fn sqrt2(first: float, second: float) -> float = "{0}.sqrt()"\n'
    assert format_source(extern, 40) == 'extern fn sqrt2(\n    first: float,\n    second: float,\n) -> float = "{0}.sqrt()"\n'


def test_long_imports_are_parenthesized():
    out = format_source("from a.b import alpha, beta as b, gamma\n", 30)
    assert out == "from a.b import (\n    alpha,\n    beta as b,\n    gamma,\n)\n"
    assert format_source("pub from a import alpha, beta\n", 20).startswith("pub from a import (\n")
    assert format_source("from a import alpha\n", 5) == "from a import alpha\n"  # nothing to split


def test_long_or_patterns_are_parenthesized_and_keep_their_guard():
    src = "match x:\n    A.One | A.Two | A.Three if y > 1:\n        pass_it = 1\n    _:\n        pass_it = 2\n"
    out = format_source(src, 24)
    assert out.startswith("match x:\n    (\n        A.One\n        | A.Two\n        | A.Three\n    ) if y > 1:\n")
    assert dump(parse(out)) == dump(parse(src))


def test_a_trailing_comment_stays_on_the_first_line_of_a_wrapped_statement():
    out = format_source("x = foo(alpha, beta)  # why\n", 12)
    assert out == "x = foo(  # why\n    alpha,\n    beta,\n)\n"
    assert format_source(out, 12) == out


def test_comments_between_items_survive_and_wrapping_is_stable():
    src = "x = [\n    1,  # one\n    # two\n    2,\n]\ny = 3\n"
    out = check(src, 4 + 4)
    assert "# one" in out and "# two" in out


def test_wrapped_output_uses_the_multi_line_literal_rules():
    out = format_source(LONG, 40)
    assert any(line.startswith(")") for line in out.splitlines())
    assert parse(out)  # indentation inside the brackets is free, and each closing bracket stands on its own line


def test_f_strings_and_long_strings_are_never_split():
    src = 'print(f"hello {name} and a very long text that cannot be split at all, never ever")\n'
    out = format_source(src, 30)
    assert out == 'print(\n    f"hello {name} and a very long text that cannot be split at all, never ever",\n)\n'


# ---- the new syntax the formatter relies on ------------------------------------------------------------------

def test_the_parser_accepts_trailing_commas_in_parameters_and_parenthesized_imports_and_patterns():
    parse("fn f(a: int, b: int,) -> int:\n    return a\n")
    parse("struct S:\n    n: int\n\n    fn m(self,):\n        pass_it = 1\n")
    parse("from a import (b, c,)\nfrom a import (d)\n")
    parse("match x:\n    (A.B | A.C):\n        pass_it = 1\n    _:\n        pass_it = 2\n")
    with pytest.raises(ZyloError):
        parse("from a import (b, c\n")


# ---- configuration -----------------------------------------------------------------------------------------

def test_the_cli_takes_a_line_length(tmp_path, capsys):
    f = tmp_path / "a.zy"
    f.write_text("x = foo(alpha, beta)\n")
    assert main(["fmt", "--stdout", "--line-length", "12", str(f)]) == 0
    assert capsys.readouterr().out == "x = foo(\n    alpha,\n    beta,\n)\n"
    assert main(["fmt", "--stdout", str(f)]) == 0
    assert capsys.readouterr().out == "x = foo(alpha, beta)\n"
    assert main(["fmt", "--line-length", "12", str(f)]) == 0
    assert "alpha,\n" in f.read_text()
    assert main(["fmt", "--check", "--line-length", "12", str(f)]) == 0
    assert main(["fmt", "--line-length", "3", str(f)]) == 2
    assert "line-length" in capsys.readouterr().err


def test_zylo_toml_sets_the_line_length_for_files_below_it(tmp_path):
    (tmp_path / "zylo.toml").write_text('[package]\nname = "p"\n\n[format]\nline-length = 12\n')
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.zy"
    f.write_text("x = foo(alpha, beta)\n")
    assert configured_line_length(f) == 12
    assert configured_line_length(src) == 12
    assert format_paths([f]).changed == [f]
    assert f.read_text() == "x = foo(\n    alpha,\n    beta,\n)\n"
    f.write_text("x = foo(alpha, beta)\n")
    assert format_paths([f], line_length=88).unchanged == [f]  # the flag wins over the file


def test_without_a_setting_the_line_length_is_88(tmp_path):
    (tmp_path / "zylo.toml").write_text('[package]\nname = "p"\n')
    assert configured_line_length(tmp_path / "a.zy") == 88


def test_an_invalid_line_length_in_zylo_toml_is_an_error(tmp_path):
    (tmp_path / "zylo.toml").write_text('[package]\nname = "p"\n[format]\nline-length = "wide"\n')
    with pytest.raises(ZyloError, match="line-length"):
        configured_line_length(tmp_path / "a.zy")


def test_rewriting_the_manifest_keeps_the_format_setting(tmp_path):
    from zylo.packaging.manifest import dump_manifest, load_manifest
    path = tmp_path / "zylo.toml"
    path.write_text('[package]\nname = "p"\n\n[format]\nline-length = 100\n')
    manifest = load_manifest(path)
    assert manifest.line_length == 100
    path.write_text(dump_manifest(manifest))
    assert load_manifest(path).line_length == 100
