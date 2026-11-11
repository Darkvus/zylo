"""Input and output: program arguments, stdin, files, environment, stderr, time."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from zylo.checker import check
from zylo.errors import ZyloError
from zylo.parser import parse
from zylo.types import BOOL, FLOAT, INT, STR, UNIT, ListT, OptionT, ResultT

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")


def checked(src: str):
    return check(parse(src))


def ty(src: str):
    return checked(src).body[-1].value.ty


def fails(src: str, match: str) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    return e.value


def run(tmp_path: Path, src: str, *args: str, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    prog = tmp_path / "prog.zy"
    prog.write_text(src)
    full_env = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-m", "zylo", "run", str(prog), *args], input=stdin, capture_output=True,
                          text=True, cwd=tmp_path, env=full_env, timeout=120)


# ---- types -----------------------------------------------------------------------------------------

def test_io_builtin_types():
    assert ty("x = argv()\n") == ListT(STR)
    assert ty("x = input()\n") == STR
    assert ty('x = input("name? ")\n') == STR
    assert ty("x = read_line()\n") == OptionT(STR)
    assert ty("x = read_stdin()\n") == STR
    assert ty('x = read_file("a")\n') == ResultT(STR, STR)
    assert ty('x = write_file("a", "b")\n') == ResultT(INT, STR)
    assert ty('x = append_file("a", "b")\n') == ResultT(INT, STR)
    assert ty('x = file_exists("a")\n') == BOOL
    assert ty('x = list_dir(".")\n') == ResultT(ListT(STR), STR)
    assert ty('x = env("HOME")\n') == OptionT(STR)
    assert ty("x = time()\n") == FLOAT
    assert ty("sleep(0.5)\nx = 1\n") == INT


def test_io_builtin_errors():
    fails("x = argv(1)\n", "takes 0 argument")
    fails("x = input(1)\n", "input expects a str prompt")
    fails('x = input("a", "b")\n', "input takes 0 or 1")
    fails("x = read_file(1)\n", "read_file expects a str")
    fails('x = write_file("a")\n', "write_file takes 2 argument")
    fails("x = write_file(1, 2)\n", "write_file expects \\(path: str, text: str\\)")
    fails('x = env()\n', "env takes 1 argument")
    fails("sleep(1)\n", "sleep expects float seconds")
    fails("eprint(print(1))\n", "produces no value")


def test_a_user_function_may_use_an_io_builtin_name():
    checked("fn time() -> int:\n    return 7\nx = time()\n")
    checked("fn env(k: str) -> str:\n    return k\nx = env(\"a\")\n")


# ---- end to end ---------------------------------------------------------------------------------------

@needs_rustc
def test_program_arguments(tmp_path):
    src = 'args = argv()\nprint(len(args))\nfor a in args:\n    print(a)\n'
    r = run(tmp_path, src, "one", "two words", "--flag")
    assert r.returncode == 0 and r.stdout == "3\none\ntwo words\n--flag\n"
    assert run(tmp_path, src).stdout == "0\n"
    assert run(tmp_path, src, "--", "x").stdout == "1\nx\n"


@needs_rustc
def test_input_prompt_and_read_line_until_eof(tmp_path):
    src = ('name = input("name? ")\nprint(f"hello {name}")\ntotal = 0\nwhile true:\n    line = read_line()\n'
           '    if line is none:\n        break\n    n = parse_int(line)\n    if n is none:\n        continue\n    total += n\nprint(total)\n')
    r = run(tmp_path, src, stdin="Ana\n1\n2\nx\n3\n")
    assert r.returncode == 0 and r.stdout == "name? hello Ana\n6\n"


@needs_rustc
def test_input_handles_windows_line_endings_and_no_final_newline(tmp_path):
    src = 'a = input()\nb = input()\nprint(len(a), len(b), a + b)\n'
    assert run(tmp_path, src, stdin="ab\r\ncd").stdout == "2 2 abcd\n"


@needs_rustc
def test_input_at_end_of_input_stops_with_an_error(tmp_path):
    r = run(tmp_path, 'x = input("? ")\nprint("unreachable")\n', stdin="")
    assert r.returncode == 101 and "end of input" in r.stderr and "unreachable" not in r.stdout


@needs_rustc
def test_read_stdin_reads_everything(tmp_path):
    r = run(tmp_path, 'text = read_stdin()\nprint(len(text.lines()))\nprint(len(text))\n', stdin="a\nb\nc\n")
    assert r.stdout == "3\n6\n"


@needs_rustc
def test_files_round_trip(tmp_path):
    src = ('print(write_file("out.txt", "a\\nb\\n"))\nprint(append_file("out.txt", "c\\n"))\n'
           'print(read_file("out.txt"))\nprint(file_exists("out.txt"), file_exists("nope.txt"))\n'
           'r = read_file("out.txt")\nif r is ok:\n    print(r.lines())\n'
           'print(list_dir("."))\nprint(write_file("é.txt", "héllo"))\n')
    r = run(tmp_path, src)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "Ok(4)", "Ok(2)", 'Ok("a\\nb\\nc\\n")', "true false", '["a", "b", "c"]',
        'Ok(["out.txt", "prog.zy"])', "Ok(5)"]
    assert (tmp_path / "out.txt").read_text() == "a\nb\nc\n"


@needs_rustc
def test_file_errors_are_values_that_mention_the_path(tmp_path):
    src = ('r = read_file("missing.txt")\nif r is err:\n    print("failed:", r)\n'
           'w = write_file("no_such_dir/x.txt", "a")\nprint(w is err)\nprint(list_dir("nope") is err)\n')
    r = run(tmp_path, src)
    assert r.returncode == 0
    assert "failed: missing.txt: " in r.stdout and r.stdout.splitlines()[1:] == ["true", "true"]


@needs_rustc
def test_environment_variables(tmp_path):
    src = 'print(env("ZYLO_TEST_VAR"))\nprint(env("ZYLO_TEST_UNSET_VAR"))\nv = env("ZYLO_TEST_VAR").unwrap_or("default")\nprint(v)\n'
    r = run(tmp_path, src, env={"ZYLO_TEST_VAR": "hello"})
    assert r.stdout == 'Some("hello")\nNone\nhello\n'


@needs_rustc
def test_eprint_goes_to_stderr_and_print_takes_many_values(tmp_path):
    r = run(tmp_path, 'eprint("warn", 1, [2])\neprint()\nprint("out", 2.5)\n')
    assert r.stdout == "out 2.5\n" and r.stderr == "warn 1 [2]\n\n"


@needs_rustc
def test_time_and_sleep_and_exit_code(tmp_path):
    src = 't0 = time()\nsleep(0.05)\nt1 = time()\nprint(t0 > 1000000000.0, t1 - t0 >= 0.04)\nexit(3)\n'
    r = run(tmp_path, src)
    assert r.stdout == "true true\n" and r.returncode == 3
