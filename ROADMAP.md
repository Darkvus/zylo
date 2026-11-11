# Roadmap de Zylo (post-MVP)

El MVP v0.1 está completo (ver [PLAN.md](PLAN.md)). Orden de los hitos según valor/esfuerzo.
Cada hito termina con tests, ejemplos y documentación, y se avanza siempre en vertical.

| Hito | Contenido | Por qué ahora |
|---|---|---|
| **v0.2 Datos propios** | `struct` (campos, constructor, acceso y asignación), después métodos; `Option` y manejo de errores | Sin tipos propios no se pueden modelar programas reales |
| **v0.3 Colecciones y texto** | `dict[K,V]`, `set`, tuplas, `in`, rebanadas, métodos de lista y `str` (`split`, `trim`, `upper`…), f-strings | La mitad de lo que hace útil a Python |
| **v0.4 E/S** | `argv`, `input()`/stdin, leer y escribir archivos, variables de entorno | Necesario para el nicho scripts/CLI |
| **v0.5 Memoria y rendimiento** | Inferencia de préstamos (no clonar listas al pasarlas a funciones), parámetros inmutables como `&T` | Quita la limitación conocida del MVP |
| **v0.6 Módulos** | `import` entre archivos `.zy`, librería estándar mínima (`math`, `os`) | Proyectos de más de un archivo |
| **v0.7 Interop con Rust** | Usar crates (backend Cargo), llamar funciones de Rust desde Zylo | Crítico para adopción real |
| **v0.8 Herramientas** | Resaltado de sintaxis para VS Code, formateador, varios errores por ejecución, LSP | Experiencia de desarrollo |

Más adelante: genéricos, closures, enums con datos, concurrencia.

## Decisiones abiertas

- Sintaxis de métodos: dentro del `struct` con `self` (estilo Python) o bloque `impl` separado.
- Errores: `Option`/`Result` explícitos frente a excepciones.
- Modelo de memoria a largo plazo: clon implícito + préstamos inferidos frente a ownership simplificado.
