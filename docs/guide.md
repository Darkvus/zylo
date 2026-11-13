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
| `tuple[A, B, ...]` | fixed group of 2+ values | `(A, B, ...)` |
| `dict[K, V]` | key → value map, sorted by key | `BTreeMap<K, V>` |
| `set[T]` | set of distinct values, sorted | `BTreeSet<T>` |
| `Cell[T]` | a shared, mutable box holding one `T` (see [Shared state](#shared-state-with-cell)) | `Rc<RefCell<T>>` |
| `Name` | a struct you define | `struct` |
| `Name[T]` | an instance of a generic struct or enum (see [Generics](#generics)) | one `struct` per `T` |
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
- Membership: `x in c` and `x not in c` for a list, `str` (substring), `set` or `dict` (keys).
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

### Default and named arguments

A parameter can have a default value, written `name: type = value`. Callers may leave it out, and may
pass any argument **by name**.

```zylo
fn greet(name: str, greeting: str = "Hello", times: int = 1) -> str:
    return f"{greeting}, {name}!".repeat(times)

print(greet("Ana"))                       # Hello, Ana!
print(greet("Ana", "Hi"))                 # Hi, Ana!
print(greet("Ana", times=2))              # Hello, Ana!Hello, Ana!
print(greet(times=3, name="Bo", greeting="Yo"))
```

- Parameters with a default must come **after** the ones without.
- In a call, positional arguments come first, then named ones (`name=value`). Every parameter is given at most
  once, and every parameter without a default must be given.
- A default must be a compile-time constant: a number, string, `true` / `false`, `none`, an empty list `[]` or dict
  `{}`, an enum variant (`Color.Red`, `Shape.Circle(1.0)`), or a tuple of those. There is no shared mutable default as in Python: every call gets a fresh value.
- Arguments are evaluated in **parameter order**, not in the order written, so `f(b=g(), a=h())` calls `h` first.
- Methods work the same way (`p.move(dy=1.0)`). Built-in functions and variant constructors take positional
  arguments only.

## Functions are values

A function can be passed around, stored and returned. Its type is written `fn(A, B) -> R` (`fn(A)` when it returns
nothing, `fn() -> R` when it takes nothing) and can be used anywhere a type can: parameters, return types, struct
fields, and the elements of lists, dicts, tuples, `Option` and `Result`.

```zylo
fn double(x: int) -> int:
    return x * 2

fn apply(f: fn(int) -> int, x: int) -> int:
    return f(x)

fn twice(f: fn(int) -> int) -> fn(int) -> int:
    return fn(x: int) => f(f(x))

print(apply(double, 3))          # 6: a top-level function used as a value
print(twice(double)(5))          # 20: calling the result of a call
ops: dict[str, fn(int, int) -> int] = {"add": fn(a: int, b: int) => a + b, "mul": fn(a: int, b: int) => a * b}
print(ops["add"](2, 3), ops["mul"](2, 3))
```

Write `(fn(int) -> int)?` for an optional function: a `?` after a function type would otherwise belong to its return type.

### Anonymous functions

`fn(params) => expression` is a function without a name. Parameter types are required, unless the context already says
what the function must be (an argument of a function or built-in method, a typed variable...), and then they may be
left out. The return type is inferred from the expression; write `-> T` before the `=>` to state it.

```zylo
inc = fn(x: int) => x + 1
parse = fn(s: str) -> int? => parse_int(s)
print(inc(1))                    # 2
print([1, 2, 3].map(fn(x) => x * x))
```

I chose `=>` after the parameter list over Python's `lambda x: ...` for three reasons: it keeps the word `fn` for
every kind of function, so the same parameter and type syntax is used everywhere; a `:` would be ambiguous inside a
dict literal (`{"k": fn(x: int): x}`) and in `if`/`while` headers; and it makes a one-expression body look like a
value, which is what a callback is. The body is a single expression and ends at the next `,` or `)`.

For a function that needs several statements, define it inside another function with a normal `fn`. It is an ordinary function that can use the variables around it, and its name is a variable holding it:

```zylo
fn make_counter_report(limit: int) -> list[str]:
    fn label(n: int) -> str:
        if n > limit:
            return "big"
        return "small"
    return [1, 5, 9].map(label)

print(make_counter_report(4))    # ["small", "big", "big"]
```

A local `fn` is defined when the line runs, so it cannot call itself (its name does not exist yet inside its own
body); use a top-level function for recursion.

### Closures capture copies

An anonymous or local function can use the variables around it. Like everything in Zylo it captures **values**: when the
function is created it takes a copy of each variable it uses.

```zylo
n = 10
add_n = fn(x: int) => x + n
n = 99
print(add_n(1))                  # 11: it kept the n of 10
```

So a change made after the function was created is not seen inside, and a function can never change the variables it
captured. The checker rejects an assignment (`n = 1`, `xs[0] = 1`, `xs.append(1)`...) to a captured variable inside the
function, because it would silently change a copy; build and return a new value instead, or share the state through a
[`Cell`](#shared-state-with-cell). Parameters and variables
declared inside the function are its own and can be changed freely. `self` cannot be captured: copy what you need into a
local first (`size = self.size`).

### Shared state with `Cell`

Because closures capture copies, two of them cannot share a counter or a list. A `Cell[T]` is a box that can be
shared: `cell(value)` creates one, and **copies of a cell share the same storage**, so a closure that captures it, a
struct that holds it and a function that receives it all see the same value.

```zylo
fn make_counter(count: Cell[int]) -> fn() -> int:
    fn next() -> int:
        count.update(fn(n: int) => n + 1)
        return count.get()
    return next

hits = cell(0)
a = make_counter(hits)
b = make_counter(hits)
print(a(), b(), a(), hits.get())     # 1 2 3 3

names: Cell[list[str]] = cell([])    # an empty literal needs the annotation
names.set(["ana"])
```

| Expression | Meaning |
|---|---|
| `cell(v)` | a new cell holding `v` (any type that has a value) |
| `c.get() -> T` | a **copy** of the current value (changing that copy, for example appending to a list, does not change the cell) |
| `c.set(v)` | replace the value; `v` must be a `T` |
| `c.update(fn(T) -> T)` | replace the value with the function's result |

`Cell[T]` is an ordinary type: it works as a variable annotation, a function parameter or result and a struct field,
and structs holding cells still compare and print. Assigning a cell (`b = a`) or passing it does not copy the box, only
the handle. Two cells are equal when their values are equal (identity does not matter), and a cell prints as
`Cell(value)`. A cell is not JSON data: `to_json` rejects a struct that holds one. Cells are single-threaded (they are
`Rc<RefCell<T>>`); the server in `std.http` runs handlers on one thread, so handlers can share a cell.

### Calling function values

`f(x)` calls a variable of function type, `handlers["GET"](req)` the result of any expression, and
`self.handler(req)` a field of function type (the field and a method cannot share a name, so there is no ambiguity).

```zylo
struct Button:
    label: str
    on_click: fn(str) -> str

    fn click(self) -> str:
        return self.on_click(self.label)

b = Button("ok", fn(s: str) => f"clicked {s}")
print(b.click())
print(b.on_click("direct"))
```

Function values are cheap to copy (they are reference counted), so passing and storing them never clones the code.
They cannot be compared or printed: `==`, `print`, f-strings and `str()` reject a function, or a struct, enum or
collection that contains one. A struct holding a function still works as any other struct otherwise. To use a method as a
value, wrap it: `fn(x: int) => counter.add(x)` (the receiver is captured by copy). Built-in and `extern` functions are
wrapped the same way.

## Lists

```zylo
xs = [3, 1, 2]
xs.append(10)
xs[0] = 99
print(xs)              # [99, 1, 2, 10]
print(xs[1:3])         # [1, 2]
print(xs[-1], xs[:-1]) # 10 [99, 1, 2]
grid = [[1, 2], [3, 4]]
grid[1][0] = 7
print(grid)            # [[1, 2], [7, 4]]
print(2 in xs, len(xs))
```

- Indexing starts at 0 and is checked: an out-of-range index stops the program (exit code 101).
  A negative index counts from the end (`xs[-1]` is the last element) when the list is a variable or field.
- Slices `xs[a:b]`, `xs[:b]`, `xs[a:]`, `xs[:]` never fail: bounds are clamped, and may be negative.
- Assigning a list to another variable copies it.
- Methods: `append(v)`, `pop()` (an `int?`: `none` on an empty list), `insert(i, v)`, `remove(v)` (a `bool`:
  was it there?), `clear()`, `reverse()`, `sort()`, `extend(other)`, `index(v)` (an `int?`), `count(v)`.
  `append(xs, v)` is the same as `xs.append(v)`.
- Methods that take a function (see [Functions are values](#functions-are-values)); `T` is the element type:

| Method | Result |
|---|---|
| `xs.map(f)` | `f: fn(T) -> U`; a new `list[U]` |
| `xs.filter(f)` | `f: fn(T) -> bool`; a new list with the elements for which it is true |
| `xs.fold(init, f)` | `f: fn(U, T) -> U`; folds from the left starting at `init`; returns `U` |
| `xs.reduce(f)` | `f: fn(T, T) -> T`; like `fold` with the first element as start; a `T?` (`none` when empty) |
| `xs.sort_by(key)` | `key: fn(T) -> K` (`K` int, float, str, bool or tuples of those); sorts in place, stable |
| `xs.any(f)`, `xs.all(f)` | `f: fn(T) -> bool`; a `bool` (`all` of an empty list is true) |
| `xs.find(f)` | `f: fn(T) -> bool`; the first element for which it is true, a `T?` |

```zylo
words = ["pear", "fig", "banana", "kiwi"]
lengths = words.map(fn(w) => len(w))
long_ones = words.filter(fn(w) => len(w) > 3)
total = lengths.fold(0, fn(acc, n) => acc + n)
words.sort_by(fn(w) => len(w))
print(lengths, long_ones, total, words)
print(words.any(fn(w) => w == "fig"), words.all(fn(w) => len(w) > 3), words.find(fn(w) => w.startswith("b")))
```

## Strings

```zylo
name = "Ana"
n = 3
print(f"hi {name}! {n + 1} {2.5:.2f} {n:>4} {n:03d}")   # f-strings
print("tab\there", 'single quotes', "a" + "b")
s = "  Hello, World  ".strip()
print(s.upper(), s.split(", "), s[0:5], s[-1])
print(",".join(["a", "b"]), s.replace("World", "Zylo"), s.find("World"))
print("lo" in s, len("héllo"))
for ch in "hi":
    print(ch)
```

- Strings use `"` or `'`. Escapes: `\n \t \r \0 \\ \" \'`.
- **f-strings** `f"... {expr} ..."` accept any expression, and a format spec after `:` — a width and
  alignment (`>8`, `<8`, `^8`, `05`), a precision (`.2`) and a type (`f`, `d`, `s`, `x`, `X`, `b`).
  Write `{{` and `}}` for literal braces. Inside an f-string use the other quote type for nested strings.
- Strings are indexed and sliced by **character**: `s[i]` is a one-character `str`; `len(s)` counts characters.
- Methods: `strip()`, `upper()`, `lower()`, `split()` (on whitespace) or `split(sep)`, `join(list)` (called on the
  separator), `replace(a, b)`, `startswith(p)`, `endswith(p)`, `find(sub)` (an `int?`), `count(sub)`, `lines()`,
  `chars()`, `repeat(n)`.
- `x in s` tests for a substring. Iterating over a string yields its characters.

## Tuples

```zylo
fn divmod(a: int, b: int) -> tuple[int, int]:
    return a / b, a % b

q, r = divmod(17, 5)
print(q, r)                  # 3 2
a, b = 1, 2
a, b = b, a                  # swap
pair = (1, "x")
print(pair, pair[1])         # (1, "x") x
for n, s in [(1, "a"), (2, "b")]:
    print(n, s)
```

A tuple groups two or more values of any types: `tuple[int, str]`. It is immutable, compared field by
field, and indexed with a **literal** (`t[0]`). `a, b = ...` unpacks into variables (new or existing), also in
a `for` header and in `return a, b`.

## Dicts and sets

```zylo
ages = {"ana": 30, "bob": 25}
ages["cy"] = 41
ages["ana"] += 1
print(ages)                           # {"ana": 31, "bob": 25, "cy": 41}
print("bob" in ages, len(ages))
for name, age in ages.items():
    print(name, age)
print(ages.get("zed"), ages.get("zed", 0))   # None 0
tags: set[str] = {"b", "a"}
tags.add("c")
print(tags, "a" in tags)              # {"a", "b", "c"} true
empty: dict[str, list[int]] = {}
```

- `dict[K, V]` and `set[T]`; keys and elements must be `int`, `str`, `bool` or tuples of those.
- **Iteration and printing are in sorted key order**, so output is deterministic (this differs from Python,
  which keeps insertion order). Iterating a dict yields its keys.
- `d[k]` on a missing key stops the program (exit code 101); use `d.get(k)` (a `V?`) or `d.get(k, default)`.
- `{}` and `{x}` need context to tell dict from set: annotate empty ones (`d: dict[str, int] = {}`).
- Dict methods: `get`, `keys()`, `values()`, `items()` (a list of `(key, value)` tuples), `pop(k)` (a `V?`),
  `clear()`. Set methods: `add(v)`, `remove(v)` (a `bool`), `union(o)`, `intersection(o)`, `difference(o)`, `clear()`.

## Input, output and files

```zylo
args = argv()                       # the arguments after the program: `zylo run prog.zy a b`
name = input("name? ")              # prints the prompt, reads one line
print(f"hello {name}, you passed {len(args)} arguments")

total = 0
while true:
    line = read_line()              # a str?: none at the end of the input
    if line is none:
        break
    n = parse_int(line)
    if n is none:
        eprint("skipping", line)    # to standard error
        continue
    total += n
print(total)

result = write_file("notes.txt", "one\ntwo\n")
if result is err:
    print("could not write:", result)
else:
    print(read_file("notes.txt"))   # Ok("one\ntwo\n")
print(env("HOME").unwrap_or("?"), file_exists("notes.txt"))
```

- **Arguments:** `zylo run prog.zy a b` passes `a` and `b` to the program. A built binary receives them directly.
  Put `--` first to pass something that looks like an option.
- **Input:** `input(prompt)` reads a line without its line ending and stops the program (exit code 101) at the end of the
  input; `read_line()` returns `none` instead; `read_stdin()` reads everything.
- **Files** return a `Result` whose error is a message that includes the path, so failures are values, not crashes:
  `read_file(path)` (`Result[str, str]`), `write_file(path, text)` and `append_file(path, text)` (`Result[int, str]`: the
  number of characters written), `list_dir(path)` (`Result[list[str], str]`, sorted) and `file_exists(path)` (`bool`).
- **Environment:** `env(name)` is a `str?`. `eprint(...)` is `print` for standard error.
- **Time:** `time()` is the Unix time in seconds (a `float`), `sleep(seconds)` pauses.

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

- The constructor takes the fields in order, positionally or by name (see below).
- Structs are **values**: `q = p` copies. Passing one to a function copies it too.
- Methods take `self` first. The compiler infers whether a method modifies `self` (also when it
  does so by calling another method that does), so there is no `mutating` keyword.
- Fields can be structs or lists, and `==` compares structs field by field. A struct cannot contain
  itself directly; use a `list` of it.
- Structs and methods are defined at the top level.

### Field defaults and named constructor arguments

A field may have a default value (a compile-time constant, like a parameter default). The constructor then lets
you skip it, and any field can be passed by name.

```zylo
struct Config:
    host: str = "localhost"
    port: int = 8080
    tags: list[str] = []
    debug: bool = false

a = Config()
b = Config(port=9000, debug=true)
c = Config("example.com", 80)
print(a)    # Config(host="localhost", port=8080, tags=[], debug=false)
print(b.port, c.host)
```

Fields with defaults must come after the ones without, exactly like parameters.

## Enums and `match`

An enum is a value that is exactly one of several **variants**, and each variant can carry its own data.

```zylo
enum Shape:
    Circle(float)
    Rect(float, float)
    Empty

fn area(s: Shape) -> float:
    match s:
        Shape.Circle(r):
            return 3.0 * r * r
        Shape.Rect(w, h):
            return w * h
        Shape.Empty:
            return 0.0

print(area(Shape.Rect(2.0, 3.5)))    # 7.0
print(Shape.Circle(1.0))             # Circle(1.0)
```

- Build a variant with the enum's name: `Shape.Circle(2.0)`, or `Shape.Empty` when it carries nothing.
- `match` takes one arm per variant, written `Enum.Variant(a, b):`; the names bind the variant's data, in order, and
  live only inside the arm. Use `_` as a name to ignore one value.
- A `match` must cover **every** variant, or end with a `_:` arm for the rest. Forgetting a variant is a compile
  error, so adding a variant to an enum shows you everything that has to change.
- A `_` arm that could never run (everything is already matched) and a variant matched twice are errors too.
- Enums are values, like structs: they are copied when assigned or passed, compare with `==`, and can be stored in
  lists, dicts' values, structs, `Option` and `Result`. An enum cannot contain itself directly; use a `list`.
- Enums, like structs, are defined at the top level and can be imported: `sh.Shape.Circle(1.0)`.
- `match` also works on `int`, `str`, `bool`, `Option` and `Result` (see below).

### Methods on enums

An enum can have methods, written like struct methods after the variants. `self` is the enum value, and the usual
body is a `match self:`.

```zylo
enum Shape:
    Circle(float)
    Rect(float, float)
    Empty

    fn area(self) -> float:
        match self:
            Shape.Circle(r):
                return 3.0 * r * r
            Shape.Rect(w, h):
                return w * h
            Shape.Empty:
                return 0.0

    fn scaled(self, k: float = 2.0) -> Shape:
        match self:
            Shape.Circle(r):
                return Shape.Circle(r * k)
            Shape.Rect(w, h):
                return Shape.Rect(w * k, h * k)
            _:
                return self

print(Shape.Rect(2.0, 3.0).scaled().area())    # 24.0
```

Enum values are immutable, so a method cannot change `self`; it returns a new value instead.

### Matching on values, `Option` and `Result`

`match` also takes an `int`, `str` or `bool` and matches literals, and takes an `Option` or `Result` and matches
`some(x)` / `none` and `ok(v)` / `err(e)`, binding the inner value.

```zylo
fn describe(n: int) -> str:
    match n:
        0:
            return "zero"
        1:
            return "one"
        -1:
            return "minus one"
        _:
            return "many"

fn command(s: str) -> int:
    match s:
        "start":
            return 1
        "stop":
            return 2
        _:
            return 0

fn show(x: int?) -> str:
    match x:
        some(v):
            return f"got {v}"
        none:
            return "nothing"

fn unwrap_or_zero(r: Result[int, str]) -> int:
    match r:
        ok(v):
            return v
        err(_):
            return 0

fn yes_no(b: bool) -> str:
    match b:
        true:
            return "yes"
        false:
            return "no"

print(describe(1), command("stop"), show(some(3)), unwrap_or_zero(err("x")), yes_no(true))
```

- Literal arms are integers (optionally negative), strings and `true` / `false`. Floats are not matched (equality on
  floats is a trap); use `if`.
- A `match` on an `int` or `str` must end with a `_:` arm. A `match` on a `bool` is complete with both `true` and
  `false`; `Option` needs `some` and `none`, `Result` needs `ok` and `err`; otherwise add `_:`.
- As with enums, an arm that can never run (a repeated literal, a `_` after everything is covered) is an error.
- `some(x)`, `ok(v)` and `err(e)` bind exactly one name (`_` ignores it). The bound names live inside the arm.
- `is some` / `is ok` narrowing is unchanged and still the lighter way to test a single case.

### Guards and or-patterns

An arm can carry a guard, `Pattern if condition:`, which may use the names the pattern binds. Several patterns can
share one arm with `|`.

```zylo
enum Color:
    Red
    Green
    Blue
    Rgb(int, int)
    Gray(int)

fn describe(c: Color) -> str:
    match c:
        Color.Red | Color.Green:
            return "warm-ish"
        Color.Rgb(a, b) if a > b:
            return "a is bigger"
        Color.Rgb(a, _) | Color.Gray(a):
            return f"level {a}"
        Color.Blue:
            return "blue"

fn size(n: int) -> str:
    match n:
        0 | 1:
            return "tiny"
        _ if n < 0:
            return "negative"
        _:
            return "big"

print(describe(Color.Red), describe(Color.Gray(4)), size(1), size(-3))
```

- A guard must be a `bool`. Guards work on every kind of pattern, including `_` and literals.
- An arm with a guard does **not** count toward exhaustiveness, because the guard may reject the value: another arm
  (or a final `_:`) must still cover the case. A `_ if cond:` arm is not a catch-all.
- In an or-pattern every alternative must bind the same names with the same types (`Color.Rgb(a, _) | Color.Gray(a)`
  is fine; `Color.Rgb(a, b) | Color.Gray(a)` is an error). Alternatives that bind nothing, such as literals and
  data-less variants, combine freely. `_` cannot be an alternative, since it already matches everything.
- Repeating a value or variant that an earlier unguarded arm (or alternative) already matched is an error.
- With a guard, the whole arm is `Pattern | Pattern if cond:`: the guard applies to all alternatives.

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

**Fields narrow too.** A field path (`p.name`, `self.limit`, `req.user.email`) can be tested like a variable, so you
no longer copy it to a local first:

```zylo
struct Patch:
    name: str?
    age: int?

fn describe(p: Patch) -> str:
    if p.name is some:
        return "name " + p.name          # p.name is a plain str here
    return "no name"

fn next_age(p: Patch) -> int:
    if p.age is none:
        return -1
    return p.age + 1                     # narrowed for the rest of the block

print(describe(Patch(name=some("Ada"), age=none)))
print(next_age(Patch(name=none, age=some(41))))
```

Narrowing rules and limits:

- It applies to plain variables (`x is some`) and to field paths made only of names and fields (`p.name`, `self.a.b`),
  not to call results or indexed elements (`xs[0].name`).
- The condition must be exactly one `is` check (`x is some and y > 0` does not narrow).
- A narrowed variable cannot be reassigned inside the region where it is narrowed.
- A narrowed field path is narrowed only while nothing can change it. In the region where it holds (the branch, or the
  rest of the block after an early exit) it is an error to assign the path, a part of it, or its owner (`p.name = ...`,
  `p = ...`), or to call a method that modifies the owner (`p.clear()`). Writing a sibling field
  (`p.items.append(1)`, `p.n = 2`) is fine.
- Closures and local functions do not see a field narrowing: copy the field to a local first.
- For a `Result`, the `ok` branch sees the value and the `err` branch sees the error.

### Multi-line literals

Inside `(...)`, `[...]` or `{...}` a line break does not end the statement, and indentation does not matter, so long
lists, dicts, tuples and calls can be written one item per line. A trailing comma is allowed. Comments and blank lines may
appear between the items.

```zylo
routes = {
    "home": [1, 2],
    "about": [
        3,
        4,
    ],
}
point = (
    10,
    20,
)
print(len(routes), point)
```

A line that is not indented past the line that opened a bracket and does not start with a closing bracket is taken as
the start of the next statement, so a forgotten `)` is reported where it happened. `zylo fmt` prints such a literal on
one line when it fits in 88 columns and one item per line otherwise (see [the formatter](editors.md#the-formatter)).

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

## Generics

A function, struct or enum can take **type parameters** in square brackets, so one definition works for many types:

```zylo
fn first[T](xs: list[T]) -> T:
    return xs[0]

struct Box[T]:
    value: T

    fn get(self) -> T:
        return self.value

    fn set(self, v: T):
        self.value = v

enum Maybe[T]:
    Just(T)
    Nothing

    fn or_else(self, fallback: T) -> T:
        match self:
            Maybe.Just(x):
                return x
            Maybe.Nothing:
                return fallback

print(first([1, 2, 3]), first(["a", "b"]))     # 1 a
b = Box(value=1.5)
b.set(b.get() * 2.0)
print(b)                                        # Box(value=3.0)
print(Maybe.Just(4).or_else(0))                 # 4
```

**Type arguments are inferred.** At a call the compiler matches each argument's type with the declared parameter type
(`list[T]` with `list[int]` gives `T = int`); every use of the same `T` must agree. What the arguments do not decide
comes from the type the context expects, and an argument that cannot be typed alone (`none`, `[]`, an anonymous function
whose parameter types depend on `T`) is checked last:

```zylo
struct Box[T]:
    value: T

enum Maybe[T]:
    Just(T)
    Nothing

fn map_all[T, U](xs: list[T], f: fn(T) -> U) -> list[U]:
    out: list[U] = []
    for x in xs:
        append(out, f(x))
    return out

fn or_default[T](x: T?, fallback: T) -> T:
    return fallback

lengths = map_all(["a", "bb"], fn(s) => len(s))   # list[int]: U comes from the function's result
nothing: Maybe[str] = Maybe.Nothing                # T comes from the annotation
empty: Box[list[int]] = Box(value=[])
print(or_default(none, 3))                         # 3
```

A type parameter that nothing determines is an error that tells you so (`cannot infer type parameter T of make`).
**Write the arguments yourself** when you want to, or must: `first[int](xs)`, `Box[str](value="x")`,
`Maybe[int].Nothing`, `util.first[Point](points)`, and in types `Box[int]`, `Pair[str, list[int]]`. The brackets after a
name are read as type arguments when they hold types (built-in names or capitalised names) and a `(` or `.` follows;
`table[key](x)` still indexes a variable. A function cannot be indexed, so once the compiler knows that a name is a
function, the brackets after it are type arguments whatever they hold: `first[point](xs)` and `pick[point, int](a, 1)`
work with a lowercase struct even when `first` comes from another module.

**The body is checked once, for every type.** Inside a generic definition a `T` can be stored, passed around and returned,
but nothing more: `a > b`, `a + b`, `print(a)` or `a == b` on a plain `T` are errors, because some type might not allow them.
Ask for what you need with a **bound**: `T: Ord`, or several with `+`:

| Bound | Gives `T` | Accepted types |
|---|---|---|
| `Eq` | `==`, `!=`, `in` | anything without a function inside |
| `Show` | `print`, f-strings | anything without a function inside |
| `Ord` | `<` `>` `<=` `>=`, `min`, `max`, `sorted` | `int`, `float`, `str` |
| `Num` | arithmetic, `abs`, `float(x)`, `sum` | `int`, `float` |
| `Key` | dict keys and set elements | `int`, `str`, `bool`, tuples of those |

`Ord`, `Num` and `Key` include `Eq` and `Show` (and `Num`, `Key` include `Ord`).

```zylo
fn largest[T: Ord](xs: list[T]) -> T:
    best = xs[0]
    for x in xs:
        if x > best:
            best = x
    return best

fn describe[T: Eq + Show](a: T, b: T) -> str:
    if a == b:
        return f"{a} equals {b}"
    return f"{a} differs from {b}"

struct Counter[K: Key]:
    counts: dict[K, int]

    fn add(self, k: K):
        if k in self.counts:
            self.counts[k] = self.counts[k] + 1
        else:
            self.counts[k] = 1

print(largest([3, 9, 4]), largest(["pear", "apple"]))   # 9 pear
print(describe("a", "b"))                               # a differs from b
```

Using a type that lacks the bound is reported where you call: `largest([Point(...)])` says that `Point` cannot be used
for `T`, which is bound by `Ord`.

### Generic methods

A method can have type parameters of its own, besides those of its struct or enum. They are written after the method's
name and inferred at the call like those of a function (or written: `b.map[str](f)`):

```zylo
struct Box[T]:
    value: T

    fn map[U](self, f: fn(T) -> U) -> Box[U]:
        return Box(value=f(self.value))

    fn pair[U](self, other: U) -> tuple[T, U]:
        return (self.value, other)

enum Maybe[T]:
    Just(T)
    Nothing

    fn map[U](self, f: fn(T) -> U) -> Maybe[U]:
        match self:
            Maybe.Just(x):
                return Maybe.Just(f(x))
            Maybe.Nothing:
                return Maybe.Nothing

struct Log:
    count: int

    fn tag[T: Show](self, x: T) -> str:
        return f"{x}#{self.count}"

b = Box(value=21)
print(b.map(fn(x) => x * 2).value)             # 42
print(b.map(fn(x) => f"<{x}>").value)          # <21>
print(b.map[float](fn(x) => float(x)).value)   # 21.0
print(b.pair("a"))                             # (21, "a")
print(Maybe.Just(2).map(fn(x) => x > 1))       # Just(true)
print(Log(count=3).tag(2.5))                   # 2.5#3
```

A method's parameters may have bounds (`[T: Show]`) and must not reuse a name the struct or enum already declares. The
body is checked once for every type, like any generic. Each list of type arguments a program uses makes an ordinary
method of the struct or enum (`Box[int].map[str]`).

### Generic functions as values

A generic function can be passed around once its type arguments are decided: written (`first[int]`), or taken from the
function type the context expects (a variable with a type, a parameter, a return type). It is an ordinary function value
after that.

```zylo
fn first[T](xs: list[T]) -> T:
    return xs[0]

fn apply_all[T, U](xs: list[T], f: fn(T) -> U) -> list[U]:
    out: list[U] = []
    for x in xs:
        append(out, f(x))
    return out

g = first[int]
h: fn(list[str]) -> str = first
print(g([4, 5]), h(["p", "q"]))                 # 4 p
print(apply_all([[1, 2], [3]], first))          # [1, 3]
```

`g = first` alone is an error: nothing says which `T` is meant. Bounds are checked for the chosen types.

### Numbers in generics

An integer literal can take the type of a parameter bounded by `Num`: it is an `int` when `T` is `int` and a `float`
when `T` is `float`. The other operand (or the declared type, or the other arguments of the call) says which `T` it is:

```zylo
fn add_one[T: Num](a: T) -> T:
    return a + 1

fn total[T: Num](xs: list[T]) -> T:
    acc: T = 0
    for x in xs:
        acc = acc + x
    return acc

fn clamp[T: Num](x: T, low: T, high: T) -> T:
    if x < low:
        return low
    if x > high:
        return high
    return x

print(add_one(1), add_one(1.5))                 # 2 2.5
print(total([1, 2, 3]), total([0.5, 0.25]))     # 6 0.75
```

Only integer literals work this way (a float literal would be wrong for `T = int`: compute in `float(a)` instead), and
only with `Num`: `a + 1` on a `T: Ord` or a plain `T` is an error that says so.

### How generics compile

Generics are **monomorphized**: for every list of type arguments a program uses, the compiler makes a copy of the
definition for those types (`first[int]`, `Box[str]`) and compiles it like hand-written code, so generic code is exactly
as fast as the specialised version and nothing is boxed. The same goes for `to_json`, `from_json` and `json_schema` on a
generic struct (`Pair[int, Point]` has its own functions, and appears in schemas as `Pair_int_Point`). Generics work
across modules and re-exports. See [design.md](design.md#generics) for the reasoning.

- A generic function used as a value needs its type arguments decided (`first[int]`, or an expected function type);
  `g = first` is an error.
- A generic that keeps instantiating itself with bigger types (`f[T]` calling `f[list[T]]`) is stopped with an error
  after 12 levels of nesting.
- There are no user-defined bounds (traits) and no default type arguments. Float literals cannot be a `T`.
- In the editor, hovering a name inside a generic body shows its type with the bounds of the parameters it mentions and
  the types the program instantiated them with.

## Typed JSON

`to_json`, `from_json`, `json_schema` and `json_schema_defs` convert between your own types and the `Json` value of
[`std.json`](stdlib.md#stdjson). They are compiler-known built-ins, not generic functions: the type is worked out at
compile time and the compiler writes the conversion code for it (ordinary Zylo functions, one per type). `std.json` is
loaded for you when a program uses them.

```zylo
import std.json as json

enum Role:
    Admin
    Guest

struct User:
    "A registered user."
    name: str
    "Display name."
    age: int
    nickname: str?
    role: Role = Role.Guest
    tags: list[str] = []

u = User("Ana", 31, none, Role.Admin, ["a", "b"])
j = to_json(u)
print(json.stringify(j))
# {"age":31,"name":"Ana","nickname":null,"role":"Admin","tags":["a","b"]}

back = from_json(User, j)                     # Result[User, str]
print(back == ok(u))                          # true

bad = json.parse('{"name": "Bo", "age": "x"}').unwrap()
print(from_json(User, bad))                   # Err("$.age: expected int, got string")
print(from_json(list[int], json.parse("[1, 2.5]").unwrap()))   # Err("$[1]: expected int, got 2.5 (not a whole number)")
print(json.pretty(json_schema(User)))         # the JSON Schema of User
```

- `to_json(value) -> Json` works on `int`, `float`, `bool`, `str`, `list[T]`, `set[T]`, `tuple[...]`, `dict[str, T]` and
  `dict[int, T]`, `T?`, structs and enums built from those, and on `Json` itself (kept as is). A `Result` or a function
  type anywhere inside is a compile error that names the field.
- `from_json(Type, j) -> Result[Type, str]` takes the type as its first argument, written like any type
  (`from_json(list[User], j)`, `from_json(dict[str, int], j)`, `from_json(shapes.Point, j)`).
- `json_schema(Type) -> Json` is the JSON Schema (draft 2020-12, as used by OpenAPI 3.1) of a type.
  `json_schema_defs(Type) -> Json` is an object with the schema of every struct and enum that the type is, or
  contains, keyed by name. A struct or enum nested in another schema is a `{"$ref": "#/components/schemas/Name"}`,
  so `{"components": {"schemas": json_schema_defs(T)}}` is what an OpenAPI document needs.

### How values are encoded

| Zylo | JSON |
|---|---|
| `int`, `float` | number (an `int` is written as a float: integers beyond 2^53 lose precision) |
| `bool`, `str` | boolean, string |
| `list[T]`, `set[T]` | array (a set is written in sorted order) |
| `tuple[A, B]` | array of exactly that many items |
| `dict[str, T]` | object; `dict[int, T]` uses the decimal text of the key (`{"1": ...}`) |
| `T?` | `null` for `none`, otherwise the value |
| struct | object with one member per field, by field name; a `none` field is written as `null` |
| enum with no data in any variant | the variant name as a string: `"Admin"` |
| enum with data | object tagged by `type`: `{"type": "Circle", "value": 2.0}`; several values are `"values": [..]`; a variant without data is `{"type": "Empty"}` |

The tagged form is what OpenAPI generators and most JSON libraries call a discriminated union, it is easy to
describe with `oneOf`, and it reads back unambiguously.

### Reading JSON back

- Errors say where: `$.items[2].price: expected float, got string`. `$` is the root, `.name` a field,
  `[2]` an array position and `["key"]` a dict key.
- A missing field is an error (`$.name: missing required field`) unless the field has a default (the default is used)
  or is an `Option` (`none`). `null` is accepted for an `Option` field only.
- Unknown fields in an object are **ignored**: clients may send more than you read.
- An `int` is read from a whole number that fits 64 bits (`3` and `3.0` are fine, `3.5` and `1e30` are errors).
- A set with repeated items is read as the set of them.

### Documenting types

A string literal as the first line of a struct or enum is its description, and a string literal right after a field
(or an enum variant) describes that field. They become `description` in the schema and nothing else; the formatter
keeps them.

## Modules and packages

A program can be split over several files, and use libraries written by others.

```
# shapes.zy                      # main.zy
struct Rect:                     import shapes
    w: int                       from shapes import Rect as R
    h: int                       print(shapes.area(R(3, 4)))

fn area(r: Rect) -> int:
    return r.w * r.h
```

- `import shapes` gives you `shapes.area(...)`, `shapes.Rect(...)` and the type `shapes.Rect`.
  `from shapes import area, Rect` brings the names in directly; `as` renames either form.
- `pub from shapes import Rect, area` inside a module re-exports those names, so a package's entry file can offer
  one import point (`from mylib import Rect, area`); see [packages.md](packages.md#re-exports).
- A library module may only contain imports, functions and structs. Names starting with `_` are private.
- `import a.b` loads `a/b.zy`, relative to the file that imports it; if there is no such file, it
  looks for an installed package named `a`.
- Import cycles are an error.

Install packages from git into a per-project environment:

```
zylo init myapp                          # zylo.toml + src/main.zy
zylo venv                                # create .zylo-env/
zylo add github.com/ana/textkit@v1.0.0   # install it, update zylo.toml and zylo.lock
zylo run src/main.zy                     # finds .zylo-env on its own
```

Everything about modules, environments, the lock file and the security model is in
[packages.md](packages.md).

## Standard library

Zylo ships a library written in Zylo, imported with the reserved prefix `std`: `std.json` (parse and write JSON),
`std.math`, `std.text` and `std.os`. Nothing has to be installed, and no local file or package can shadow `std`.

```zylo
import std.json as json
from std.math import gcd

doc = json.parse('{"id": 7, "tags": ["a", "b"]}').unwrap()
print(json.as_int(json.get(doc, "id").unwrap()))     # Some(7)
print(json.stringify(doc), gcd(12, 18))              # {"id":7,"tags":["a","b"]} 6
```

Every module, with its functions and design decisions, is described in [stdlib.md](stdlib.md).

## Calling Rust

`extern fn` declares a Zylo function whose body is Rust: a path, or a template where `{0}`, `{1}`... are the
arguments. Crates come from `[rust-dependencies]` in `zylo.toml`.

```zylo
extern fn hypot(a: float, b: float) -> float = "f64::hypot"
extern fn shout(s: str) -> str = "{0}.to_uppercase()"
extern fn parse_hex(s: str) -> Result[int, str] = "i64::from_str_radix(&{0}, 16).map_err(|e| e.to_string())"

print(hypot(3.0, 4.0), shout("hi"), parse_hex("ff"))
```

Packages from the community may not call Rust unless your project trusts them. Details, the type mapping and the
trust rules are in [rust-interop.md](rust-interop.md).

## Tools

- `zylo check file.zy` reports **every** mistake in a program, not only the first. After a syntax error the parser skips
  the bad statement and carries on; the type checker checks each statement on its own and does not repeat an error for
  a variable whose declaration already failed. The output ends with `error: aborting due to N previous errors`.
- `zylo fmt` formats files in the canonical style and `zylo lsp` runs a language server (diagnostics, hover, go to
  definition, rename, signature help, completion, quick fixes). Both, and the Visual Studio Code extension, are described in
  [editors.md](editors.md). A string literal as the first statement of a function is its description, shown on hover and in
  signature help; it has no other effect.

## Built-in functions

| Function | Description |
|---|---|
| `print(a, b, ...)` | print any values separated by spaces (`print()` prints an empty line) |
| `len(x)` | length of a `str` (characters), `list`, `dict` or `set` |
| `append(xs, v)` | same as `xs.append(v)` |
| `int(x)`, `float(x)`, `str(x)` | explicit conversions (`int("abc")` crashes; use `parse_int`) |
| `parse_int(s)`, `parse_float(s)` | `int?` / `float?` — the number in `s`, or `none` |
| `some(x)`, `ok(x)`, `err(e)`, `none` | `Option` / `Result` constructors |
| `sum(xs)` | total of a list of `int` or `float` |
| `min(xs)`, `max(xs)`, `min(a, b)`, `max(a, b)` | smallest / largest (of a non-empty list, or of two values) |
| `abs(x)` | absolute value of an `int` or `float` |
| `sorted(xs)`, `reversed(xs)` | a new sorted / reversed list |
| `enumerate(xs)`, `zip(a, b)` | lists of `(index, item)` and `(a_item, b_item)` tuples |
| `sqrt(x)`, `pow(x, y)` | `float` math |
| `round(x)`, `floor(x)`, `ceil(x)` | a `float` to an `int` (`round` rounds half away from zero, unlike Python) |
| `argv()` | the program's arguments, a `list[str]` |
| `input(prompt)`, `read_line()`, `read_stdin()` | read standard input (see above) |
| `read_file(p)`, `write_file(p, t)`, `append_file(p, t)`, `list_dir(p)`, `file_exists(p)` | files |
| `env(name)`, `eprint(...)`, `time()`, `sleep(s)` | environment, standard error, clock |
| `exit(code)` | stop the program with an exit code |
| `cell(v)` | a shared mutable `Cell[T]`, see above |
| `to_json(x)`, `from_json(T, j)`, `json_schema(T)`, `json_schema_defs(T)` | typed JSON, see above |
| `range(n)`, `range(a, b)` | only in a `for` |

A program may define its own function with the name of any built-in above except the core ones
(`print`, `len`, `int`, `float`, `str`, `append`, `range`, `exit`, `some`, `ok`, `err`) and the typed JSON built-ins.

## Runtime errors

These stop the program with exit code **101**: integer overflow (compile with
`--no-overflow-checks` to wrap instead), division by zero, list index out of range, `unwrap()` on
`none` or `err`, and `int(...)` / `float(...)` of text that is not a number.

## Known limitations

No list comprehensions, no recursive local functions, and no multi-statement anonymous functions (use a local `fn`). See the [roadmap](../ROADMAP.md).
