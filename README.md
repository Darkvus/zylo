# Lumo

Lenguaje con sintaxis tipo Python, tipado estático e inferencia local, que transpila a Rust.
Extensión de archivo: `.lm`. **Estado: en desarrollo temprano (MVP completo, v0.1).**

```
fn fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

print(fib(35))
```

## Uso

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q

lumo check examples/fib.lm        # solo sintaxis y tipos
lumo emit  examples/fib.lm        # ver el Rust generado
lumo run   examples/fib.lm        # compilar con rustc y ejecutar (devuelve el exit code del programa)
lumo build examples/fib.lm -o fib --emit-rs
```

Los errores indican archivo, línea y columna, con el fragmento de código:

```
bad.lm:2:9: error: `+` entre int y float: los tipos deben coincidir (usa float(x) o int(x) para convertir)
  |
2 | print(x + 2.5)
  |         ^
```

Necesita Python 3.11+ y [Rust](https://rustup.rs) para `build`/`run`.
El estado y las decisiones de diseño están en [PLAN.md](PLAN.md).

## Rendimiento

Mismos programas en Lumo y en CPython (mediana de 5 ejecuciones):

| Programa | Python | Lumo | Speedup |
|---|---:|---:|---:|
| fib(35) recursivo | 1.021 s | 0.037 s | **28×** |
| primos < 300 000 | 0.648 s | 0.019 s | **35×** |
| burbuja, 3 000 elementos | 0.388 s | 0.004 s | **90×** |
| bucle aritmético, 20 M | 1.364 s | 0.016 s | 87× (*) |

(*) LLVM optimiza mucho este bucle trivial; es una cota superior. Detalles, método y la variante sin
comprobación de overflow en [benchmarks/RESULTS.md](benchmarks/RESULTS.md). Reproducible con
`python benchmarks/bench.py`.

## Ejemplos

13 programas en [examples/](examples/): FizzBuzz, primos, Collatz, ordenación, búsqueda binaria,
matrices, estadísticas, factoriales… Todos se compilan y verifican en los tests.

## Licencia

MIT
