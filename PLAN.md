# Lumo: plan del transpilador mínimo

Lenguaje Python-like con tipado estático e inferencia local que transpila a Rust.
Extensión de archivo: `.lm`. Objetivo: sintaxis limpia como Python, rendimiento cercano a Rust.

## Decisiones de diseño (fijas para el MVP)
- **Implementación:** Python, lexer y parser recursivo descendente escritos a mano.
- **Backend:** genera Rust y compila con `rustc -O`. No hay optimizador propio.
- **Tipado:** estático con inferencia local. Anotaciones obligatorias solo en parámetros y retornos de `fn`.
- **Memoria:** todo por valor o `Rc` implícito. Sin borrow checker visible; se acepta perder algo de rendimiento.
- **Tipos:** `int` (i64), `float` (f64), `bool`, `str`, `list[T]`.

## Ejemplo objetivo
```
fn fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

print(fib(35))
```

## Estado actual (scaffold, hecho)
- [x] Lexer con INDENT/DEDENT, comentarios, errores con línea/columna (`lumo/lexer.py`)
- [x] AST mínimo: literales, nombres, BinOp, UnaryOp, Call, ExprStmt (`lumo/ast.py`)
- [x] Parser de expresiones con precedencia, paréntesis y llamadas (`lumo/parser.py`)
- [x] Codegen: `print(expr)` → `println!` dentro de `fn main()` (`lumo/codegen.py`)
- [x] CLI: `lumo emit|build|run archivo.lm` (`lumo/cli.py`)
- [x] 5 tests pasando (`tests/test_pipeline.py`)
- [x] **Fase 2 hecha (2026-11-11):** parser/AST completos (assign, `x: T = v`, `+=`, if/elif/else, while, for, break/continue, fn, return, listas, índices, and/or/not, posiciones en nodos), 30 tests
- [ ] **Pendiente para probar `run`/`build`:** instalar Rust (`curl https://sh.rustup.rs -sSf | sh`)

Limitaciones actuales: solo sentencias de expresión (`print(...)`); el lexer ya reconoce
las keywords `fn if else while return let true false` y el token `->`, pero el parser aún no las usa.

## Fases pendientes

| # | Fase | Entregable | Estimación |
|---|------|-----------|-----------|
| 2 | **Parser + AST completo** | `let`, asignación, `if/else`, `while`, `for x in range(...)`, `fn`, `return`, bloques por indentación | 4-5 días |
| 3 | **Chequeo de tipos** | Inferencia local, tabla de símbolos, errores con línea/columna, comprobación de retornos | 4-5 días |
| 4 | **Codegen completo** | AST tipado → Rust: funciones, variables `let`/`let mut`, control de flujo, listas (`Vec<T>`), strings | 3-4 días |
| 5 | **CLI pulida** | Mensajes de error legibles con fragmento de código, `--emit-rs`, ejecución con código de salida | 1-2 días |
| 6 | **Tests + benchmark** | Casos `.lm` con salida esperada; benchmark `fib(35)` vs Python | 2 días |

### Fase 2: detalle
1. Nodos AST nuevos: `Let(name, type?, value)`, `Assign(name, value)`, `If(cond, then, else_)`, `While(cond, body)`, `Return(value?)`, `FnDef(name, params, ret, body)`, `For(var, iter, body)`.
2. `parse_block()`: espera `NEWLINE INDENT stmt+ DEDENT` tras `:`.
3. `parse_stmt()` despacha por keyword; si empieza por IDENT seguido de `=`, es asignación.
4. Añadir `and`, `or`, `not` a keywords y precedencia (menor que comparaciones).
5. Parseo de tipos: `int`, `float`, `bool`, `str`, `list[T]`.
6. Tests por construcción (un test por sentencia + errores de sintaxis con posición).

### Fase 3: detalle
- `types.py` con `Type` (Int, Float, Bool, Str, List(T), Fn(params, ret), Unit).
- Pasada de chequeo sobre el AST que anota cada expresión con su tipo.
- Reglas: sin coerción implícita int↔float, condiciones deben ser `bool`, `return` coherente con el tipo declarado, variables declaradas antes de usar.
- Reasignación: `let x = 1` inmutable por defecto; `let mut x` (o decidir sintaxis) para reasignables. **Decisión abierta.**

### Fase 4: detalle
- Funciones top-level → `fn nombre(a: i64) -> i64 { ... }`.
- Código top-level fuera de `fn` → cuerpo de `main`.
- `for i in range(a, b)` → `for i in a..b`.
- `list[T]` → `Vec<T>`; `str` → `String` (o `&str` para literales, decidir).
- Overflow: Rust en release hace wrapping; decidir si se usa `checked_*` o se acepta.

## Estructura del repo
```
lumo/
  lumo/
    lexer.py      # hecho
    ast.py        # ampliar
    parser.py     # ampliar
    types.py      # fase 3
    codegen.py    # ampliar
    errors.py
    cli.py
  examples/
  tests/
  PLAN.md
  pyproject.toml
```

## Cómo trabajar
- Entorno: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` (ya creado; `pip install -e .`, no poetry).
- Tests: `.venv/bin/pytest -q`
- Ver Rust generado: `.venv/bin/lumo emit examples/hello.lm`
- Avanzar siempre en vertical: cada construcción nueva debe pasar lexer → parser → tipos → codegen → rustc antes de pasar a la siguiente.

## Criterio de éxito del MVP
- `fib(35)` en `.lm` al menos 20× más rápido que en Python.
- Errores de sintaxis y de tipos con línea y columna.
- 10 ejemplos que compilan y pasan tests.

## Fuera de alcance (v2)
Structs/clases, módulos e imports, genéricos, closures, gestor de paquetes, LSP, interop con crates de Rust
(esta última es crítica para adopción real), resaltado de sintaxis en VS Code.

## Decisiones abiertas
- Nombre definitivo del lenguaje (`lumo` es provisional).
- ~~Sintaxis de mutabilidad~~ **Decidido:** estilo Python (`x = 1` declara; reasignar lo detecta el checker; `let` eliminado de keywords).
- ~~Licencia~~ **Decidido:** MIT.
- Modelo de memoria a futuro: `Rc` implícito vs ownership simplificado.
- Manejo de overflow de enteros.
- Nicho objetivo: scripts/CLI, procesamiento de datos o automatización.

## Referencias de lenguajes similares
Mojo, Nim, Crystal, Codon, Vale, Gleam: revisar qué resolvió cada uno antes de decidir el modelo de memoria.
