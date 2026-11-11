"""Tests de extremo a extremo: .lm -> Rust -> rustc -> ejecución, comparando la salida.

Cada `X.lm` con `X.out` en `tests/programs/` y en `examples/` debe imprimir exactamente ese contenido.
(Las salidas de `examples/` se calcularon con implementaciones independientes en Python.)
Se omiten si `rustc` no está instalado.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from lumo.cli import compile_rust, transpile

PROGRAMS = Path(__file__).parent / "programs"
EXAMPLES = Path(__file__).parent.parent / "examples"

pytestmark = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc no instalado")


def build(lm: Path, tmp_path: Path) -> Path:
    exe = tmp_path / "prog"
    compile_rust(transpile(lm), exe)
    return exe


CASES = sorted(PROGRAMS.glob("*.out")) + sorted(EXAMPLES.glob("*.out"))


@pytest.mark.parametrize("lm", CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_program_output(lm: Path, tmp_path: Path):
    src = lm.with_suffix(".lm")
    exe = build(src, tmp_path)
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout == lm.read_text()


def test_integer_overflow_panics(tmp_path: Path):
    exe = build(PROGRAMS / "overflow.lm", tmp_path)
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 101
    assert "overflow" in r.stderr


BENCHMARKS = Path(__file__).parent.parent / "benchmarks"


@pytest.mark.parametrize("lm", sorted(BENCHMARKS.glob("*.lm")), ids=lambda p: p.stem)
def test_benchmark_programs_still_compile(lm: Path, tmp_path: Path):
    build(lm, tmp_path)  # solo compila; ejecutarlos tarda: usa benchmarks/bench.py
