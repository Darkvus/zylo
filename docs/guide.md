# Zylo language guide

Code blocks marked `zylo` are complete programs that are type-checked by the test suite.

## Program structure

A program is a sequence of statements. Top-level statements run in order, like a script.
`fn` and `struct` definitions are allowed only at the top level, and a function may be used
before the line that defines it.

Blocks are made with indentation (spaces only, no tabs) after a `:`. Comments start with `#`.

```zylo
# Hello
fn greet(name: str):
    print("hello " + name)

greet("zylo")
```

## Types

| Type | Meaning | Rust |
|---|---|---|
| `int` | 64-bit signed integer | `i64` |
| `float` | 64-bit float | `f64` |
| `bool` | `true` / `false` | `bool` |
| `str` | text | `String` |
| `list[T]` | growable list | `Vec<T>` |
| `Name` | a struct you define | `struct` |
| `T?` | optional value (`T` or nothing) | `Option<T>` |
| `Result[T, E]` | a `T` or an error `E` | `Result<T, E>` |

There is **no implicit coercion**: `1 + 2.0` is an error. Convert explicitly with `int(x)`,
`float(x)` and `str(x)`.

## Variables

`x = 1` declares `x` the first time and reassigns it afterwards (the type cannot change).
Annotate when you want to be explicit, or when the type cannot be inferred (an empty list).
Variables are scoped to their block: one declared inside an `if` or loop does not exist after it.
Functions only see their own parameters and locals, never top-level variables.

```zylo
total = 0
names: list[str] = []
for i in range(3):
    total += i
append(names, "ana")
print(total)
print(len(names))
```

## Operators

- Arithmetic: `+ - * / %`. `int / int` is **integer division** (truncating, like Rust). `+` also joins strings.
- Comparison: `== != < > <= >=` (`<` and friends work on `int`, `float` and `str`; `==` works on every type).
- Logic: `and`, `or`, `not` (operands must be `bool`).
- Compound assignment: `+= -= *= /= %=`.

## Control flow

```zylo
fn classify(n: int) -> str:
    if n < 0:
        return "negative"
    elif n == 0:
        return "zero"
    else:
        return "positive"

i = 0
while i < 3:
    i += 1
    if i == 2:
        continue
    print(classify(i))

for k in range(1, 4):
    print(k)
for x in [10, 20]:
    print(x)
```

`range(n)` and `range(a, b)` can only appear as the iterable of a `for`.

## Functions

Parameters need types; the return type is optional (omit it for no value). A function with a
return type must return on every path.

```zylo
fn gcd(a: int, b: int) -> int:
    while b != 0:
        t = a % b
        a = b
        b = t
    return a

print(gcd(48, 18))
```

Arguments are passed **by value**: a function that changes a list or struct parameter changes its
own copy, never the caller's.

## Lists and strings

```zylo
xs = [3, 1, 2]
append(xs, 10)
xs[0] = 99
print(xs)          # [99, 1, 2, 10]
print(len(xs))     # 4
grid = [[1, 2], [3, 4]]
grid[1][0] = 7
print(grid)        # [[1, 2], [7, 4]]
s = "hello" + " " + "world"
print(len(s))      # 11 (characters, not bytes)
```

Assigning a list to another variable copies it. Strings cannot be indexed yet.

## Structs

```zylo
struct Point:
    x: float
    y: float

    fn dist2(self) -> float:
        return self.x * self.x + self.y * self.y

    fn move(self, dx: float, dy: float):
        self.x += dx
        self.y += dy

p = Point(3.0, 4.0)
print(p.dist2())    # 25.0
p.move(1.0, 1.0)
print(p)            # Point(x=4.0, y=5.0)
```

- The constructor is positional and takes every field in order.
- Structs are **values**: `q = p` copies. Passing one to a function copies it too.
- Methods take `self` first. The compiler infers whether a method modifies `self` (also when it
  does so by calling another method that does), so there is no `mutating` keyword.
- Fields can be structs or lists, and `==` compares structs field by field. A struct cannot contain
  itself directly; use a `list` of it.
- Structs and methods are defined at the top level.

## Option and Result

Zylo has no exceptions. A value that may be missing is `T?`; an operation that may fail is
`Result[T, E]`.

Constructors are `none`, `some(x)`, `ok(x)` and `err(e)`. They take their type from context —
an annotation, a parameter, a return type, a field — so an unannotated `none` or `ok(1)` is an error.

```zylo
fn find_positive(xs: list[int]) -> int?:
    for x in xs:
        if x > 0:
            return some(x)
    return none

print(find_positive([-1, 3]))     # Some(3)
print(find_positive([-1]))        # None
```

### Checking with `is` — and type narrowing

`x is some`, `x is none`, `r is ok` and `r is err` are boolean expressions. When you test a plain
variable in an `if`, its type is **narrowed** inside the branches:

```zylo
fn describe(r: Result[int, str]) -> str:
    if r is ok:
        return "value " + str(r)     # r is an int here
    return "error " + r              # ...and a str here (r is err)

x: Result[int, str] = ok(1)
print(describe(x))
```

**Early exit** narrows the rest of the block. This is the idiomatic way to unwrap:

```zylo
fn add_one(s: str) -> int?:
    n = parse_int(s)
    if n is none:
        return none
    return some(n + 1)       # n is a plain int from here on

print(add_one("41"))
```

Narrowing rules and limits:

- It applies to plain variables only (`x is some`), not to fields or call results.
- The condition must be exactly one `is` check (`x is some and y > 0` does not narrow).
- A narrowed variable cannot be reassigned inside the region where it is narrowed.
- For a `Result`, the `ok` branch sees the value and the `err` branch sees the error.

### The `?` operator

`expr?` unwraps an `Option` or `Result`, or returns early with `none` / the error. The enclosing
function must return an `Option` (for an `Option`) or a `Result` with the same error type
(for a `Result`).

```zylo
fn parse_pair(a: str, b: str) -> Result[int, str]:
    x = parse_int(a)
    y = parse_int(b)
    if x is none:
        return err("not a number: " + a)
    if y is none:
        return err("not a number: " + b)
    return ok(x + y)

fn double_sum(a: str, b: str) -> Result[int, str]:
    s = parse_pair(a, b)?
    return ok(s * 2)

print(double_sum("4", "5"))      # Ok(18)
```

### Methods

`x.unwrap()` returns the value or crashes (exit code 101). `x.unwrap_or(default)` returns the
value or the default.

`print` shows these values as `Some(3)`, `None`, `Ok(18)` and `Err("message")`.

## Built-in functions

| Function | Description |
|---|---|
| `print(x)` | print any value followed by a newline |
| `len(x)` | length of a `str` (characters) or a `list` |
| `append(xs, v)` | add `v` to the end of the list `xs` |
| `int(x)`, `float(x)`, `str(x)` | explicit conversions (`int("abc")` crashes; use `parse_int`) |
| `parse_int(s)` | `int?` — the integer in `s`, or `none` |
| `parse_float(s)` | `float?` — the float in `s`, or `none` |
| `some(x)`, `ok(x)`, `err(e)`, `none` | `Option` / `Result` constructors |
| `exit(code)` | stop the program with an exit code |
| `range(n)`, `range(a, b)` | only in a `for` |

## Runtime errors

These stop the program with exit code **101**: integer overflow (compile with
`--no-overflow-checks` to wrap instead), division by zero, list index out of range, `unwrap()` on
`none` or `err`, and `int(...)` / `float(...)` of text that is not a number.

## Known limitations

No modules or imports, no `dict` / `set` / tuples, no string indexing or methods, no closures or
generics, no standard input, files or command-line arguments. See the [roadmap](../ROADMAP.md).
