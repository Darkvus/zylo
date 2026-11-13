# Zylo

A small statically typed language with Python-like syntax that compiles to Rust.
Write code that reads like Python, get a native binary that runs 25–80× faster on compute-heavy code.

**Status:** v0.4 — early but complete enough to write real programs and share libraries. See the [roadmap](ROADMAP.md).

```zylo
fn fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

print(fib(35))
```

## Install

Zylo compiles through Rust, so install [Rust](https://rustup.rs) first. Then:

```
# Linux and macOS
curl -fsSL https://raw.githubusercontent.com/Darkvus/zylo/main/install.sh | sh

# Windows: download and run zylo-setup-x86_64.exe from the latest release
# Any system with Python 3.11+
pip install "zylo @ git+https://github.com/Darkvus/zylo.git"
```

Details, options and uninstalling: [docs/install.md](docs/install.md).

## Use

```
zylo check examples/fib.zy        # syntax and types only
zylo emit  examples/fib.zy        # print the generated Rust
zylo run   examples/fib.zy        # compile and run (returns the program's exit code)
zylo build examples/fib.zy -o fib --emit-rs
```

Errors point at the exact spot:

```
bad.zy:2:9: error: `+` between int and float: types must match (use float(x) or int(x) to convert)
  |
2 | print(x + 2.5)
  |         ^
```

## A quick tour

**Static types with local inference.** Annotate function signatures; everything else is inferred.
There is no implicit coercion: `1 + 2.0` is an error, `float(1) + 2.0` is not.

**Structs and methods.** Structs are values; methods receive `self` by reference and the compiler
infers whether they modify it.

```zylo
struct Account:
    owner: str
    balance: int

    fn deposit(self, amount: int):
        self.balance += amount

acc = Account("Ana", 100)
acc.deposit(50)
print(acc.balance)   # 150
```

**Explicit errors with `Option` and `Result`.** No exceptions. `T?` is an optional value,
`Result[T, E]` is a value or an error. `is` checks them and narrows the type, and `?` propagates.

```zylo
fn parse_pair(a: str, b: str) -> Result[int, str]:
    x = parse_int(a)
    y = parse_int(b)
    if x is none:
        return err("not a number: " + a)
    if y is none:
        return err("not a number: " + b)
    return ok(x + y)          # x and y are plain ints here

fn double_sum(a: str, b: str) -> Result[int, str]:
    s = parse_pair(a, b)?     # on error, return it to the caller
    return ok(s * 2)
```

**Modules and community packages.** Split a program over files with `import`, and install libraries
from git into a per-project virtual environment, pinned by a lock file:

```
zylo init myapp
zylo venv                                # create .zylo-env/
zylo add github.com/ana/textkit@v1.0.0   # installs it and records the exact commit in zylo.lock
zylo run src/main.zy                     # import textkit, from textkit import shout...
```

**Good tooling.** `zylo check` reports every error at once, `zylo fmt` formats your code without ever changing its
meaning, and `zylo lsp` plus a Visual Studio Code extension give you live diagnostics, formatting, an outline,
completion (members after a dot, named arguments, imports), hover, go to definition, rename, signature help and quick fixes in your editor ([docs/editors.md](docs/editors.md)).

**Rust when you need it.** `extern fn` calls any Rust function, and `[rust-dependencies]` in `zylo.toml` brings in
crates. Community packages stay plain Zylo unless you explicitly trust them
([docs/rust-interop.md](docs/rust-interop.md)).

The full language is documented in the [language guide](docs/guide.md); design decisions and their
reasons are in [docs/design.md](docs/design.md).

## Performance

The same programs in Zylo, hand-written Rust and CPython (median of 7 runs; Rust and Zylo are built with the same
`rustc -O` flags):

| Program | Python | Rust | Zylo | Zylo vs Python | Zylo vs Rust |
|---|---:|---:|---:|---:|---:|
| fib(35), recursive | 0.929 s | 0.036 s | 0.035 s | **26×** faster | 0.98× |
| primes below 300 000 | 0.600 s | 0.020 s | 0.018 s | **33×** faster | 0.94× |
| bubble sort, 3 000 items | 0.325 s | 0.004 s | 0.004 s | **80×** faster | 1.11× |
| a list passed to a function 1 000 times | 0.088 s | 0.007 s | 0.031 s | **3×** faster | 4.09× |
| arithmetic loop, 20 M iterations | 1.258 s | 0.016 s | 0.016 s | 81× faster (*) | 1.00× |

"Zylo vs Rust" is Zylo's time divided by Rust's, so 1.00× is the same speed. The one real gap is the string-list
loop, which clones every string it visits.

(*) LLVM optimizes this trivial loop very aggressively, so treat it as an upper bound.
Method and the variant without overflow checks: [benchmarks/RESULTS.md](benchmarks/RESULTS.md).
Reproduce with `python benchmarks/bench.py`.

## Examples

Nineteen programs in [examples/](examples/) — FizzBuzz, primes, Collatz, sorting, binary search,
matrices, a bank account, an inventory, vectors, number parsing with `Option`/`Result`, and more.
Every one is compiled and checked in the test suite.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md) for how the code is organised. Zylo is licensed under the MIT license.
