# Contributing to Zylo

## Setup

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q          # the end-to-end tests need rustc; without it they are skipped
```

Install Rust from <https://rustup.rs> and make sure `git` is available (the package manager tests create real local git repositories). If `rustc` is not on your `PATH`, the end-to-end tests are
skipped silently — run `source ~/.cargo/env` first.

## How it is built

```
.zy ──lexer──▶ tokens ──parser──▶ AST ──checker──▶ typed AST ──codegen──▶ Rust ──rustc──▶ binary
```

| Module | Responsibility |
|---|---|
| `zylo/lexer.py` | Tokens, INDENT/DEDENT, positions |
| `zylo/ast.py` | Nodes (all carry `line`, `col`, `ty`) |
| `zylo/parser.py` | Hand-written recursive descent |
| `zylo/types.py`, `zylo/checker.py` | Types, local inference, scopes, mutability, narrowing |
| `zylo/codegen.py` | Typed AST → Rust |
| `zylo/linker.py` | Resolves `import`s and merges modules into one program |
| `zylo/manifest.py`, `zylo/env.py`, `zylo/lock.py`, `zylo/gitfetch.py`, `zylo/packages.py`, `zylo/pkgcmds.py` | `zylo.toml`, virtual environments, the lock file, git download and the package commands |
| `zylo/diagnostics.py`, `zylo/cli.py` | Readable errors and the commands |

Design reasons are in [docs/design.md](docs/design.md); the language is described in
[docs/guide.md](docs/guide.md).

## Adding a feature

Always work **vertically**: a new construct goes through the whole pipeline before the next one starts.

1. Lexer/parser, with tests in `tests/test_parser.py`.
2. Type rules, with tests for the success cases **and for every error** (including its line), in `tests/test_checker.py` or a feature-specific file.
3. Codegen, plus an end-to-end program `X.zy` with its expected output `X.out` in `tests/programs/`.
4. An example in `examples/` if it teaches something. Compute its `.out` with an independent implementation (for instance plain Python), **not** with Zylo itself.
5. Update `docs/guide.md` (its `zylo` code blocks are type-checked by the tests) and `ROADMAP.md`.

## Conventions

- Commit messages and documentation are written in **English**.
- User mistakes are `ZyloError` with a line and column; a `rustc` failure on generated code is a Zylo bug (`BackendError`).
- No implicit coercions between types: this is a design decision.
- Error messages should say what is wrong and, when possible, how to fix it.
