# Roadmap

Milestones are ordered by value and effort. Each one ends with tests, examples and documentation,
and is built vertically (lexer → parser → checker → codegen → tests).

| Milestone | Scope | Status |
|---|---|---|
| **v0.1 MVP** | Lexer, parser, type checker, Rust codegen, CLI, benchmarks | done |
| **v0.2 Own data and errors** | `struct` with methods; `Option` / `Result` with `is` narrowing and `?` | done |
| **v0.4 Collections and text** | `dict`, `set`, tuples, `in`, slices, negative indices, string indexing, list/`str`/`dict`/`set` methods, f-strings, escapes, math builtins | done |
| **v0.5 I/O** | `argv`, `input()` / stdin, files (as `Result`), environment variables, `eprint`, time | done |
| **v0.6 Memory and performance** | Infer borrows: read-only parameters are lent instead of cloned, and loops over read-only lists do not copy them | done |
| **Installers and releases** | `install.sh` for Linux/macOS, a Windows installer, standalone binaries and a wheel built by CI on every merge to `main` | done |
| **v0.3.0 Modules and packages** | `import`, virtual environments (`zylo venv`), git packages with a lock file (`zylo add/install/update/remove/list`), see [docs/packages.md](docs/packages.md) | done |
| **v0.12 Standard library** | `std.*` modules bundled with the compiler: `std.json` (parse, stringify, pretty, accessors), `std.math`, `std.text`, `std.os`; see [docs/stdlib.md](docs/stdlib.md) | done |
| **v0.7 Rust interop** | `extern fn` with Rust paths and templates, crates from `[rust-dependencies]` built with Cargo, and a trust rule for community packages | done |
| **v0.8 Tooling** | `zylo fmt`, every error in one run, a language server (`zylo lsp`) and a VS Code extension with a generated grammar | done |
| **v0.9 Enums** | `enum` with data per variant, exhaustive `match`, enums across modules and in editor symbols | done |
| **v0.10 Language polish** | Methods on enums, `match` on `int` / `str` / `bool` / `Option` / `Result`, default values for parameters and struct fields, named arguments in calls and constructors | done |
| **v0.11 Functions as values** | `fn(A) -> R` types, top-level functions as values, anonymous functions (`fn(x: int) => x * 2`) and local `fn`s that capture copies, calls of any function value, and `map` / `filter` / `fold` / `reduce` / `sort_by` / `any` / `all` / `find` on lists | done |
| **v0.13 HTTP** | `std.http`: a blocking HTTP/1.1 server (`serve`, `serve_n`, panics become 500, body limit, HEAD / OPTIONS) and a client (`get`, `post`...), built on `tiny_http` and `ureq`; stdlib modules can bring Rust crates; see [docs/stdlib.md](docs/stdlib.md) | done |
| **v0.14 Typed JSON** | `to_json` / `from_json` / `json_schema` / `json_schema_defs` for structs, enums and collections, with JSON-path errors, OpenAPI-ready schemas and doc strings on types and fields | done |
| **Community** | Documentation translations (the docs are English-only for now) and localized diagnostics | |

Later: generics, concurrency, a central package registry with `zylo publish`, semantic-version ranges and a dependency resolver.

## Open questions

- Language server: hover, go to definition, rename and signature help.
- Formatter: wrapping of very long lines.

- Narrowing beyond plain variables: `and` chains and struct fields (`self.age is some`).
- `match` guards, or-patterns (`1 | 2`) and nested patterns; non-constant default values.
- Long-term memory model: implicit clones plus inferred borrows, or simplified ownership.
- Custom error types and conversions for `?` between different error types.
- Package trust: signing or an allow-list on top of the lock file checksums.
