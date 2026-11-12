# Modules, environments and packages

Specification of Zylo's module system and package manager. Status: implemented (v0.3.0). Scope decisions: community libraries are **Zylo source code** (no Rust crates yet),
distributed through **git** and pinned by a **lock file**, installed into a **per-project virtual
environment**.

## 1. Modules

A module is one `.zy` file. Modules let a program be split over several files and use libraries.

```
import geometry                       # use as geometry.area(...)
import geometry as g                  # use as g.area(...)
from geometry import area, Point      # use area(...) and Point(...) directly
from geometry import area as get_area
```

- A module may contain only `import` statements, `fn` and `struct` definitions. Top-level statements
  (code that runs) are allowed only in the program's entry file.
- `import a.b` loads `a/b.zy`. The module is bound to its **last** segment (`b`) or to the `as` name.
- Module values do not exist: `geometry.area(...)` calls a function, `geometry.Point(...)` builds a
  struct and `geometry.Point` is usable as a type (`p: geometry.Point`), but `geometry` alone or
  `geometry.x` is an error.
- **Privacy:** names starting with an underscore (`_helper`) are private to their module.
- **Re-exports:** `pub from a import x, y as z` imports names and also makes them part of the importing module's
  public surface, so a package entry file can offer one import point (see below).
- **Cycles** between modules are an error. Names defined in two places clash with an error.
- A variable or parameter cannot reuse the name of an imported module.

### Re-exports

A package usually has several modules but wants callers to write one import. In the package entry file (`lib.zy`),
`pub from` re-exports names:

```
# lib.zy of package `shop`
pub from model import Item, Kind
pub from pricing import total, discount as apply_discount
```

```
from shop import Item, Kind, total, apply_discount     # instead of shop.model / shop.pricing
```

- It works for functions, structs and enums, and chains: `pub from` of a name that is itself re-exported is fine.
- A re-exported type is the same type as the original: the linker names it after the module that defines it, so it
  appears once in the generated Rust and values flow between `shop.model.Item` and `shop.Item` unchanged.
- The name must exist and be public (`_private` names cannot be re-exported, nor re-exported under a `_` alias).
- It clashes like any other name: a re-export that shares its name with a function, struct, enum or import of the
  same module is an error. A plain `from a import x` (without `pub`) stays private to the file.
- Re-export cycles are import cycles, so they are reported as `import cycle: a -> b -> a`.
- `pub` only applies to `from ... import` and only in library modules, not in the entry file of a program.

### Resolution order

For `import a.b.c` written in file `F`:

0. **Standard library:** when `a` is `std`, the module comes from the library bundled with the compiler
   (`import std.json`, see [stdlib.md](stdlib.md)) and nothing else is consulted, so no local file or package can shadow `std`.
1. **Local:** `<dir of F>/a/b/c.zy`.
2. **Package:** if `a` is an installed package in the active environment, `c` is looked up inside it:
   `import a` is the package entry file, `import a.b.c` is `<package src>/b/c.zy`.

Local modules win over packages. Imports inside a package are resolved the same way, relative to
the package's own files.

### Implementation: linking

Zylo compiles one Rust program. The linker loads the entry file and everything it imports, renames
each non-entry top-level `fn`/`struct` to `module·name` (`strutil·shout`; `·` becomes `__` in the
generated Rust, and error messages show `strutil.shout`), rewrites every reference, and merges all
modules into a single program that goes through the normal checker and code generator. Struct
`Debug` output keeps the original name. Each AST node remembers its file, so errors in a library
show the library's file, line and a code snippet.

## 2. Project manifest: `zylo.toml`

```toml
[package]
name = "myapp"          # letters, digits and underscores
version = "0.1.0"
entry = "src/lib.zy"    # optional; libraries default to src/lib.zy, then lib.zy

[dependencies]
strutil = "github.com/ana/strutil@v1.2.0"        # git: <host/user/repo>[@ref]
other   = "https://example.org/team/other.git@main"
local   = { path = "../local-lib" }               # a directory on disk
```

`zylo add` and `zylo remove` rewrite `zylo.toml`, so comments in that file are not preserved.

A *ref* is a tag, a branch or a commit. Version ranges are not supported yet: dependencies are pinned
to an exact ref. Dependencies of dependencies are installed too (flat: one version per name; two
different requirements for the same name are an error). `path` dependencies are only allowed in the
project's own manifest.

## 3. Virtual environments

```
zylo venv                # create .zylo-env/ in the current directory
zylo venv path/to/env    # or elsewhere (--force recreates)
```

```
.zylo-env/
  env.toml               # Zylo version that created it
  packages/
    strutil/             # a package's sources, without .git
    other/
```

An environment is isolated: two projects never share installed versions. **Discovery:** `zylo run`,
`build`, `check` and `emit` use `--env DIR`, else `$ZYLO_ENV`, else the nearest `.zylo-env` found by
walking up from the source file's directory. Without an environment, only local imports work.

## 4. Package commands

| Command | Effect |
|---|---|
| `zylo init [name]` | create `zylo.toml` and `src/main.zy` |
| `zylo venv [path]` | create an environment |
| `zylo add <source>[@ref]` | add a dependency to `zylo.toml`, install it (and its dependencies) and update the lock |
| `zylo remove <name>` | remove a dependency and prune the environment |
| `zylo install` | make the environment match `zylo.toml` and `zylo.lock` (reproducible) |
| `zylo update` | re-resolve refs (a branch moves forward) and rewrite the lock |
| `zylo list` | list installed packages with their version, source and commit |

`<source>` is `host/user/repo`, an `https://`, `ssh://`, `git@host:path` or `file://` URL.
`--name` overrides the dependency name when the package does not declare one.

## 5. Lock file: `zylo.lock`

```toml
version = 1

[[package]]
name = "strutil"
version = "1.2.0"
source = "https://github.com/ana/strutil.git"
ref = "v1.2.0"
commit = "9f3c2a1…"          # exact commit the ref pointed to
checksum = "sha256:…"        # hash of the installed files
```

`zylo install` uses the locked commit, never the moving ref, and verifies the checksum: if the
upstream history changed, or an installed file was edited, installation fails or repairs the package
instead of silently using different code.

## 6. Security model

Installing a package **never executes its code**: it only downloads files. Zylo packages are source
code compiled into your program, so — as with any dependency — you are trusting their authors when
you run the result. Mitigations built into the tool:

- Package names are restricted to `[A-Za-z_][A-Za-z0-9_]*`, so a manifest cannot write outside the
  environment (no `..`, no separators).
- Git URLs and refs are validated (no leading `-`, no whitespace) and passed after `--`, so they
  cannot inject git options.
- Git runs with `GIT_TERMINAL_PROMPT=0` and a timeout; it never waits for credentials.
- Packages containing symbolic links are rejected, so a package cannot make the compiler read files
  outside itself.
- Checksums in the lock detect tampering and upstream force-pushes.

## 7. Rust access and trust

A package is Zylo source and cannot call Rust by default. If a package contains an `extern fn`, or declares
`[rust-dependencies]`, your project must list it in `[rust] trusted-packages` in `zylo.toml`; otherwise the first is an
error and the second is ignored. See [rust-interop.md](rust-interop.md).

## 8. Out of scope (for now)

A central registry and `zylo publish`, semantic-version ranges and a resolver, module-level
constants, wrapping Rust crates (see the Rust interop milestone), and per-package feature
flags.
