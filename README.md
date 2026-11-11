# Lumo

Lenguaje con sintaxis tipo Python, tipado estático e inferencia local, que transpila a Rust.
Extensión de archivo: `.lm`. **Estado: en desarrollo temprano (fase 4 de 6).**

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
.venv/bin/lumo emit examples/hello.lm   # ver el Rust generado
.venv/bin/lumo run examples/hello.lm    # compilar con rustc y ejecutar
```

Hoy funciona el pipeline completo (lexer, parser, tipos, codegen y ejecución con rustc); faltan la CLI pulida y el benchmark, ver
[PLAN.md](PLAN.md).

## Licencia

MIT
