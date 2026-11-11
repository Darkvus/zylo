import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .checker import check
from .codegen import generate
from .diagnostics import format_error
from .errors import BackendError, ZyloError
from .parser import parse

# CLI exit codes: 0 ok · 1 compile error · 2 incorrect usage (argparse)
# `zylo run` returns the exit code of the program it ran.


def transpile_source(src: str) -> str:
    return generate(check(parse(src)))


def transpile(path: Path) -> str:
    return transpile_source(path.read_text())


def compile_rust(rs: str, out: Path, overflow_checks: bool = True) -> None:
    rustc = shutil.which("rustc")
    if not rustc:
        raise BackendError("rustc not found. Install Rust: https://rustup.rs")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.rs"
        src.write_text(rs)
        cmd = [rustc, "-O", "-o", str(out), str(src)]
        if overflow_checks:
            cmd[2:2] = ["-C", "overflow-checks=yes"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BackendError(
                "rustc rejected the generated code. This is a Zylo bug; "
                "use `zylo emit` to see the Rust and report it.\n" + r.stderr.rstrip()
            )


def exit_code(returncode: int) -> int:
    """Shell convention: killed by signal N -> 128 + N."""
    return 128 - returncode if returncode < 0 else returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zylo", description="Zylo: Python-like syntax, static typing, compiles to Rust.")
    p.add_argument("--version", action="version", version=f"zylo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{check,emit,build,run}")

    def common(sp: argparse.ArgumentParser, backend: bool) -> None:
        sp.add_argument("file", type=Path, help=".zy source file")
        if backend:
            sp.add_argument("--no-overflow-checks", action="store_true",
                            help="no integer overflow checks (faster; integers wrap around silently)")

    common(sub.add_parser("check", help="check syntax and types without compiling"), backend=False)
    common(sub.add_parser("emit", help="print the generated Rust"), backend=False)
    b = sub.add_parser("build", help="compile to a native executable")
    common(b, backend=True)
    b.add_argument("-o", "--out", type=Path, help="output executable (default: the file name without extension)")
    b.add_argument("--emit-rs", action="store_true", help="also save the generated Rust next to the executable (<output>.rs)")
    common(sub.add_parser("run", help="compile and run; returns the program's exit code"), backend=True)
    return p


def use_color() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        source = a.file.read_text()
    except OSError as e:
        print(f"zylo: cannot read {a.file}: {e.strerror}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print(f"zylo: {a.file} is not valid UTF-8 text", file=sys.stderr)
        return 1

    try:
        if a.cmd == "check":
            check(parse(source))
            return 0
        rs = transpile_source(source)
        if a.cmd == "emit":
            sys.stdout.write(rs)
            return 0
        checks = not a.no_overflow_checks
        if a.cmd == "run":
            with tempfile.TemporaryDirectory() as tmp:
                exe = Path(tmp) / "prog"
                compile_rust(rs, exe, checks)
                sys.stdout.flush()
                return exit_code(subprocess.run([str(exe)]).returncode)
        out = a.out or a.file.with_suffix("")
        if out.resolve() == a.file.resolve():
            raise BackendError(f"output {out} would overwrite the source file; use -o")
        compile_rust(rs, out, checks)
        if a.emit_rs:
            out.with_name(out.name + ".rs").write_text(rs)
        print(f"generado {out}")
        return 0
    except ZyloError as e:
        print(format_error(e, source, str(a.file), use_color()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
