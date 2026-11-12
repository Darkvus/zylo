#!/usr/bin/env python3
"""Benchmark Zylo against Python (CPython) and hand-written Rust.

Each program has equivalent `X.zy`, `X.py` and `X.rs` files. The script compiles every .zy and .rs once (with
and without overflow checks, the same `rustc -O` flags for both), verifies that all outputs match, times each
executable N times and reports the median. Process start-up is included everywhere.
Usage:  python benchmarks/bench.py [--runs 5] [--write]
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

from zylo.application.compile import transpile
from zylo.infrastructure.rustc import compile_rust

HERE = Path(__file__).parent
PROGRAMS = ["fib", "loop", "primes", "sort", "listsum"]
NOTE = (
    "Beware of `loop`: it is a trivial arithmetic loop that LLVM can transform very aggressively, so its "
    "speedup es una cota superior poco representativa. `fib`, `primes` y `sort` (llamadas, ramas y acceso a listas) "
    "reflect typical performance better.\n\n"
    "`listsum` is the one place where Zylo is clearly behind hand-written Rust: a `for` over a list of strings "
    "emits `.iter().cloned()`, which allocates a copy of every string it visits, while the Rust program borrows "
    "them. Iterating by reference when the loop variable is never modified is a known improvement.\n"
)


def run_once(cmd: list[str]) -> tuple[float, str]:
    """Run a command once.

    Args:
        cmd: The command line.

    Returns:
        The elapsed seconds and the command's standard output.
    """
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if r.returncode:
        sys.exit(f"failed {cmd}: {r.stderr}")
    return dt, r.stdout


def measure(cmd: list[str], runs: int) -> tuple[float, float, str]:
    """Run a command several times.

    Args:
        cmd: The command line.
        runs: How many times.

    Returns:
        The best time, the median time and the last output.
    """
    times, out = [], ""
    for _ in range(runs):
        dt, out = run_once(cmd)
        times.append(dt)
    return min(times), statistics.median(times), out


def main() -> int:
    """Run the benchmark suite and print (and optionally write) the results table.

    Returns:
        The exit code.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--write", action="store_true", help="escribe benchmarks/RESULTS.md")
    a = ap.parse_args()
    if not shutil.which("rustc"):
        sys.exit("rustc no encontrado")

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in PROGRAMS:
            rs = transpile(HERE / f"{name}.zy")
            t0 = time.perf_counter()
            compile_rust(rs, Path(tmp) / f"{name}_chk", overflow_checks=True)
            compile_t = time.perf_counter() - t0
            compile_rust(rs, Path(tmp) / f"{name}_raw", overflow_checks=False)
            rust_src = (HERE / f"{name}.rs").read_text()
            compile_rust(rust_src, Path(tmp) / f"{name}_rust", overflow_checks=True)
            compile_rust(rust_src, Path(tmp) / f"{name}_rustraw", overflow_checks=False)

            py_best, py_med, py_out = measure([sys.executable, str(HERE / f"{name}.py")], a.runs)
            chk_best, chk_med, chk_out = measure([str(Path(tmp) / f"{name}_chk")], a.runs)
            raw_best, raw_med, raw_out = measure([str(Path(tmp) / f"{name}_raw")], a.runs)
            rust_best, rust_med, rust_out = measure([str(Path(tmp) / f"{name}_rust")], a.runs)
            rawr_best, rawr_med, rawr_out = measure([str(Path(tmp) / f"{name}_rustraw")], a.runs)
            if not (py_out == chk_out == raw_out == rust_out == rawr_out):
                sys.exit(f"{name}: outputs differ\npython: {py_out!r}\nzylo:   {chk_out!r}\nraw:    {raw_out!r}\n"
                         f"rust:   {rust_out!r}\nrust raw: {rawr_out!r}")
            rows.append((name, py_med, chk_med, raw_med, rust_med, rawr_med, compile_t))
            print(f"{name:8} ok  python {py_med:.3f}s  rust {rust_med:.3f}s  zylo {chk_med:.3f}s  "
                  f"zylo (no checks) {raw_med:.3f}s  rust (no checks) {rawr_med:.3f}s", flush=True)

    lines = [
        "| Program | Python | Rust | Zylo | Zylo vs Python | Zylo vs Rust | Rust (no checks) | Zylo (no checks) | Zylo vs Rust (no checks) | Compile time |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, py, chk, raw, rust, rawr, ct in rows:
        lines.append(f"| {name} | {py:.3f} s | {rust:.3f} s | {chk:.3f} s | **{py / chk:.0f}×** | {chk / rust:.2f}× | "
                     f"{rawr:.3f} s | {raw:.3f} s | {raw / rawr:.2f}× | {ct:.2f} s |")
    rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout.strip()
    meta = (f"Median of {a.runs} runs, including process start-up. "
            f"Python {platform.python_version()} (CPython), {rustc}, {platform.machine()}, "
            f"{platform.system()} ({'WSL2' if 'microsoft' in platform.release().lower() else platform.release()}).")
    table = "\n".join(lines)
    print("\n" + table + "\n\n" + meta)
    if a.write:
        (HERE / "RESULTS.md").write_text(
            "# Benchmark results\n\nGenerated with `python benchmarks/bench.py --write`.\n\n"
            + table + "\n\n" + meta + "\n\n"
            "The Python versions are written inside functions (the idiomatic and faster style in CPython). "
            "The Rust versions are idiomatic hand-written programs with the same algorithms, built with the same "
            "`rustc -O` flags as the code Zylo generates (default: overflow checks on). "
            "The outputs of all languages are compared and must be identical. "
            "`Zylo vs Rust` is Zylo's time divided by Rust's: 1.00× means the same speed.\n\n"
            + NOTE
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
