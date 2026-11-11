# Roadmap de Zylo (post-MVP)

El MVP v0.1 está completo (ver [PLAN.md](PLAN.md)). Orden de los hitos según valor/esfuerzo.
Cada hito termina con tests, ejemplos y documentación, y se avanza siempre en vertical.

| Hito | Contenido | Por qué ahora |
|---|---|---|
| **v0.2 Datos propios** | ✅ `struct` con campos, constructor y métodos (hecho). Pendiente: `Option` y `Result` explícitos | Sin tipos propios no se pueden modelar programas reales |
| **v0.3 Colecciones y texto** | `dict[K,V]`, `set`, tuplas, `in`, rebanadas, métodos de lista y `str` (`split`, `trim`, `upper`…), f-strings | La mitad de lo que hace útil a Python |
| **v0.4 E/S** | `argv`, `input()`/stdin, leer y escribir archivos, variables de entorno | Necesario para el nicho scripts/CLI |
| **v0.5 Memoria y rendimiento** | Inferencia de préstamos (no clonar listas al pasarlas a funciones), parámetros inmutables como `&T` | Quita la limitación conocida del MVP |
| **v0.6 Módulos** | `import` entre archivos `.zy`, librería estándar mínima (`math`, `os`) | Proyectos de más de un archivo |
| **v0.7 Interop con Rust** | Usar crates (backend Cargo), llamar funciones de Rust desde Zylo | Crítico para adopción real |
| **v0.8 Herramientas** | Resaltado de sintaxis para VS Code, formateador, varios errores por ejecución, LSP | Experiencia de desarrollo |

Más adelante: genéricos, closures, enums con datos, concurrencia.

## Decisiones tomadas

- **Métodos dentro del `struct`**, con `self` como primer parámetro (estilo Python/Swift). Decidido el 2026-11-11.
- **Errores con `Option`/`Result` explícitos**, no excepciones: mapean directo a Rust y encajan con el tipado estático. Sintaxis por diseñar (borrador: `int?` y `is some`).
- **Structs por valor**: copiar o pasar a una función clona; los métodos reciben `self` por referencia (`&self` o `&mut self`, inferido). El constructor es posicional con todos los campos.

## Decisiones abiertas

- Sintaxis concreta de `Option`/`Result` y de la propagación de errores (¿operador `?`?).
- Modelo de memoria a largo plazo: clon implícito + préstamos inferidos frente a ownership simplificado.
- Valores por defecto en campos y constructores con nombre (`Point(x=1.0, y=2.0)`).
