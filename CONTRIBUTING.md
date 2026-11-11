# Contribuir a Zylo

## Entorno

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q          # necesita rustc para los tests end-to-end (si falta, se omiten)
```

## Cómo está hecho

```
.zy ──lexer──▶ tokens ──parser──▶ AST ──checker──▶ AST tipado ──codegen──▶ Rust ──rustc──▶ binario
```

| Módulo | Responsabilidad |
|---|---|
| `zylo/lexer.py` | Tokens, INDENT/DEDENT, posiciones |
| `zylo/ast.py` | Nodos (todos llevan `line`, `col`, `ty`) |
| `zylo/parser.py` | Recursivo descendente escrito a mano |
| `zylo/types.py`, `zylo/checker.py` | Tipos, inferencia local, ámbitos, mutabilidad |
| `zylo/codegen.py` | AST tipado → Rust |
| `zylo/diagnostics.py`, `zylo/cli.py` | Errores legibles y comandos |

## Cómo añadir una característica

Avanza **siempre en vertical**: una construcción nueva debe atravesar todo el pipeline antes de pasar a la siguiente.

1. Lexer/parser + test en `tests/test_parser.py`.
2. Reglas de tipos + tests de éxito **y de cada error** (con su línea) en `tests/test_checker.py`.
3. Codegen + un programa `.zy` con su `.out` en `tests/programs/`.
4. Un ejemplo en `examples/` si aporta (su `.out` se calcula con una implementación independiente, no con Zylo).
5. Actualiza `PLAN.md`/`ROADMAP.md` si cambia una decisión de diseño.

## Reglas

- Los errores del usuario son `ZyloError` con línea y columna; un fallo de `rustc` sobre código generado es un bug de Zylo (`BackendError`).
- Sin coerciones implícitas entre tipos: es una decisión de diseño.
- Mensajes de error en español, con una pista accionable cuando se pueda.
