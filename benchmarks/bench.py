#!/usr/bin/env python3
"""Benchmark Lumo vs Python (CPython).

Cada programa tiene una versión `X.lm` y una `X.py` equivalentes. El script compila los .lm una
vez (con y sin overflow-checks), comprueba que las salidas coinciden con las de Python, mide cada
ejecutable N veces y muestra la mejor marca y la mediana. Se incluye el arranque del proceso en
ambos lados. Uso:  python benchmarks/bench.py [--runs 5] [--write]
"""
import argparse
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lumo.cli import compile_rust, transpile

HERE = Path(__file__).parent
PROGRAMS = ["fib", "loop", "primes", "sort"]
NOTE = (
    "Ojo con `loop`: es un bucle aritmético trivial que LLVM puede transformar de forma muy agresiva, así que su "
    "speedup es una cota superior poco representativa. `fib`, `primes` y `sort` (llamadas, ramas y acceso a listas) "
    "reflejan mejor el rendimiento típico.\n"
)


def run_once(cmd: list[str]) -> tuple[float, str]:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if r.returncode:
        sys.exit(f"falló {cmd}: {r.stderr}")
    return dt, r.stdout


def measure(cmd: list[str], runs: int) -> tuple[float, float, str]:
    times, out = [], ""
    for _ in range(runs):
        dt, out = run_once(cmd)
        times.append(dt)
    return min(times), statistics.median(times), out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--write", action="store_true", help="escribe benchmarks/RESULTS.md")
    a = ap.parse_args()
    if not shutil.which("rustc"):
        sys.exit("rustc no encontrado")

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in PROGRAMS:
            rs = transpile(HERE / f"{name}.lm")
            t0 = time.perf_counter()
            compile_rust(rs, Path(tmp) / f"{name}_chk", overflow_checks=True)
            compile_t = time.perf_counter() - t0
            compile_rust(rs, Path(tmp) / f"{name}_raw", overflow_checks=False)

            py_best, py_med, py_out = measure([sys.executable, str(HERE / f"{name}.py")], a.runs)
            chk_best, chk_med, chk_out = measure([str(Path(tmp) / f"{name}_chk")], a.runs)
            raw_best, raw_med, raw_out = measure([str(Path(tmp) / f"{name}_raw")], a.runs)
            if not (py_out == chk_out == raw_out):
                sys.exit(f"{name}: las salidas difieren\npython: {py_out!r}\nlumo:   {chk_out!r}\nraw:    {raw_out!r}")
            rows.append((name, py_med, chk_med, raw_med, compile_t))
            print(f"{name:8} ok  python {py_med:.3f}s  lumo {chk_med:.3f}s  lumo(sin checks) {raw_med:.3f}s", flush=True)

    lines = [
        "| Programa | Python | Lumo | Speedup | Lumo sin overflow-checks | Speedup | Compilación |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, py, chk, raw, ct in rows:
        lines.append(f"| {name} | {py:.3f} s | {chk:.3f} s | **{py / chk:.0f}×** | {raw:.3f} s | {py / raw:.0f}× | {ct:.2f} s |")
    rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout.strip()
    meta = (f"Mediana de {a.runs} ejecuciones, incluye el arranque del proceso. "
            f"Python {platform.python_version()} (CPython), {rustc}, {platform.machine()}, "
            f"{platform.system()} ({'WSL2' if 'microsoft' in platform.release().lower() else platform.release()}).")
    table = "\n".join(lines)
    print("\n" + table + "\n\n" + meta)
    if a.write:
        (HERE / "RESULTS.md").write_text(
            "# Resultados del benchmark\n\nGenerado con `python benchmarks/bench.py --write`.\n\n"
            + table + "\n\n" + meta + "\n\n"
            "Las versiones de Python están escritas dentro de funciones (lo idiomático y más rápido en CPython). "
            "Las salidas de ambos lenguajes se comparan y deben ser idénticas.\n\n"
            + NOTE
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
