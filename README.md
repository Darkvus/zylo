# Lumo

Lenguaje con sintaxis tipo Python, tipado estático e inferencia local, que transpila a Rust.
Extensión de archivo: `.lm`. **Estado: en desarrollo temprano (fase 5 de 6).**

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

## Licencia

MIT
