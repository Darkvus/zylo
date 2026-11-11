# Benchmark results

Generated with `python benchmarks/bench.py --write`.

| Program | Python | Zylo | Speedup | Zylo without overflow checks | Speedup | Compile time |
|---|---:|---:|---:|---:|---:|---:|
| fib | 1.021 s | 0.037 s | **28×** | 0.022 s | 46× | 0.24 s |
| loop | 1.364 s | 0.016 s | **87×** | 0.015 s | 89× | 0.14 s |
| primes | 0.648 s | 0.019 s | **35×** | 0.019 s | 35× | 0.14 s |
| sort | 0.388 s | 0.004 s | **90×** | 0.004 s | 94× | 0.16 s |

Median of 5 runs, including process start-up. Python 3.12.3 (CPython), rustc 1.99.1 (58cf8f9c3 2026-10-13), x86_64, Linux (WSL2).

The Python versions are written inside functions (the idiomatic and faster style in CPython). The outputs of both languages are compared and must be identical.

Beware of `loop`: it is a trivial arithmetic loop that LLVM can transform very aggressively, so its speedup is an upper bound that is not representative. `fib`, `primes` and `sort` (calls, branches and list access) reflect typical performance better.
