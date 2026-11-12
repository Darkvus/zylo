# Calling Rust from Zylo

Zylo compiles to Rust, so it can call Rust code directly: the standard library, and any crate. This is a
power tool — Rust code is not checked by Zylo — and it comes with trust rules for community packages.

## `extern fn`

An `extern fn` declares a Zylo function whose body is a piece of Rust. The signature is Zylo (so the checker
knows the types); the Rust is a string after `=`.

```zylo
extern fn hypot(a: float, b: float) -> float = "f64::hypot"
extern fn shout(s: str) -> str = "{0}.to_uppercase()"
extern fn hex(n: int) -> str = "format!(\"{:x}\", {0})"

print(hypot(3.0, 4.0))     # 5.0
print(shout("hi"), hex(255))
```

- **Path form** (no placeholders): `"f64::hypot"` becomes `f64::hypot(a, b)`.
- **Template form**: `{0}`, `{1}`... are the arguments, so any Rust expression works. Other braces are left alone.
- Arguments are passed **by value** (a `str` is a `String`, a `list[T]` a `Vec<T>`, `int` is `i64`, `float` is `f64`,
  `T?` is `Option<T>`, `Result[T, E]` is `Result<T, E>`, a tuple is a tuple, `dict`/`set` are `BTreeMap`/`BTreeSet`).
  Borrow in the template when the Rust function wants a reference: `"my_crate::count(&{0})"`.
- The value of the Rust expression must have exactly the Rust type of the declared return type. Convert in the
  template: `"{0}.parse::<i64>().map_err(|e| e.to_string())"` for a `Result[int, str]`.
- An `extern fn` can be imported from a module like any function, and is called like any function.

## Using crates

Declare crates in `zylo.toml`, with the same syntax as Cargo:

```toml
[package]
name = "myapp"

[rust-dependencies]
rand = "0.8"
serde_json = { version = "1", features = ["std"] }
mylib = { path = "../mylib" }
```

```zylo
extern fn roll() -> int = "(rand::random::<u32>() % 6 + 1) as i64"
print(roll())
```

When a project has Rust dependencies, `zylo build` and `zylo run` generate a Cargo project in `.zylo-build/`
(next to `zylo.toml`) and build it with `cargo`; crates are downloaded and compiled the first time and cached
afterwards. Without Rust dependencies Zylo keeps calling `rustc` directly, which is faster. `--offline` makes
Cargo use only what is already downloaded. Allowed dependency keys: `version`, `features`, `default-features`,
`path`, `git`, `branch`, `tag`, `rev`, `package`.

### Crates brought by the standard library

A bundled module can need crates too: `import std.http` makes the project build with Cargo, using `tiny_http` and
`ureq` from a sidecar `http.toml`, and appends the Rust support code in `http.rs` to the program. Nothing changes for
programs that do not import such a module. If your `zylo.toml` lists one of those crates with a different requirement
the build stops with `conflicting requirements for crate`; use the same requirement or none. See
[stdlib.md](stdlib.md#modules-that-need-crates).

## Callbacks into Zylo

Passing a Zylo function value to an `extern fn` works: a parameter of type `fn(A) -> R` is an
`std::rc::Rc<dyn Fn(A) -> R>` in the template, so Rust code can call it (`{0}(x)`, or from a helper that takes that
type). Two rules follow from the representation: the `Rc` is not `Send`, so call it on the thread that received it,
and a panic inside it is an ordinary Rust panic that you can `catch_unwind` (`std.http` does, to answer 500).

## Packages and trust

A Zylo package from the community is plain Zylo code, and that is the point: it cannot run arbitrary Rust.

- The bundled standard library (`std.*`) is the exception: it is trusted and uses `extern fn` internally.
- A package that contains an `extern fn` is **rejected** unless your project trusts it.
- Crates declared by a package's own `zylo.toml` are **ignored** unless your project trusts it.

```toml
[rust]
trusted-packages = ["fastjson"]
```

Trust is a statement about the package's authors: compiling a crate runs its `build.rs` and procedural macros,
and `extern fn` can run anything, so a trusted package can do what you can do. Installing a package still never
runs code. Path dependencies are only allowed in your own `zylo.toml`.

## Limits

No Rust generics or traits are exposed to Zylo, `extern fn` cannot be a method, and Zylo structs are plain Rust structs named like the Zylo struct (derive `Clone`, `PartialEq`),
which you can name in templates but whose layout is not a stable interface.
