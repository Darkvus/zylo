import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from . import ast
from . import env as envmod
from . import pkgcmds as pkg
from .checker import check
from .codegen import generate
from .diagnostics import format_error
from .errors import BackendError, PackageError, ZyloError
from .linker import Linker
from .parser import parse

# CLI exit codes: 0 ok · 1 compile error · 2 incorrect usage (argparse)
# `zylo run` returns the exit code of the program it ran.


def transpile_source(src: str) -> str:
    """Compile a single self-contained source string (no imports)."""
    prog = parse(src)
    if any(isinstance(s, (ast.Import, ast.FromImport)) for s in prog.body):
        raise ZyloError("imports need a source file; compile a file instead of a string")
    return generate(check(prog))


def load_program(path: Path, env: str | Path | None = None) -> ast.Program:
    """Parse a file and everything it imports (using the active environment, if any)."""
    active = envmod.discover(path.resolve().parent, env)
    return Linker(active.packages_dir if active else None).link(path)


def transpile(path: Path, env: str | Path | None = None) -> str:
    return generate(check(load_program(path, env)))


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
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{check,emit,build,run,init,venv,add,remove,install,update,list}")

    def common(sp: argparse.ArgumentParser, backend: bool) -> None:
        sp.add_argument("file", type=Path, help=".zy source file")
        sp.add_argument("--env", metavar="DIR", help="virtual environment to use (default: $ZYLO_ENV or the nearest .zylo-env)")
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

    def env_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--env", metavar="DIR", help="virtual environment to use (default: $ZYLO_ENV or the nearest .zylo-env)")

    sp = sub.add_parser("init", help="create zylo.toml and src/main.zy in the current directory")
    sp.add_argument("name", nargs="?", help="package name (default: the directory name)")
    sp = sub.add_parser("venv", help="create a virtual environment (default: ./.zylo-env)")
    sp.add_argument("path", nargs="?", help="where to create it")
    sp.add_argument("--force", action="store_true", help="recreate it if it already exists")
    sp = sub.add_parser("add", help="add a dependency to zylo.toml and install it")
    sp.add_argument("source", help="host/user/repo[@ref], a git URL[@ref], or with --path a directory")
    sp.add_argument("--name", help="dependency name, if the package does not declare one")
    sp.add_argument("--path", dest="as_path", action="store_true", help="the source is a local directory")
    env_arg(sp)
    sp = sub.add_parser("remove", help="remove a dependency")
    sp.add_argument("name")
    env_arg(sp)
    env_arg(sub.add_parser("install", help="make the environment match zylo.toml and zylo.lock"))
    env_arg(sub.add_parser("update", help="re-resolve refs and rewrite zylo.lock"))
    env_arg(sub.add_parser("list", help="list the installed packages"))
    return p


PACKAGE_COMMANDS = {"init", "venv", "add", "remove", "install", "update", "list"}


def package_main(a: argparse.Namespace) -> int:
    cwd = Path.cwd()
    try:
        match a.cmd:
            case "init":
                return pkg.cmd_init(cwd, a.name)
            case "venv":
                return pkg.cmd_venv(cwd, a.path, a.force)
            case "add":
                return pkg.cmd_add(cwd, a.source, a.name, a.as_path, a.env)
            case "remove":
                return pkg.cmd_remove(cwd, a.name, a.env)
            case "install":
                return pkg.cmd_install(cwd, a.env)
            case "update":
                return pkg.cmd_install(cwd, a.env, update=True)
            case "list":
                return pkg.cmd_list(cwd, a.env)
    except PackageError as e:
        print(f"zylo: error: {e.msg}", file=sys.stderr)
        return 1
    return 2


def read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def use_color() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd in PACKAGE_COMMANDS:
        return package_main(a)
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
            check(load_program(a.file, a.env))
            return 0
        rs = transpile(a.file, a.env)
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
        print(f"built {out}")
        return 0
    except PackageError as e:
        print(f"zylo: error: {e.msg}", file=sys.stderr)
        return 1
    except ZyloError as e:
        shown = e.file or str(a.file)
        text = source if shown == str(a.file) else read_text_or_none(Path(shown))
        print(format_error(e, text, shown, use_color()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
