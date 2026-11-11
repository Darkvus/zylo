import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.domain.errors import BackendError, ZyloError
from zylo.interface.cli import exit_code, main
from zylo.interface.diagnostics import format_error
from zylo.semantics.checker import check
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc no instalado")


def write(tmp_path: Path, src: str, name: str = "prog.zy") -> Path:
    f = tmp_path / name
    f.write_text(src)
    return f


# ---- diagnostics ---------------------------------------------------------------------

def test_format_error_snippet_and_caret():
    e = ZyloError("something failed", 2, 5)
    out = format_error(e, "a = 1\nb = oops\n", "x.zy")
    assert out.splitlines() == [
        "x.zy:2:5: error: something failed",
        "  |",
        "2 | b = oops",
        "  |     ^",
    ]


def test_format_error_gutter_width_with_two_digit_lines():
    src = "\n" * 9 + "bad line\n"
    out = format_error(ZyloError("m", 10, 1), src, "f.zy")
    assert "10 | bad line" in out and "   | ^" in out


def test_format_error_without_position():
    out = format_error(ZyloError("general"), "x = 1\n", "x.zy")
    assert out == "x.zy: error: general"


def test_format_error_line_out_of_range_is_safe():
    out = format_error(ZyloError("m", 99, 1), "x = 1\n", "x.zy")
    assert out == "x.zy:99:1: error: m"


def test_format_error_color_wraps_ansi():
    out = format_error(ZyloError("m", 1, 1), "x\n", "x.zy", color=True)
    assert "\033[" in out
    assert "\033[" not in format_error(ZyloError("m", 1, 1), "x\n", "x.zy", color=False)


def test_backend_error_has_no_snippet():
    out = format_error(BackendError("rustc roto"), "x = 1\n", "x.zy")
    assert out == "x.zy: error: rustc roto"


# ---- parse messages ----------------------------------------------------------------------

def test_error_at_end_of_file_has_real_position():
    with pytest.raises(ZyloError) as e:
        parse("fn f():\n")
    assert e.value.line == 1 and "an indented block" in e.value.msg


def test_friendly_token_names():
    with pytest.raises(ZyloError, match="expected ':', found end of line"):
        parse("if a\n    x = 1\n")
    with pytest.raises(ZyloError, match="expected '\\)'"):
        parse("print(1\n")


# ---- exit() ---------------------------------------------------------------------------------

def test_exit_requires_int():
    with pytest.raises(ZyloError, match="expected int"):
        check(parse('exit("a")\n'))


def test_exit_arity():
    with pytest.raises(ZyloError, match="takes 1 argument"):
        check(parse("exit()\n"))


def test_exit_codegen():
    rs = pipeline.transpile_source("exit(3)\n")
    assert "std::process::exit((3i64) as i32)" in rs


# ---- comandos sin rustc --------------------------------------------------------------------

def test_exit_code_signal_convention():
    assert exit_code(0) == 0 and exit_code(101) == 101 and exit_code(-9) == 137


def test_check_ok(tmp_path, capsys):
    assert main(["check", str(write(tmp_path, "x = 1\nprint(x)\n"))]) == 0
    assert capsys.readouterr().out == ""


def test_check_reports_error_with_snippet(tmp_path, capsys):
    f = write(tmp_path, "x = 1\nprint(x + 2.5)\n")
    assert main(["check", str(f)]) == 1
    err = capsys.readouterr().err
    assert f"{f}:2:9: error:" in err
    assert "2 | print(x + 2.5)" in err
    assert "^" in err


def test_emit_prints_rust(tmp_path, capsys):
    assert main(["emit", str(write(tmp_path, "print(1)\n"))]) == 0
    assert "fn main()" in capsys.readouterr().out


def test_emit_does_not_print_on_error(tmp_path, capsys):
    assert main(["emit", str(write(tmp_path, "print(zzz)\n"))]) == 1
    assert capsys.readouterr().out == ""


def test_missing_file(tmp_path, capsys):
    assert main(["check", str(tmp_path / "nope.zy")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_non_utf8_file(tmp_path, capsys):
    f = tmp_path / "bin.zy"
    f.write_bytes(b"\xff\xfe\x00")
    assert main(["check", str(f)]) == 1
    assert "UTF-8" in capsys.readouterr().err


def test_unknown_command_is_usage_error():
    with pytest.raises(SystemExit) as e:
        main(["frobnicate", "x.zy"])
    assert e.value.code == 2


def test_rustc_missing_is_reported(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert main(["run", str(write(tmp_path, "print(1)\n"))]) == 1
    err = capsys.readouterr().err
    assert "rustc not found" in err and "rustup.rs" in err


def test_build_refuses_to_overwrite_source(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/true")
    f = write(tmp_path, "print(1)\n", name="prog")  # no extension: the default output == the source
    assert main(["build", str(f)]) == 1
    assert "would overwrite" in capsys.readouterr().err
    assert f.read_text() == "print(1)\n"


# ---- commands that need rustc -----------------------------------------------------------------------

@needs_rustc
def test_build_and_emit_rs(tmp_path, capsys):
    f = write(tmp_path, 'print("hola")\n')
    out = tmp_path / "hola"
    assert main(["build", str(f), "-o", str(out), "--emit-rs"]) == 0
    assert "built" in capsys.readouterr().out
    assert subprocess.run([str(out)], capture_output=True, text=True).stdout == "hola\n"
    assert "fn main()" in (tmp_path / "hola.rs").read_text()


@needs_rustc
def test_build_without_emit_rs_leaves_no_rs(tmp_path):
    f = write(tmp_path, "print(1)\n")
    assert main(["build", str(f), "-o", str(tmp_path / "a")]) == 0
    assert not (tmp_path / "a.rs").exists()


@needs_rustc
def test_run_forwards_stdout_and_zero(tmp_path, capfd):
    assert main(["run", str(write(tmp_path, "print(1 + 1)\n"))]) == 0
    assert capfd.readouterr().out == "2\n"


@needs_rustc
def test_run_forwards_exit_code(tmp_path):
    assert main(["run", str(write(tmp_path, "exit(7)\n"))]) == 7


@needs_rustc
def test_run_overflow_panics_with_101(tmp_path, capfd):
    src = "fn inc(n: int) -> int:\n    return n + 1\nprint(inc(9223372036854775807))\n"
    assert main(["run", str(write(tmp_path, src))]) == 101
    assert "overflow" in capfd.readouterr().err


@needs_rustc
def test_no_overflow_checks_wraps(tmp_path, capfd):
    src = "fn inc(n: int) -> int:\n    return n + 1\nprint(inc(9223372036854775807))\n"
    assert main(["run", "--no-overflow-checks", str(write(tmp_path, src))]) == 0
    assert capfd.readouterr().out == "-9223372036854775808\n"


@needs_rustc
def test_type_error_stops_before_rustc(tmp_path, capsys):
    assert main(["run", str(write(tmp_path, "x = 1 + 2.0\n"))]) == 1
    assert "error:" in capsys.readouterr().err
