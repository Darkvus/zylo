# Roadmap

Milestones are ordered by value and effort. Each one ends with tests, examples and documentation,
and is built vertically (lexer → parser → checker → codegen → tests).

| Milestone | Scope | Status |
|---|---|---|
| **v0.1 MVP** | Lexer, parser, type checker, Rust codegen, CLI, benchmarks | done |
| **v0.2 Own data and errors** | `struct` with methods; `Option` / `Result` with `is` narrowing and `?` | done |
| **v0.3 Collections and text** | `dict[K, V]`, `set`, tuples, `in`, slices, list and `str` methods (`split`, `trim`, `upper`, `pop`, `sort`…), f-strings | next |
| **v0.4 I/O** | `argv`, `input()` / stdin, reading and writing files, environment variables | |
| **v0.5 Memory and performance** | Infer borrows so lists and structs are not cloned when passed to functions | |
| **v0.6 Modules** | `import` between `.zy` files, a minimal standard library (`math`, `os`) | |
| **v0.7 Rust interop** | Use crates (Cargo backend) and call Rust functions from Zylo | |
| **v0.8 Tooling** | VS Code syntax highlighting, formatter, several errors per run, LSP | |
| **Community** | Documentation translations (the docs are English-only for now) and localized diagnostics | |

Later: generics, closures, enums with data, concurrency.

## Open questions

- Narrowing beyond plain variables: `and` chains and struct fields (`self.age is some`).
- Default values for struct fields and constructors with named arguments (`Point(x=1.0, y=2.0)`).
- Long-term memory model: implicit clones plus inferred borrows, or simplified ownership.
- Custom error types and conversions for `?` between different error types.
