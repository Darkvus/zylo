# Roadmap

Milestones are ordered by value and effort. Each one ends with tests, examples and documentation,
and is built vertically (lexer → parser → checker → codegen → tests).

| Milestone | Scope | Status |
|---|---|---|
| **v0.1 MVP** | Lexer, parser, type checker, Rust codegen, CLI, benchmarks | done |
| **v0.2 Own data and errors** | `struct` with methods; `Option` / `Result` with `is` narrowing and `?` | done |
| **v0.4 Collections and text** | `dict`, `set`, tuples, `in`, slices, negative indices, string indexing, list/`str`/`dict`/`set` methods, f-strings, escapes, math builtins | done |
| **v0.5 I/O** | `argv`, `input()` / stdin, reading and writing files, environment variables | |
| **v0.6 Memory and performance** | Infer borrows so lists and structs are not cloned when passed to functions | |
| **Installers and releases** | `install.sh` for Linux/macOS, a Windows installer, standalone binaries and a wheel built by CI on every merge to `main` | done |
| **v0.3.0 Modules and packages** | `import`, virtual environments (`zylo venv`), git packages with a lock file (`zylo add/install/update/remove/list`), see [docs/packages.md](docs/packages.md) | done |
| **Standard library** | A minimal bundled library (`math`, `os`, text helpers) | |
| **v0.7 Rust interop** | Use crates (Cargo backend) and call Rust functions from Zylo | |
| **v0.8 Tooling** | VS Code syntax highlighting, formatter, several errors per run, LSP | |
| **Community** | Documentation translations (the docs are English-only for now) and localized diagnostics | |

Later: generics, closures, enums with data, concurrency, a central package registry with `zylo publish`, semantic-version ranges and a dependency resolver.

## Open questions

- Narrowing beyond plain variables: `and` chains and struct fields (`self.age is some`).
- Default values for struct fields and constructors with named arguments (`Point(x=1.0, y=2.0)`).
- Long-term memory model: implicit clones plus inferred borrows, or simplified ownership.
- Custom error types and conversions for `?` between different error types.
- Package trust: signing or an allow-list on top of the lock file checksums.
