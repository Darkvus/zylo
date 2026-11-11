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

## Packages and trust

A Zylo package from the community is plain Zylo code, and that is the point: it cannot run arbitrary Rust.

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

No Rust generics or traits are exposed to Zylo, `extern fn` cannot be a method, callbacks from Rust into Zylo are
not supported, and Zylo structs are plain Rust structs named like the Zylo struct (derive `Clone`, `PartialEq`),
which you can name in templates but whose layout is not a stable interface.
