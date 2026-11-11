import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .codegen import generate
from .errors import LumoError
from .parser import parse


def transpile(path: Path) -> str:
    return generate(parse(path.read_text()))


def compile_rust(rs: str, out: Path) -> None:
    rustc = shutil.which("rustc")
    if not rustc:
        raise LumoError("rustc no encontrado. Instala Rust: https://rustup.rs")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.rs"
        src.write_text(rs)
        r = subprocess.run([rustc, "-O", "-o", str(out), str(src)], capture_output=True, text=True)
        if r.returncode:
            raise LumoError(f"rustc falló:\n{r.stderr}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lumo")
    p.add_argument("cmd", choices=["emit", "build", "run"])
    p.add_argument("file", type=Path)
    p.add_argument("-o", "--out", type=Path)
    a = p.parse_args(argv)
    try:
        rs = transpile(a.file)
        if a.cmd == "emit":
            sys.stdout.write(rs)
            return 0
        out = a.out or a.file.with_suffix("")
        if a.cmd == "run":
            with tempfile.TemporaryDirectory() as tmp:
                exe = Path(tmp) / "prog"
                compile_rust(rs, exe)
                return subprocess.run([str(exe)]).returncode
        compile_rust(rs, out)
        print(f"generado {out}")
        return 0
    except LumoError as e:
        print(f"{a.file}:{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
