"""Reporting every mistake in one run: parser recovery, the checker's per-statement checking and the CLI output."""
from pathlib import Path

import pytest

from zylo.application.compile import diagnose_source
from zylo.domain.errors import ZyloError
from zylo.interface.cli import main
from zylo.interface.diagnostics import format_errors
from zylo.semantics.checker import check, check_all
from zylo.syntax.parser import parse, parse_collect


def messages(errors):
    return [(e.line, e.msg) for e in errors]


# ---- the parser ----------------------------------------------------------------------------------

def test_a_correct_program_has_no_syntax_errors():
    prog, errors = parse_collect("x = 1\nprint(x)\n")
    assert errors == [] and len(prog.body) == 2


def test_each_bad_statement_is_reported_and_the_good_ones_are_kept():
    src = "x = 1 +\nprint(2)\nprint(3\ny = 4\nfn ok() -> int:\n    return 1\n"
    prog, errors = parse_collect(src)
    assert [e.line for e in errors] == [1, 3]
    assert [type(s).__name__ for s in prog.body] == ["ExprStmt", "Assign", "FnDef"]


def test_a_bad_block_header_is_skipped_with_its_whole_body():
    src = "fn f(:\n    print(1)\n    print(2)\nz = 7\n"
    prog, errors = parse_collect(src)
    assert [e.line for e in errors] == [1]
    assert [type(s).__name__ for s in prog.body] == ["Assign"]  # the body was not parsed as stray statements


def test_errors_inside_a_block_do_not_hide_the_following_ones():
    src = "while true:\n    a = (\n    b = 2\n    c = 3 +\nd = 1\n"
    prog, errors = parse_collect(src)
    assert [e.line for e in errors] == [2, 4]
    assert len(prog.body) == 2 and len(prog.body[0].body) == 1  # `b = 2` survived inside the loop


def test_nested_blocks_recover_at_their_own_level():
    src = "fn f():\n    if x\n        print(1)\n    print(2)\n    for i in [1]:\n        y = (\n        z = 1\nfn g():\n    return\n"
    prog, errors = parse_collect(src)
    assert [e.line for e in errors] == [2, 6]
    assert [s.name for s in prog.body] == ["f", "g"]


def test_a_lexical_error_is_the_only_one_reported():
    prog, errors = parse_collect('x = (\ny = "unterminated\n')
    assert len(errors) == 1 and "unterminated string" in errors[0].msg and prog.body == []


def test_collected_errors_carry_the_file():
    _, errors = parse_collect("x = (\n", "a.zy")
    assert errors[0].file == "a.zy"


def test_parse_still_raises_on_the_first_error():
    with pytest.raises(ZyloError, match="unexpected expression"):
        parse("x = (\ny = (\n")


def test_unclosed_block_at_the_end_of_the_file_is_reported():
    _, errors = parse_collect("fn f():\n")
    assert len(errors) == 1 and "indented block" in errors[0].msg


# ---- the checker -----------------------------------------------------------------------------------

def test_every_type_error_is_reported_in_source_order():
    src = 'x = 1 + "a"\nprint(zzz)\nfn f(n: int) -> int:\n    return "oops"\nfn g():\n    if 1:\n        print(1)\n'
    assert messages(check_all(parse(src))) == [
        (1, "`+` between int and str: types must match"), (2, "undefined variable 'zzz'"),
        (4, "returning str but the function declares int"), (6, "the `if` condition must be bool, not int")]


def test_several_errors_inside_one_function():
    src = 'fn f(a: int) -> int:\n    x = a + "s"\n    y = a.nope\n    return a\n'
    assert [e.line for e in check_all(parse(src))] == [2, 3]


def test_a_variable_whose_declaration_failed_is_not_reported_again():
    src = 'x = 1 + "a"\ny = x + 2\nprint(x, y)\nz = y * 2\nprint(zzz)\n'
    assert messages(check_all(parse(src))) == [(1, "`+` between int and str: types must match"), (5, "undefined variable 'zzz'")]


def test_a_failed_return_is_not_also_a_missing_return():
    src = 'fn f(a: int) -> int:\n    b = a + "x"\n    return b + 1\n'
    assert [e.line for e in check_all(parse(src))] == [2]


def test_a_missing_return_is_still_reported_when_nothing_else_is_wrong():
    assert messages(check_all(parse("fn f(a: int) -> int:\n    print(a)\n"))) == [(1, "function 'f' can finish without a `return`")]


def test_poisoned_names_do_not_leak_between_functions():
    src = 'fn f():\n    x = 1 + "a"\n    print(x)\nfn g():\n    print(x)\n'
    assert [e.line for e in check_all(parse(src))] == [2, 5]


def test_each_method_is_checked_on_its_own():
    src = 'struct P:\n    n: int\n\n    fn a(self) -> int:\n        return "s"\n\n    fn b(self) -> int:\n        return "t"\n'
    assert [e.line for e in check_all(parse(src))] == [5, 8]


def test_a_broken_declaration_is_reported_alone():
    errors = check_all(parse("struct A:\n    n: int\nstruct A:\n    m: int\nprint(zzz)\n"))
    assert [e.msg for e in errors] == ["struct 'A' is already defined"]


def test_check_raises_the_first_error():
    with pytest.raises(ZyloError, match="undefined variable 'a'"):
        check(parse("print(a)\nprint(b)\n"))


def test_state_survives_a_failed_statement():
    src = 'while true:\n    x = 1 + "a"\n    break\nfor i in [1]:\n    y = 1 + "b"\nprint(1)\nbreak\n'
    assert [e.line for e in check_all(parse(src))] == [2, 5, 7]  # loop bookkeeping stayed consistent


# ---- diagnose_source and the output ------------------------------------------------------------------

def test_syntax_errors_come_first_and_stop_the_run(tmp_path):
    errors = diagnose_source("x = (\nprint(zzz)\ny = (\n", tmp_path / "a.zy")
    assert [e.line for e in errors] == [1, 3]


def test_unsaved_text_is_what_gets_checked(tmp_path):
    f = tmp_path / "main.zy"
    f.write_text("print(1)\n")  # the file on disk is fine
    assert diagnose_source("print(zzz)\n", f)[0].msg == "undefined variable 'zzz'"
    assert diagnose_source("print(1)\n", f) == []


def test_an_import_error_is_a_single_error(tmp_path):
    f = tmp_path / "main.zy"
    errors = diagnose_source("import nothing\nprint(zzz)\n", f)
    assert len(errors) == 1 and "module 'nothing' not found" in errors[0].msg


def test_format_errors_summary_limit_and_other_files(tmp_path):
    lib = tmp_path / "lib.zy"
    lib.write_text("fn f() -> int:\n    return 1\n")
    errors = [ZyloError("first", 1, 1), ZyloError("in lib", 2, 5, str(lib)), ZyloError("third", 1, 1)]
    text = format_errors(errors, "main.zy", "x = 1\n")
    assert text.count("error:") == 4  # three errors and the closing line
    assert "lib.zy:2:5: error: in lib" in text and "    return 1" in text
    assert text.rstrip().endswith("error: aborting due to 3 previous errors")
    assert "... and 1 more" in format_errors(errors, "main.zy", "x = 1\n", limit=2)
    assert "aborting" not in format_errors(errors[:1], "main.zy", "x = 1\n")


def test_the_cli_reports_every_error_and_exits_with_1(tmp_path, capsys):
    f = tmp_path / "p.zy"
    f.write_text('x = 1 + "a"\nprint(zzz)\nfn f() -> int:\n    return "oops"\n')
    assert main(["check", str(f)]) == 1
    err = capsys.readouterr().err
    assert err.count(": error:") == 3 and "aborting due to 3 previous errors" in err


def test_the_cli_does_not_build_when_there_are_errors(tmp_path, capsys):
    f = tmp_path / "p.zy"
    f.write_text("x = (\nprint(zzz)\n")
    assert main(["run", str(f)]) == 1
    assert "unexpected expression" in capsys.readouterr().err
    assert not Path(tmp_path / "p").exists()
