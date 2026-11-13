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
| **v0.15 Shared state** | `Cell[T]`: a shared mutable box (`cell(v)`, `get`, `set`, `update`) compiled to `Rc<RefCell<T>>`, so closures, handlers and structs can share a counter or a list | done |
| **v0.16 Re-exports** | `pub from a import x` makes names importable from a package root: one import point, chains, a single type identity, clear errors for unknown names, conflicts and cycles | done |
| **v0.17 Ergonomics** | Narrowing on field paths (`if p.name is some:` uses `p.name` as the plain value), multi-line list / dict / tuple literals with trailing commas, `std.json` `get_*` / `as_array` / `as_object` / `at` accessors | done |
| **v0.18 Richer `match`** | Guards (`Pattern if cond:`, never counted toward exhaustiveness) and or-patterns (`1 \| 2`, `Color.Red \| Color.Blue`, variants that bind the same names with the same types) | done |
| **v0.19 Language server navigation** | Hover (types, signatures, descriptions), go to definition (across modules, imports and re-exports), rename (scope-correct for locals, project-wide for top-level items, fields, variants, methods and named arguments, with `prepareRename`) and signature help (active parameter, defaults, named arguments); see [docs/editors.md](docs/editors.md) | done |
| **v0.20 Formatter line wrapping** | `zylo fmt` wraps lines longer than 88 columns (`--line-length N` or `[format] line-length` in `zylo.toml`): calls, literals and struct constructors one item per line, long boolean / arithmetic expressions and method chains inside parentheses, signatures, imports and `match` or-patterns; trailing commas in parameter lists, parenthesized `from ... import (...)` and `(A \| B):` patterns | done |
| **v0.21 Language server completion** | Completion that knows types (fields and methods after a dot, module exports, enum variants, named arguments, imports, types, names in scope, built-in methods), hover and signature help for built-in functions and methods, quick fixes (add a missing import, add the missing `match` arms), and semantic features that keep working around syntax errors; see [docs/editors.md](docs/editors.md) | done |
| **v0.22 Generics** | Type parameters on functions, structs and enums (`fn first[T]`, `struct Box[T]`, `enum Opt[T]`), inferred at calls or written (`first[int](xs)`), a fixed set of bounds (`Eq`, `Show`, `Ord`, `Num`, `Key`), generic bodies checked once for every type, monomorphized to plain Rust, working across modules, re-exports, typed JSON and the language server; see [docs/guide.md](docs/guide.md#generics) | done |
| **Community** | Documentation translations (the docs are English-only for now) and localized diagnostics | |

Later: generic methods and user-defined bounds, concurrency, a central package registry with `zylo publish`, semantic-version ranges and a dependency resolver.

## Open questions

- Language server: more code actions (unused variables, wrapping a value in `some`), and completion of `match` patterns.

- Narrowing beyond plain variables and field paths: `and` chains, indexed elements and closures.
- Nested patterns and integer ranges in `match`; non-constant default values.
- Long-term memory model: implicit clones plus inferred borrows, or simplified ownership.
- Custom error types and conversions for `?` between different error types.
- Package trust: signing or an allow-list on top of the lock file checksums.
