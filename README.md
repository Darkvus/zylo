# Lumo

Lenguaje con sintaxis tipo Python, tipado estático e inferencia local, que transpila a Rust.
Extensión de archivo: `.lm`. **Estado: en desarrollo temprano (fase 3 de 6).**

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

Hoy funcionan el lexer, el parser y el chequeo de tipos; el codegen completo
están en el [plan](PLAN.md).

## Licencia

MIT
