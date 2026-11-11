# Benchmark results

Generated with `python benchmarks/bench.py --write`.

| Program | Python | Zylo | Speedup | Zylo without overflow checks | Speedup | Compile time |
|---|---:|---:|---:|---:|---:|---:|
| fib | 1.015 s | 0.039 s | **26×** | 0.022 s | 47× | 0.15 s |
| loop | 1.284 s | 0.016 s | **79×** | 0.016 s | 82× | 0.15 s |
| primes | 0.649 s | 0.018 s | **35×** | 0.019 s | 35× | 0.14 s |
| sort | 0.355 s | 0.004 s | **82×** | 0.004 s | 85× | 0.17 s |
| listsum | 0.100 s | 0.032 s | **3×** | 0.033 s | 3× | 0.18 s |

Median of 5 runs, including process start-up. Python 3.12.3 (CPython), rustc 1.99.1 (58cf8f9c3 2026-10-13), x86_64, Linux (WSL2).

The Python versions are written inside functions (the idiomatic and faster style in CPython). The outputs of both languages are compared and must be identical.

Beware of `loop`: it is a trivial arithmetic loop that LLVM can transform very aggressively, so its speedup es una cota superior poco representativa. `fib`, `primes` y `sort` (llamadas, ramas y acceso a listas) reflect typical performance better.

`listsum` passes a list of 2 000 strings to a function 1 000 times. Before version 0.6 every call cloned the list, which made this pattern slower than Python (0.112 s); read-only parameters are now lent instead of cloned.
