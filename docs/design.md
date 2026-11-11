# Zylo design notes

Why Zylo works the way it does. For the language itself, see the [guide](guide.md).

## Goals

Python-like syntax, static typing with local inference, and native speed by compiling to Rust.
Zylo is a transpiler: it checks your program, emits Rust and lets `rustc` do the optimizing.

## Pipeline

```
.zy ──lexer──▶ tokens ──parser──▶ AST ──checker──▶ typed AST ──codegen──▶ Rust ──rustc──▶ binary
```

The compiler is written in Python with a hand-written lexer and recursive-descent parser, so error
messages are fully under our control. Every AST node carries its `line`, `col` and (after checking)
its `ty`; the checker also leaves hints the code generator needs (`declares`, `mutable`,
`self_mut`, narrowing bindings).

## Decisions and their reasons

**Transpile to Rust instead of writing a backend.** LLVM-quality optimization for free, a safe
target and a large ecosystem to interoperate with later. The cost is `rustc` compile time
(roughly 0.15–0.25 s for small programs).

**Static types, local inference, no implicit coercion.** Only function signatures must be
annotated. `int + float` is an error because silent numeric coercions are a common source of bugs;
conversions are explicit (`float(x)`).

**Value semantics, no visible borrow checker.** Zylo programs never mention lifetimes or moves.
Everything behaves like a value: reading a non-`Copy` variable (`str`, `list`, struct) as a value
emits `.clone()` in the Rust, while pure reads (indexing, `len`, `print`, comparisons) take a
reference and do not clone. Consequence: passing a list to a function copies it. This is the main
known performance cost; inferring borrows is on the [roadmap](../ROADMAP.md).

**Declaration vs reassignment is inferred.** `x = 1` declares the first time and reassigns after,
like Python. The checker marks variables that are reassigned or mutated (`append`, `xs[i] = v`,
`p.x = v`, mutating method calls) and the generator emits `let mut` only for those.

**Block scope.** A variable declared inside a block does not exist outside it. It matches the
generated Rust and avoids Python's "variable might be unbound" class of surprises.

**Integer overflow panics.** The generated code is compiled with overflow checks (exit code 101
instead of silent wrap-around). Cost measured on `fib`: about 1.6×. `--no-overflow-checks` turns it
off. `int / int` is truncating integer division, like Rust.

**Structs are values; methods live inside the struct.** `self` is passed by reference
(`&self` or `&mut self`); the checker infers which, iterating to a fixed point so that a method that
calls a mutating method is itself mutating. `Debug` output is written by hand to print
`Point(x=1.0, y=2.0)`.

**Errors are values.** `T?` and `Result[T, E]` map directly onto Rust's `Option` and `Result`,
with no hidden control flow and no runtime cost. Three features make them pleasant:

- `none`, `some`, `ok` and `err` take their type from context (annotation, parameter, return type,
  field), the same mechanism that types an empty list.
- `x is some` narrows `x` to its inner type inside the branch, and an early exit
  (`if x is none: return ...`) narrows it for the rest of the block. It is generated as a Rust
  `match` that rebinds the name, so narrowed variables are cheap and cannot be reassigned.
- `expr?` is Rust's `?`: it requires the enclosing function to return an `Option`, or a `Result`
  with the same error type.

**Generated names never collide.** Zylo identifiers that are Rust keywords or would clash with the
prelude (`type`, `match`, `String`, a method called `clone`...) get a trailing `_` in the output.

**Diagnostics.** Errors are `file:line:col: error: message` with the source line and a caret.
A `rustc` failure on generated code is reported as a Zylo bug (`BackendError`), never as a mistake
in the user's program.

**Modules are linked, not compiled separately.** Zylo emits one Rust program, so the linker loads the
entry file and every imported module, renames each library definition to `module·name`, rewrites the
references and hands one merged program to the checker. This keeps type checking and code generation
unchanged and makes imported code optimize together with yours. Libraries may contain only imports,
functions and structs, so importing has no side effects.

**Packages are source code pinned by git commit.** There is no central registry to run or trust: a
dependency is a git URL and a ref, and `zylo.lock` records the exact commit and a checksum of the files
(see [packages.md](packages.md)). Environments are per project, so two projects never share versions.
Installing never executes package code.

## Known limitations

- Lists and structs are cloned when passed to functions.
- `is` narrowing works on plain variables and a single condition (no `and` chains, no fields).
- No `dict`/`set`/tuples, string indexing/methods, closures, generics or I/O yet.
- No central registry, version ranges or dependency resolver: dependencies are pinned to exact refs.
- Diagnostics are in English only.
