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
from .errors import BackendError, LumoError
from .parser import parse

# Códigos de salida de la CLI: 0 ok · 1 error de compilación · 2 uso incorrecto (argparse)
# `lumo run` devuelve el código del programa ejecutado.


def transpile_source(src: str) -> str:
    return generate(check(parse(src)))


def transpile(path: Path) -> str:
    return transpile_source(path.read_text())


def compile_rust(rs: str, out: Path, overflow_checks: bool = True) -> None:
    rustc = shutil.which("rustc")
    if not rustc:
        raise BackendError("rustc no encontrado. Instala Rust: https://rustup.rs")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.rs"
        src.write_text(rs)
        cmd = [rustc, "-O", "-o", str(out), str(src)]
        if overflow_checks:
            cmd[2:2] = ["-C", "overflow-checks=yes"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BackendError(
                "rustc rechazó el código generado. Esto es un bug de Lumo; "
                "usa `lumo emit` para ver el Rust y repórtalo.\n" + r.stderr.rstrip()
            )


def exit_code(returncode: int) -> int:
    """Convención de shell: muerte por señal N -> 128 + N."""
    return 128 - returncode if returncode < 0 else returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lumo", description="Lumo: sintaxis tipo Python, tipado estático, compila a Rust.")
    p.add_argument("--version", action="version", version=f"lumo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{check,emit,build,run}")

    def common(sp: argparse.ArgumentParser, backend: bool) -> None:
        sp.add_argument("file", type=Path, help="archivo .lm")
        if backend:
            sp.add_argument("--no-overflow-checks", action="store_true",
                            help="sin comprobación de desbordamiento (más rápido; los enteros dan la vuelta en silencio)")

    common(sub.add_parser("check", help="comprueba sintaxis y tipos, sin compilar"), backend=False)
    common(sub.add_parser("emit", help="imprime el Rust generado"), backend=False)
    b = sub.add_parser("build", help="compila a un ejecutable nativo")
    common(b, backend=True)
    b.add_argument("-o", "--out", type=Path, help="ejecutable de salida (por defecto: el nombre del archivo sin extensión)")
    b.add_argument("--emit-rs", action="store_true", help="guarda también el Rust generado junto al ejecutable (<salida>.rs)")
    common(sub.add_parser("run", help="compila y ejecuta; devuelve el código de salida del programa"), backend=True)
    return p


def use_color() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        source = a.file.read_text()
    except OSError as e:
        print(f"lumo: no se puede leer {a.file}: {e.strerror}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print(f"lumo: {a.file} no es texto UTF-8 válido", file=sys.stderr)
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
            raise BackendError(f"la salida {out} sobrescribiría el archivo fuente; usa -o")
        compile_rust(rs, out, checks)
        if a.emit_rs:
            out.with_name(out.name + ".rs").write_text(rs)
        print(f"generado {out}")
        return 0
    except LumoError as e:
        print(format_error(e, source, str(a.file), use_color()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
