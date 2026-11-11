# Architecture

Zylo is organised in **layers** and **bounded contexts**, in the spirit of domain-driven design. The rule that
holds it together: *dependencies point inwards*. The domain depends on nothing; every other layer may depend only on
the ones listed below. `tests/test_architecture.py` enforces it on every change.

```
                 ┌──────────────┐
                 │  interface   │  the command line
                 └──────┬───────┘
                 ┌──────▼───────┐
                 │ application  │  use cases: compile a program, manage packages
                 └──┬──┬──┬──┬──┘
      ┌─────────────┘  │  │  └──────────────┐
┌─────▼─────┐  ┌───────▼──┴──┐  ┌───────────▼───┐  ┌───────────────┐
│ semantics │  │ generation  │  │   packaging   │  │infrastructure │  git, rustc, cargo
└─────┬─────┘  └──────┬──────┘  └───────┬───────┘  └───────┬───────┘
┌─────▼─────┐         │                 │                  │
│  syntax   │         │                 │                  │
└─────┬─────┘         │                 │                  │
      └───────────────┴────────┬────────┴──────────────────┘
                        ┌──────▼──────┐
                        │   domain    │  the language model
                        └─────────────┘
```

| Layer | Allowed to use | What lives here |
|---|---|---|
| `domain` | nothing | The **ubiquitous language**: `ast` (syntax nodes), `types`, `builtins` (the catalog of built-in functions) and `errors`. Pure: no input or output. |
| `syntax` | domain | The lexer, the parser (with error recovery) and the formatter: text ⇄ syntax tree. |
| `semantics` | domain, syntax, packaging | The type `checker` and the module `linker`: what a program means. |
| `generation` | domain | The code generator: typed program → Rust source. |
| `packaging` | domain | `manifest` (zylo.toml), `lock`, virtual `env`ironments, the `installer`, and `rust_deps` validation. |
| `infrastructure` | domain, packaging | Adapters to external programs: `git`, `rustc`, `cargo`. |
| `application` | everything below | The use cases: `compile` (a program, end to end, and `diagnose_source` for every error), `format`, `editor` (symbols and completion) and `package_commands`. |
| `interface` | domain, application | The `cli`, the terminal `diagnostics` and the `lsp` server. |

## How a compilation flows

`zylo run main.zy` →

1. **interface** (`cli`) parses the arguments and calls the application.
2. **application** (`compile`) finds the project's manifest and environment, then drives the contexts:
3. **semantics** (`linker`) loads `main.zy` and its imports, asking **syntax** to parse each file, and merges them
   into one program; the `checker` types it and annotates the tree.
4. **generation** turns the typed program into Rust.
5. **application** hands the Rust to an **infrastructure** adapter: `rustc` for a plain program, or `cargo` when the
   project declares Rust crates.
6. **interface** runs the result and renders any error with `diagnostics`.

## Ports and adapters

A layer never reaches for an outer one. When an inner layer needs something from the outside world it asks for it
as an argument: the `Installer` (packaging) is given a `fetcher` function, and the application passes the git
adapter's `fetch`. That keeps the installer testable without git and independent of how packages are downloaded.

## Ubiquitous language

| Term | Meaning |
|---|---|
| **Program** | The statements of one module, or of every module after linking. |
| **Module** | One `.zy` file. A *library module* has only imports, functions and structs. |
| **Linking** | Resolving imports and renaming library definitions to `module·name` so everything becomes one program. |
| **Narrowing** | After an `is` check, a variable has the narrower type inside the branch (or after an early exit). |
| **Borrow** | A parameter the function never modifies is lent (`&T`) instead of cloned. |
| **Package** | Zylo source installed from git into an environment. |
| **Environment** | An isolated `.zylo-env` directory holding the packages one project uses. |
| **Manifest / Lock** | `zylo.toml` (what a project wants) and `zylo.lock` (the exact commits and checksums installed). |
| **Trust** | A project's permission for a package to call Rust (`[rust] trusted-packages`). |

## Where to put things

- **A new built-in function**: add its name to `domain/builtins.py`, its typing rule in `semantics/checker.py` and its
  Rust in `generation/codegen.py`.
- **A new kind of syntax**: a node in `domain/ast.py`, parsing in `syntax/parser.py`, then the checker and the generator.
- **A new command**: a use case in `application/`, then its arguments and output in `interface/cli.py`.
- **A new external tool**: an adapter in `infrastructure/`, called by the application layer.

## Conventions

- Absolute imports (`from zylo.domain.errors import ZyloError`), never relative ones.
- **Google-style docstrings** on every public module, class, function and method (`Args`, `Returns`, `Raises`,
  `Attributes`). `ruff` checks this together with style and import order; run `ruff check .` before pushing.
- A change that needs a new dependency between layers needs a change to this document and to the rules in
  `tests/test_architecture.py`.
