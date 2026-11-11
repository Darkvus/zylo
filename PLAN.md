# Zylo: plan del transpilador mínimo

Lenguaje Python-like con tipado estático e inferencia local que transpila a Rust.
Extensión de archivo: `.zy`. Objetivo: sintaxis limpia como Python, rendimiento cercano a Rust.

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
- [x] Lexer con INDENT/DEDENT, comentarios, errores con línea/columna (`zylo/lexer.py`)
- [x] AST mínimo: literales, nombres, BinOp, UnaryOp, Call, ExprStmt (`zylo/ast.py`)
- [x] Parser de expresiones con precedencia, paréntesis y llamadas (`zylo/parser.py`)
- [x] Codegen: `print(expr)` → `println!` dentro de `fn main()` (`zylo/codegen.py`)
- [x] CLI: `zylo emit|build|run archivo.zy` (`zylo/cli.py`)
- [x] 5 tests pasando (`tests/test_pipeline.py`)
- [x] **Fase 2 hecha (2026-11-11):** parser/AST completos (assign, `x: T = v`, `+=`, if/elif/else, while, for, break/continue, fn, return, listas, índices, and/or/not, posiciones en nodos), 30 tests
- [x] **Fase 3 hecha (2026-11-11):** `zylo/types.py` + `zylo/checker.py` (inferencia local, ámbitos de bloque, declarar vs reasignar, mutabilidad marcada en `VarInfo.mutable`, errores con línea/columna), 75 tests. Conectado en el CLI (`parse → check → generate`).
- [x] Rust 1.99.1 instalado; `zylo run examples/hello.zy` funciona de punta a punta.
- [x] **Fase 4 hecha (2026-11-11):** `zylo/codegen.py` completo (fn, let/let mut, if/elif/else, while, for range/list, listas, strings, conversiones, builtins). 10 programas e2e en `tests/programs/` (`.zy` + `.out`) compilados con rustc real + test de panic por overflow. 85 tests.
- [x] **Fase 5 hecha (2026-11-11):** `zylo/diagnostics.py` (errores `archivo:línea:col: error: msg` + fragmento + `^`, color solo en TTY y sin `NO_COLOR`), CLI con subcomandos `check|emit|build|run`, `build --emit-rs`, `--no-overflow-checks`, `--version`, `run` devuelve el exit code del programa (señal → 128+N), builtin `exit(int)`, errores del backend (`BackendError`) separados de los del usuario. Mensajes de parseo legibles y posición real al final del archivo. 113 tests.
- [x] **Fase 6 hecha (2026-11-11) — MVP completo.** 13 ejemplos documentados en `examples/` (salidas calculadas con Python independiente), benchmark reproducible en `benchmarks/` (`python benchmarks/bench.py --write` → `RESULTS.md`).
- [x] **v0.2a hecho (2026-11-11): structs y métodos.** Renombrado Lumo → Zylo (extensión `.zy`). `struct` con campos, constructor posicional, acceso y asignación de campos (`p.x`, `a.b.c = v`, `xs[i].x = v`), métodos con `self` inferidos como `&self`/`&mut self` (punto fijo para propagación entre métodos), tipos por valor, `print(p)` → `Point(x=1.0, y=2.0)`. 171+ tests, 3 ejemplos nuevos. Ver ROADMAP.md.
- Repo privado: https://github.com/Darkvus/zylo

Limitaciones actuales: solo sentencias de expresión (`print(...)`); el lexer ya reconoce
las keywords `fn if else while return let true false` y el token `->`, pero el parser aún no las usa.

## Fases pendientes

| # | Fase | Entregable | Estimación |
|---|------|-----------|-----------|
| 2 | **Parser + AST completo** | `let`, asignación, `if/else`, `while`, `for x in range(...)`, `fn`, `return`, bloques por indentación | 4-5 días |
| 3 | **Chequeo de tipos** | Inferencia local, tabla de símbolos, errores con línea/columna, comprobación de retornos | 4-5 días |
| 4 | **Codegen completo** | AST tipado → Rust: funciones, variables `let`/`let mut`, control de flujo, listas (`Vec<T>`), strings | 3-4 días |
| 5 | **CLI pulida** | Mensajes de error legibles con fragmento de código, `--emit-rs`, ejecución con código de salida | 1-2 días |
| 6 | **Tests + benchmark** | Casos `.zy` con salida esperada; benchmark `fib(35)` vs Python | 2 días |

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

### Decisiones tomadas en la fase 3
- Sin coerción; conversiones explícitas `int()`, `float()`, `str()`. Builtins: `print len int float str append range` (range solo en `for`).
- `int / int` es división entera (como Rust). Sin overflow checking por ahora.
- Ámbito de bloque; las funciones no ven variables globales; funciones solo a nivel superior, con firma visible antes de su definición.
- El checker anota `node.ty` en expresiones, `Assign.declares/var` y `Param.info`/`For.info` (con `mutable`) para que el codegen emita `let` / `let mut`.
- Lista vacía requiere anotación (`xs: list[int] = []`) salvo si el contexto da el tipo (argumento, retorno, reasignación).

### Decisiones tomadas en la fase 4
- `str` → `String`, `list[T]` → `Vec<T>`. Leer una variable no-Copy como valor emite `.clone()`; lecturas puras (len, print, índices, comparaciones, `range`) toman referencia. `for x in xs` clona la lista una vez (permite mutarla dentro, como Python).
- **Overflow:** rustc se invoca con `-O -C overflow-checks=yes` → panic (exit 101) en vez de wrapping. Coste medido ≈1.6× en fib; aun así ≈27× más rápido que Python. Pendiente: flag para desactivar.
- `float` se imprime con `{:?}` (`2.0`); listas con `{:?}` (las listas de str salen con comillas dobles, distinto de Python). `len(str)` cuenta caracteres, no bytes.
- Identificadores que son keywords de Rust (`type`, `match`, `main`...) se emiten con sufijo `_`.
- Limitación conocida: pasar una lista a una función la clona (sin préstamos todavía). Optimización candidata para v2.

### Decisiones tomadas en la fase 5
- Exit codes de la CLI: 0 ok · 1 error de compilación/archivo · 2 uso incorrecto · en `run`, el del programa.
- `zylo run` no pasa argumentos al programa: Zylo aún no tiene acceso a argv (candidato v2, junto con stdin/archivos).
- Si rustc rechaza el Rust generado se informa como bug de Zylo (nunca debería ocurrir con un programa que pasó el checker).

### Fase 4: detalle (original)
- Funciones top-level → `fn nombre(a: i64) -> i64 { ... }`.
- Código top-level fuera de `fn` → cuerpo de `main`.
- `for i in range(a, b)` → `for i in a..b`.
- `list[T]` → `Vec<T>`; `str` → `String` (o `&str` para literales, decidir).
- Overflow: Rust en release hace wrapping; decidir si se usa `checked_*` o se acepta.

## Estructura del repo
```
zylo/
  zylo/
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
- Ver Rust generado: `.venv/bin/zylo emit examples/hello.zy`
- Avanzar siempre en vertical: cada construcción nueva debe pasar lexer → parser → tipos → codegen → rustc antes de pasar a la siguiente.

## Criterio de éxito del MVP — CUMPLIDO
- [x] `fib(35)` ≥ 20× más rápido que Python: **28×** con overflow-checks, 46× sin ellos (ver `benchmarks/RESULTS.md`).
- [x] Errores de sintaxis y de tipos con línea y columna (+ fragmento de código).
- [x] 10 ejemplos que compilan y pasan tests: 13 en `examples/`.

### Criterio original
- `fib(35)` en `.zy` al menos 20× más rápido que en Python.
- Errores de sintaxis y de tipos con línea y columna.
- 10 ejemplos que compilan y pasan tests.

## Fuera de alcance (v2)
Structs/clases, módulos e imports, genéricos, closures, gestor de paquetes, LSP, interop con crates de Rust
(esta última es crítica para adopción real), resaltado de sintaxis en VS Code.

## Decisiones abiertas
- Nombre definitivo del lenguaje (`zylo` es provisional).
- ~~Sintaxis de mutabilidad~~ **Decidido:** estilo Python (`x = 1` declara; reasignar lo detecta el checker; `let` eliminado de keywords).
- ~~Licencia~~ **Decidido:** MIT.
- Modelo de memoria a futuro: `Rc` implícito vs ownership simplificado.
- Manejo de overflow de enteros.
- Nicho objetivo: scripts/CLI, procesamiento de datos o automatización.

## Referencias de lenguajes similares
Mojo, Nim, Crystal, Codon, Vale, Gleam: revisar qué resolvió cada uno antes de decidir el modelo de memoria.
