"""Command-line interface: `zylo check|emit|build|run`, and the package commands.

Exit codes: 0 on success, 1 for a compile or package error, 2 for incorrect usage, and for `zylo run` the program's
own exit code (101 when it stops on a runtime error).
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from zylo import __version__
from zylo.application import package_commands as pkg
from zylo.application.compile import build_executable, compile_file, diagnose_source
from zylo.application.format import check_line_length, configured_line_length, format_paths, format_text
from zylo.domain.errors import BackendError, PackageError, ZyloError
from zylo.interface import lsp
from zylo.interface.diagnostics import format_errors

# CLI exit codes: 0 ok · 1 compile error · 2 incorrect usage (argparse)
# `zylo run` returns the exit code of the program it ran.


def exit_code(returncode: int) -> int:
    """Shell convention: killed by signal N -> 128 + N."""
    return 128 - returncode if returncode < 0 else returncode


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every subcommand.

    Returns:
        The parser.
    """
    p = argparse.ArgumentParser(prog="zylo", description="Zylo: Python-like syntax, static typing, compiles to Rust.")
    p.add_argument("--version", action="version", version=f"zylo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{check,emit,build,run,fmt,lsp,init,venv,add,remove,install,update,list}")

    def common(sp: argparse.ArgumentParser, backend: bool) -> None:
        sp.add_argument("file", type=Path, help=".zy source file")
        sp.add_argument("--env", metavar="DIR", help="virtual environment to use (default: $ZYLO_ENV or the nearest .zylo-env)")
        if backend:
            sp.add_argument("--offline", action="store_true", help="build with Cargo without downloading crates")
            sp.add_argument("--no-overflow-checks", action="store_true",
                            help="no integer overflow checks (faster; integers wrap around silently)")

    common(sub.add_parser("check", help="check syntax and types without compiling"), backend=False)
    common(sub.add_parser("emit", help="print the generated Rust"), backend=False)
    b = sub.add_parser("build", help="compile to a native executable")
    common(b, backend=True)
    b.add_argument("-o", "--out", type=Path, help="output executable (default: the file name without extension)")
    b.add_argument("--emit-rs", action="store_true", help="also save the generated Rust next to the executable (<output>.rs)")
    run = sub.add_parser("run", help="compile and run; returns the program's exit code")
    common(run, backend=True)
    run.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the program (see argv())")

    def env_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--env", metavar="DIR", help="virtual environment to use (default: $ZYLO_ENV or the nearest .zylo-env)")

    sp = sub.add_parser("fmt", help="format .zy files in place (directories are searched recursively)")
    sp.add_argument("paths", nargs="+", type=Path, help="files or directories")
    sp.add_argument("--check", action="store_true", help="change nothing; exit 1 if any file would be reformatted")
    sp.add_argument("--stdout", action="store_true", help="print the formatted text of one file instead of rewriting it")
    sp.add_argument("--line-length", type=int, metavar="N",
                    help="wrap lines longer than N (default: [format] line-length in zylo.toml, or 88)")
    sub.add_parser("lsp", help="run the language server (Language Server Protocol over stdin/stdout) for editors")
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


def format_main(a: argparse.Namespace) -> int:
    """Run `zylo fmt`.

    Args:
        a: The parsed arguments.

    Returns:
        0 on success; 1 if a file has a syntax error, or with `--check` if any file would change.
    """
    try:
        if a.line_length is not None:
            check_line_length(a.line_length, "--line-length")
    except ZyloError as e:
        print(f"zylo: {e}", file=sys.stderr)
        return 2
    if a.stdout:
        if len(a.paths) != 1 or not a.paths[0].is_file():
            print("zylo: --stdout needs exactly one file", file=sys.stderr)
            return 2
        try:
            width = a.line_length or configured_line_length(a.paths[0])
            sys.stdout.write(format_text(a.paths[0].read_text(), width))
        except ZyloError as e:
            print(format_errors([e], str(a.paths[0]), a.paths[0].read_text(), use_color()), file=sys.stderr)
            return 1
        return 0
    result = format_paths(a.paths, check=a.check, line_length=a.line_length)
    for e in result.errors:
        print(format_errors([e], e.file or "", None, use_color()), file=sys.stderr)
    for p in result.changed:
        print(f"{'would reformat' if a.check else 'formatted'} {p}")
    done = len(result.changed) + len(result.unchanged)
    print(f"{len(result.changed)} file(s) {'would be ' if a.check else ''}changed, {len(result.unchanged)} already formatted"
          if done else "no .zy files found", file=sys.stderr if result.errors else sys.stdout)
    return 1 if result.errors or (a.check and result.changed) else 0


def package_main(a: argparse.Namespace) -> int:
    """Run a package command (`init`, `venv`, `add`, `remove`, `install`, `update`, `list`).

    Args:
        a: The parsed arguments.

    Returns:
        The exit code.
    """
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


def use_color() -> bool:
    """Whether to colour output: standard error is a terminal and `NO_COLOR` is not set."""
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def main(argv: list[str] | None = None) -> int:
    """Run the `zylo` command.

    Args:
        argv: The arguments (without the program name); defaults to `sys.argv`.

    Returns:
        The process exit code.
    """
    a = build_parser().parse_args(argv)
    if a.cmd in PACKAGE_COMMANDS:
        return package_main(a)
    if a.cmd == "fmt":
        return format_main(a)
    if a.cmd == "lsp":
        return lsp.main()
    try:
        source = a.file.read_text()
    except OSError as e:
        print(f"zylo: cannot read {a.file}: {e.strerror}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print(f"zylo: {a.file} is not valid UTF-8 text", file=sys.stderr)
        return 1

    try:
        errors = diagnose_source(source, a.file, a.env)  # every mistake, not only the first
        if errors:
            print(format_errors(errors, str(a.file), source, use_color()), file=sys.stderr)
            return 1
        if a.cmd == "check":
            return 0
        rs, crates, manifest = compile_file(a.file, a.env)
        if a.cmd == "emit":
            sys.stdout.write(rs)
            return 0
        checks = not a.no_overflow_checks
        if a.cmd == "run":
            with tempfile.TemporaryDirectory() as tmp:
                exe = Path(tmp) / "prog"
                build_executable(rs, crates, manifest, exe, checks, a.offline)
                sys.stdout.flush()
                prog_args = [x for x in a.args if x != "--"][:] if a.args[:1] != ["--"] else a.args[1:]
                return exit_code(subprocess.run([str(exe), *prog_args]).returncode)
        out = a.out or a.file.with_suffix("")
        if out.resolve() == a.file.resolve():
            raise BackendError(f"output {out} would overwrite the source file; use -o")
        build_executable(rs, crates, manifest, out, checks, a.offline)
        if a.emit_rs:
            out.with_name(out.name + ".rs").write_text(rs)
        print(f"built {out}")
        return 0
    except PackageError as e:
        print(f"zylo: error: {e.msg}", file=sys.stderr)
        return 1
    except ZyloError as e:
        print(format_errors([e], str(a.file), source, use_color()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
