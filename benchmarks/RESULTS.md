# Resultados del benchmark

Generado con `python benchmarks/bench.py --write`.

| Programa | Python | Zylo | Speedup | Zylo sin overflow-checks | Speedup | Compilación |
|---|---:|---:|---:|---:|---:|---:|
| fib | 1.021 s | 0.037 s | **28×** | 0.022 s | 46× | 0.24 s |
| loop | 1.364 s | 0.016 s | **87×** | 0.015 s | 89× | 0.14 s |
| primes | 0.648 s | 0.019 s | **35×** | 0.019 s | 35× | 0.14 s |
| sort | 0.388 s | 0.004 s | **90×** | 0.004 s | 94× | 0.16 s |

Mediana de 5 ejecuciones, incluye el arranque del proceso. Python 3.12.3 (CPython), rustc 1.99.1 (58cf8f9c3 2026-10-13), x86_64, Linux (WSL2).

Las versiones de Python están escritas dentro de funciones (lo idiomático y más rápido en CPython). Las salidas de ambos lenguajes se comparan y deben ser idénticas.

Ojo con `loop`: es un bucle aritmético trivial que LLVM puede transformar de forma muy agresiva, así que su speedup es una cota superior poco representativa. `fib`, `primes` y `sort` (llamadas, ramas y acceso a listas) reflejan mejor el rendimiento típico.
