# The standard library

Zylo ships a small library written in Zylo itself. It is part of the compiler: nothing to install, and it works the
same from a git checkout, an installed wheel and the standalone binary. Every module is imported with the reserved
prefix `std`:

```zylo
import std.math as math
from std.text import pad_left

print(math.hypot(3.0, 4.0), pad_left("7", 3, "0"))
```

`import std.math` binds `math` (the last segment, as for any module) and `from std.text import pad_left` brings a
name in directly. See [packages.md](packages.md) for the import rules in general.

## How it works

- The modules are `.zy` files in `zylo/stdlib/`. The linker resolves `std.*` **first**: a local file or an installed
  package can never shadow `std`, and `import std.nope` is an error that says the standard library has no such module.
- A standard library module is **trusted**: it may use `extern fn` (the small Rust helpers it needs) without the
  project listing it under `[rust] trusted-packages`. Your own code that imports it gets plain Zylo functions.
- Modules are linked into your program like any other, so only what you use is compiled and `rustc` is still called
  directly (no Cargo, no crates).
- The files are shipped as package data of the wheel and bundled into the standalone binary (`--collect-data zylo`).
  CI checks both: the wheel lists them and the binary runs a program that imports `std.json` from an empty directory.
- A library module cannot hold constants yet, so constants are functions: `math.pi()`.

| Module | What it is for |
|---|---|
| [`std.json`](#stdjson) | Parse and write JSON |
| [`std.math`](#stdmath) | Trigonometry, logarithms, `gcd`, `clamp`... |
| [`std.text`](#stdtext) | Padding, stripping, character classes, `split_once`... |
| [`std.os`](#stdos) | The working directory, the platform, directories, paths |

## std.json

JSON as a value you can inspect and build, following [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259).

```zylo
enum Json:
    Null
    Bool(bool)
    Num(float)
    Str(str)
    Arr(list[Json])
    Obj(dict[str, Json])
```

| Function | Meaning |
|---|---|
| `parse(text: str) -> Result[Json, str]` | Read a document. The error says what was wrong and where. |
| `stringify(j: Json) -> str` | Compact JSON, no spaces. |
| `pretty(j: Json) -> str` | Two-space indentation, one element or member per line. |
| `get(j, key) -> Json?` | A member of an object (`none` for a missing key or a non-object). |
| `index(j, i) -> Json?` | An element of an array; a negative `i` counts from the end. |
| `as_str`, `as_float`, `as_int`, `as_bool` | The payload as a `str?`, `float?`, `int?` or `bool?`. `as_int` needs a whole number that fits an `int`. |
| `as_list`, `as_dict`, `is_null` | The elements, the members, or a null test. |

```zylo
import std.json as json

text = '{"name": "Ada", "langs": ["rust", "zylo"], "age": 36}'
doc = json.parse(text)
if doc is err:
    print("invalid:", doc)
    exit(1)
name = json.get(doc, "name")
if name is some:
    print(json.as_str(name).unwrap_or("?"))
langs = json.as_list(json.get(doc, "langs").unwrap()).unwrap()
print(len(langs), json.stringify(langs[1]))
print(json.as_int(json.get(doc, "age").unwrap()))

# build a value and write it
members: dict[str, json.Json] = {}
members["ok"] = json.Json.Bool(true)
members["items"] = json.Json.Arr([json.Json.Num(1.0), json.Json.Str("two"), json.Json.Null])
print(json.stringify(json.Json.Obj(members)))     # {"items":[1,"two",null],"ok":true}
print(json.parse("[1, 2,]"))                      # Err("expected a JSON value but found ']' at line 1, column 7")
```

Decisions and limits:

- **Numbers are `float`.** `1`, `1.0` and `1e0` all read as `Num(1.0)`. A whole number is written without a
  fractional part (`1`), so `stringify(parse("1.0"))` is `1`. Integers beyond 2^53 lose precision, as in JavaScript.
  A number that overflows a float (`1e999`) is an error; `NaN` and the infinities (which JSON cannot express) are
  written as `null`.
- **Object keys are sorted.** A `dict` is ordered by key, so `stringify` writes members sorted, and a repeated key keeps its
  last value. Array order is preserved.
- **Strings** accept every escape (`\" \\ \/ \b \f \n \r \t`, `\uXXXX` and surrogate pairs such as `😀`).
  A lone surrogate is rejected, because a Zylo `str` is valid Unicode. Raw control characters inside a string are
  rejected, as the RFC requires. `stringify` escapes `"`, `\`, control characters and U+007F; other characters are written as is.
- **Strict grammar:** no trailing commas, comments, single quotes, leading zeros (`01`), `+1`, `.5`, `1.` or `NaN`;
  nothing may follow the value except whitespace.
- **Nesting** is limited to 512 levels, an error instead of a stack overflow on hostile input.
- **Errors** carry a position, `line L, column C`, counted in characters, e.g. `expected ',' or '}' but found end of input at line 1, column 7`.
- **Speed.** The text is turned into code points once, so parsing is linear: about 0.1 s to parse, write and parse again
  an 0.8 MB document, and 0.04 s for a 1 MB string. (`s += x` appends in place, which keeps building long
  text linear.)
- Mapping your own structs to and from `Json` is not here yet; use the accessors.

## std.math

Angles are in radians. Constants are functions: `pi()`, `tau()`, `e()`.

| | |
|---|---|
| `sin cos tan asin acos atan atan2(y, x) sinh cosh tanh` | trigonometry |
| `exp ln log2 log10 log(x, base) cbrt hypot(a, b)` | exponentials and roots |
| `trunc(x)` | integer part, as a `float` |
| `is_nan(x)`, `is_finite(x)` | tests |
| `radians(d)`, `degrees(r)` | conversions |
| `clamp(x, lo, hi)`, `clamp_int(x, lo, hi)`, `sign(n)` | limiting and sign |
| `gcd(a, b)`, `lcm(a, b)` | integer divisors and multiples |

`sqrt`, `pow`, `abs`, `floor`, `ceil`, `round`, `min` and `max` are built in and not repeated.

## std.text

Only what the `str` methods lack. Lengths count characters.

| | |
|---|---|
| `pad_left(s, width, fill)`, `pad_right`, `center` | pad to `width` with the first character of `fill` (a space if empty) |
| `lstrip(s)`, `rstrip(s)` | strip one side (`s.strip()` strips both) |
| `capitalize(s)`, `reverse(s)` | `"hELLO"` becomes `"Hello"`; reverse by character |
| `remove_prefix(s, p)`, `remove_suffix(s, p)` | drop it when present |
| `is_digit`, `is_alpha`, `is_alnum`, `is_space` | non-empty and every character matches |
| `split_once(s, sep) -> tuple[str, str]?` | split at the first `sep` |

## std.os

What the built-in `argv`, `env`, file and directory functions do not cover.

| | |
|---|---|
| `env_or(name, default)` | an environment variable with a default |
| `cwd() -> Result[str, str]` | the working directory |
| `platform() -> str` | `"linux"`, `"macos"`, `"windows"`... |
| `is_dir(path)`, `make_dirs(path)`, `remove_file(path)` | directories and files; the last two return a `Result[int, str]` like `write_file` |
| `join_path(a, b)` | join with the system's separator |

## Writing a standard library module

Add `zylo/stdlib/<name>.zy` (library rules: only imports, functions, structs and enums; names starting with `_`
are private), document it here, and add tests: a program in `tests/programs/` with an expected `.out`, and for anything
with a specification a conformance test like `tests/test_json.py` against an independent implementation. Prefer plain
Zylo; use an `extern fn` only for something the language cannot do or cannot do fast, and keep it private (`_name`).
