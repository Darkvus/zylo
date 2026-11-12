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
  directly (no Cargo, no crates), with one exception: a module that needs Rust crates (today `std.http`) brings
  them with it, see [Modules that need crates](#modules-that-need-crates).
- The files are shipped as package data of the wheel and bundled into the standalone binary (`--collect-data zylo`).
  CI checks both: the wheel lists them and the binary runs a program that imports `std.json` from an empty directory.
- A library module cannot hold constants yet, so constants are functions: `math.pi()`.

| Module | What it is for |
|---|---|
| [`std.json`](#stdjson) | Parse and write JSON |
| [`std.math`](#stdmath) | Trigonometry, logarithms, `gcd`, `clamp`... |
| [`std.text`](#stdtext) | Padding, stripping, character classes, `split_once`... |
| [`std.os`](#stdos) | The working directory, the platform, directories, paths |
| [`std.http`](#stdhttp) | A blocking HTTP/1.1 server and a simple client (needs Cargo) |

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
- **Your own types.** The built-ins `to_json`, `from_json`, `json_schema` and `json_schema_defs` convert structs, enums
  and collections to and from `Json` and describe them with JSON Schema; they are explained in the
  [guide](guide.md#typed-json). They need no import of their own: using one loads this module. Their generated code
  calls a few private functions of this module (names starting with `_`) for error messages and schema pieces.

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

## std.http

A blocking HTTP/1.1 server and a simple client. This is the first layer of a web framework: routing, JSON mapping and
OpenAPI come later, in libraries written in Zylo on top of it. The module is Zylo plus a little Rust over two small
crates (`tiny_http` for the server and `ureq` for the client), so a program that imports it is built with **Cargo**
(the first build downloads and compiles the crates, about 10 s; later builds reuse them).

```zylo
struct Request:
    method: str                    # "GET", "POST"... (upper case)
    path: str                      # percent-decoded, without the query string
    query: dict[str, str]          # ?a=1&b=x%20y, decoded; a repeated key keeps the last value
    headers: dict[str, str]        # names in lower case; a repeated header keeps the last value
    body: str
    path_params: dict[str, str]    # empty now: a router fills it in

struct Response:
    status: int
    headers: dict[str, str]
    body: str
```

| Function | Meaning |
|---|---|
| `text(status, body) -> Response` | `text/plain; charset=utf-8` |
| `json_response(status, body) -> Response` | `application/json`; `body` is JSON text you already built |
| `redirect(location) -> Response` | a 302 with a `location` header |
| `not_found() -> Response` | a 404 |
| `header(headers, name) -> str?` | a header looked up by name, whatever its case |
| `serve(host, port, handler) -> Result[int, str]` | answer requests forever |
| `serve_n(host, port, handler, max_requests)` | answer that many requests, then return the count (tests, scripts) |
| `serve_with(host, port, handler, max_requests, max_body)` | both limits; `max_requests` 0 means no limit |
| `get(url)`, `delete(url)`, `post(url, body, headers)`, `put(url, body, headers)` | `Result[Response, str]` |
| `send(method, url, body, headers)` | any method |

```zylo
import std.http as http

fn handle(req: http.Request) -> http.Response:
    if req.method == "GET" and req.path == "/":
        return http.text(200, "Hello, " + req.query.get("name").unwrap_or("world") + "!")
    if req.path == "/echo":
        return http.json_response(200, req.body)
    return http.not_found()

match http.serve("127.0.0.1", 8080, handle):
    ok(_):
        print("stopped")
    err(e):
        eprint(e)       # e.g. cannot listen on 127.0.0.1:8080: Address already in use
```

```zylo
import std.http as http

headers: dict[str, str] = {}
headers["content-type"] = "application/json"
match http.post("http://127.0.0.1:8080/echo", '{"a": 1}', headers):
    ok(r):
        print(r.status, r.body)
    err(e):
        print("no response:", e)
```

Decisions and limits:

- **One request at a time.** A handler is an ordinary Zylo function value (an `Rc` closure in the generated Rust),
  which cannot be shared between threads, so the accept loop is single-threaded: the next request waits until the
  handler returns. That is right for tests, tools and development; concurrency is a later language feature.
- **A panic in a handler is a 500.** A runtime error in the handler (an index out of range, `unwrap` of `none`) is
  caught, logged to stderr (`handler panicked: ...`) and answered with `500 Internal Server Error`; the server goes on.
- **Large bodies.** A body over `max_body` bytes (default 1 MiB, `default_max_body()`) is answered with `413` without
  calling the handler. The body is read as text (invalid UTF-8 becomes U+FFFD); `Content-Length` is always set from
  the body you return, whatever header you set.
- **HEAD and OPTIONS.** `HEAD` reaches the handler as `HEAD` (the body is dropped on the wire, so a `GET` handler
  answers it). An `OPTIONS` request the handler answers with 404 becomes `204` with an `Allow` header; any other
  answer of the handler is kept (so you can implement CORS yourself).
- **Malformed requests** are dropped by the server (it answers 400 or closes the connection) and the handler is not
  called; they do not count towards `max_requests`. Statuses are clamped to 100-599.
- **Headers** are one value per name (a `dict`): `Set-Cookie` cannot be sent twice yet.
- **Client.** Any HTTP status, including 404 and 500, is an `ok` response; `err` means no response arrived (refused,
  bad URL, a 30 s timeout). Redirects are **not** followed (`redirect` and `location` are yours to handle), and only
  `http://` URLs work: the crate is built without TLS so that the build stays fast and needs no C toolchain.
  `https://` is a later feature.
- **Port 0** lets the system pick, but the server does not report which; pick a free port yourself in tests.
- The server prints `listening on http://HOST:PORT` to stderr when it starts.

## Modules that need crates

A standard library module `std.x` may ship two sidecar files next to `x.zy`: `x.rs`, Rust support code that its
private `extern fn` templates call (`zylo_http::serve(...)`), and `x.toml`, a `[rust-dependencies]` table in the
format of [rust-interop.md](rust-interop.md). When a program links the module, the compiler appends `x.rs` to the
generated Rust and adds the crates to the build. Programs that do not import it are unchanged and keep calling `rustc`
directly. Both files are trusted like the module. A crate your own `zylo.toml` asks for with a different requirement
than the module's is an error (`conflicting requirements for crate`); asking for the same one is fine.

Without a `zylo.toml`, Cargo builds in the user cache (`$ZYLO_CACHE_DIR`, `$XDG_CACHE_HOME/zylo`, `%LOCALAPPDATA%\zylo`
or `~/.cache/zylo`), one directory per set of crates, so every program that imports `std.http` shares the compiled
crates. With a `zylo.toml` the build directory is `.zylo-build/` next to it, as for any crate user.
`zylo build --offline` works once the crates have been downloaded.

## Writing a standard library module

Add `zylo/stdlib/<name>.zy` (library rules: only imports, functions, structs and enums; names starting with `_`
are private), document it here, and add tests: a program in `tests/programs/` with an expected `.out`, and for anything
with a specification a conformance test like `tests/test_json.py` against an independent implementation. Prefer plain
Zylo; use an `extern fn` only for something the language cannot do or cannot do fast, and keep it private (`_name`).
