# Benchmark results

Generated with `python benchmarks/bench.py --write`.

| Program | Python | Rust | Zylo | Zylo vs Python | Zylo vs Rust | Rust (no checks) | Zylo (no checks) | Zylo vs Rust (no checks) | Compile time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fib | 0.919 s | 0.035 s | 0.035 s | **26×** | 1.00× | 0.021 s | 0.021 s | 1.01× | 0.15 s |
| loop | 1.181 s | 0.015 s | 0.015 s | **77×** | 0.99× | 0.015 s | 0.015 s | 0.99× | 0.15 s |
| primes | 0.594 s | 0.018 s | 0.018 s | **33×** | 0.99× | 0.018 s | 0.018 s | 1.01× | 0.15 s |
| sort | 0.326 s | 0.004 s | 0.004 s | **76×** | 1.17× | 0.004 s | 0.004 s | 1.18× | 0.17 s |
| listsum | 0.088 s | 0.007 s | 0.031 s | **3×** | 4.25× | 0.007 s | 0.031 s | 4.42× | 0.17 s |

Median of 7 runs, including process start-up. Python 3.12.3 (CPython), rustc 1.99.1 (58cf8f9c3 2026-10-13), x86_64, Linux (WSL2).

The Python versions are written inside functions (the idiomatic and faster style in CPython). The Rust versions are idiomatic hand-written programs with the same algorithms, built with the same `rustc -O` flags as the code Zylo generates (default: overflow checks on). The outputs of all languages are compared and must be identical. `Zylo vs Rust` is Zylo's time divided by Rust's: 1.00× means the same speed.

Beware of `loop`: it is a trivial arithmetic loop that LLVM can transform very aggressively, so its speedup es una cota superior poco representativa. `fib`, `primes` y `sort` (llamadas, ramas y acceso a listas) reflect typical performance better.

`listsum` is the one place where Zylo is clearly behind hand-written Rust: a `for` over a list of strings emits `.iter().cloned()`, which allocates a copy of every string it visits, while the Rust program borrows them. Iterating by reference when the loop variable is never modified is a known improvement.
