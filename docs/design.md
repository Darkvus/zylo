# Zylo design notes

Why Zylo works the way it does. For the language itself, see the [guide](guide.md).

How the code is organised into layers and bounded contexts is described in [architecture.md](architecture.md).

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
reference and do not clone.

**Borrows are inferred.** The checker already knows which variables are ever modified (it needs that to emit
`let mut`). A parameter that its function never modifies is declared `&T` in the Rust and callers lend the
argument (`f(&xs)`) instead of cloning it; a parameter that is modified stays by value, so the caller never sees
the change. Two details keep this safe: a method that modifies `self` takes its arguments by value (they may be
parts of `self`, which cannot be lent while `self` is borrowed mutably), and `for x in xs` iterates by reference
when `xs` is never modified anywhere, and over a snapshot otherwise (like Python, the body may modify the list).
Before this, calling a function in a loop with a large list was slower than Python (see `benchmarks/listsum.zy`).

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

**Defaults are constants resolved by the checker.** A default (`b: int = 2`, `x: float = 0.0`) must be a compile-time
constant: a literal, `none`, an empty list or dict, an enum variant with constant data, or a tuple of those. That keeps every call site trivially correct
(no evaluation-time questions, no Python-style shared mutable default). The checker rewrites each call into a full
positional argument list, copying the default expression into the arguments that were left out and ordering named
arguments by parameter. After checking, a call has exactly one argument per parameter, so the code generator,
borrow inference and `extern fn` templates are unchanged. The price is that arguments are evaluated in parameter
order rather than the order written; that only matters when two arguments have side effects. Positional arguments
come first, then named ones (the Python rule), and a parameter without a default may not follow one with a default.
Built-in functions and enum variant constructors stay positional: their signatures are fixed by the language.

**`match` is one statement with several pattern families.** Besides `Enum.Variant(a, b)`, an arm may be a literal
(`0`, `-1`, `"stop"`, `true`), or `some(x)` / `none` / `ok(v)` / `err(e)`; the family is chosen by the type of the
scrutinee. Literal and wrapper patterns reuse the enum machinery (arms bind names, `_` is the catch-all, a `match`
must be exhaustive and an unreachable arm is an error). Integers and strings are open sets, so they always need
`_`; `bool` is closed and is complete with `true` and `false`. Floats are rejected because matching on float equality
is almost always a bug. It is generated as a Rust `match` on an owned copy (a `&str` for strings), so bindings are
plain values just like the enum case. `is` narrowing is untouched: `match` is for choosing between cases, `is` for
guarding a single one.

**Enum methods mirror struct methods.** `fn` with `self` inside an `enum` body is emitted in an `impl` block. Enum
values are immutable (there is nothing to assign to), so a method always takes `&self` and the usual body is
`match self:`.

**Generated names never collide.** Zylo identifiers that are Rust keywords or would clash with the
prelude (`type`, `impl`, `String`, a method called `clone`...) get a trailing `_` in the output.

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

**The standard library is Zylo, resolved before everything else.** `std.*` modules are `.zy` files in `zylo/stdlib/`,
shipped as package data and bundled in the standalone binary, so they are written in the language they serve
(`std.json` is a recursive-descent parser in Zylo) and need no crates: the `rustc` fast path stays. The linker looks in
`std` first, so no local file or package can shadow it, and the library is trusted to use private `extern fn` helpers
where Zylo cannot be fast enough (JSON works on a list of code points, because indexing a `str` by character is linear).
`s += x` on a string appends in place, since rebuilding the string each time made building text quadratic.

**Dicts and sets are ordered by key.** They map to `BTreeMap` / `BTreeSet`, so iteration and printing are
deterministic (a hash map would print in a different order on every run) and keys only need to be `int`,
`str`, `bool` or tuples of those. The price is that order is sorted, not insertion order as in Python.

**Negative indices and slices are runtime helpers.** `xs[-1]` and slices go through small helper functions
in the generated Rust prelude. Negative indexing needs to evaluate the container twice, so it is applied when
the container is a variable, field or index chain (no side effects); other expressions use plain indexing.

**Rust interop is a template, and Rust access is opt-in for packages.** `extern fn` is a Zylo signature (so the
checker keeps its guarantees) plus a Rust path or template expanded at each call, with arguments passed by value.
Crates are built through a generated Cargo project only when a project declares some, so ordinary programs keep the
fast `rustc` path. Because compiling a crate runs its `build.rs` and `extern fn` can run anything, a community
package may use either only if the project lists it under `[rust] trusted-packages`; otherwise packages are plain
Zylo, which is what makes installing and compiling them safe to reason about.

**Tooling is built on the same pipeline.** Reporting every error needed no second front end: the parser recovers by
skipping to the end of the failed statement (and its block), the checker checks each statement on its own and
"poisons" a variable whose declaration failed so its later uses are not reported again, and `check()` still raises the
first error for callers that want one. The formatter prints the syntax tree rather than rewriting text, so it cannot
change a program's meaning, and a test proves it on every program in the repository. The language server is a thin
translation (in the interface layer) of use cases in the application layer, and the VS Code grammar is generated from
the lexer's keywords and the built-in catalog, so highlighting cannot drift from the language.

**Functions are values, and closures capture copies.** A function type is `fn(A, B) -> R` and is `Rc<dyn Fn(A, B) -> R>`
in Rust, so a function value is cheap to clone and can live in structs, lists and dicts like any other value. A top-level
function used as a value is wrapped in a closure (lent parameters are lent inside the wrapper). Closures follow the
language's value semantics: creating one clones every variable it uses (`{ let x = x.clone(); Rc::new(move |..| ..) }`),
so it is `'static` and `Fn`, never `FnMut`, and later changes to the outer variable are invisible to it. Because of that
the checker rejects assigning to, or mutating, a captured variable inside a closure (a silent change to a copy is a bug
waiting to happen), and rejects capturing `self` (Rust cannot rebind it). The checker resolves captures with closure
frames over the scope stack: a variable found outside a frame is replaced, inside it, by a fresh read-only variable and
the pair is recorded on the `Lambda` node for the generator. Functions are neither comparable nor printable: the checker
rejects `==`, `print`, f-strings and `str()` on any type that contains one, and the generator leaves `PartialEq` off such
structs and enums and prints them opaquely, so a struct with a function field still derives `Clone` and works.
The syntax is `fn(x: int) => expr`: `=>` avoids the `:` ambiguity with dict literals and block headers and keeps one
keyword for all functions; parameter types may be omitted where the context fixes them (arguments of built-in higher-order
methods, typed variables, fields). Multi-statement bodies are a local `fn name(...)` in a block, which the parser turns into
a variable assigned a block-bodied lambda, so there is a single closure mechanism. A call whose callee is not a plain name is
a `CallValue`; `obj.field(x)` is a method call node that the checker marks as a field call when the field has a function type.

## Known limitations

- Values are still cloned when stored (assigned to another variable, put in a struct or a list) or returned.
- `is` narrowing works on plain variables and a single condition (no `and` chains, no fields).
- Rust generics and traits are not exposed. Rust can call Zylo function values passed to an `extern fn` (see rust-interop.md).
- No generics or list comprehensions yet; closures are limited (see above). `match` has no guards, no or-patterns (`1 | 2`) and no nested patterns.
- The language server has no hover, go to definition or rename yet. I/O is synchronous and text-only (no binary files, sockets or subprocesses).
- No central registry, version ranges or dependency resolver: dependencies are pinned to exact refs.
- Diagnostics are in English only.
