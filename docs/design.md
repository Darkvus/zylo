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

**A modified parameter nobody sees is an error, not a silent loss.** The guide promises that arguments are passed by
value, so a function that modifies a list, dict, set or struct parameter modifies its own copy. Making that copy
by-reference would break the promise (and `Cell` exists for shared state), so the decision (v0.26) is to keep value
semantics and make the one useless case loud: `fn add(a: A, k: str): a.names[k] = "v"` changed a copy that was then
thrown away, which is never what the author meant. The checker records, per variable, the syntax nodes that mention it
(`VarInfo.uses`) and those that only name it as the root of a modified place (`mut_roots`, filled by `mark_mutable`:
`a.f = v`, `a[i] = v`, `append(a, v)`, a method that modifies `self`). After checking a function or an anonymous function,
a non-`Copy` parameter that was modified in place and has no other mention is rejected with "parameter 'a' is modified but
the changes are not visible to the caller". A parameter that is also read, returned or passed on is a deliberate local
copy and stays legal (`fn sorted_copy(xs) -> list: xs.sort(); return xs`). Plain reassignment (`xs = [1]`) is not
flagged: it rebinds a local name, which nobody expects to reach the caller. Generic functions are checked through their
copies, and closures cannot modify captured variables at all (an older error), so the rule covers every callable.
Not covered: the variable of a `for` loop (`for q in ps: q.x = 1` changes the iteration's copy, which is documented).

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
plain values just like the enum case.

**Patterns are a tree, and coverage is decided by one algorithm.** Since v0.25 a pattern is a `Pattern` node whose
children are patterns: variant data, the content of `some` / `ok` / `err`, tuple elements, or-alternatives, with names,
`_`, literals and integer ranges at the leaves. Exhaustiveness and unreachable arms come from Maranget's usefulness
algorithm (`semantics/exhaustive.py`) instead of a set of seen variant names; guarded arms are left out of the matrix.
Integers are handled as intervals cut at the ends of the ranges written, so `..0`, `0..=9`, `10..` is complete and the
error for a gap names it. Rust cannot match a string literal inside an owned `String`, so a string nested in a pattern
is generated as a fresh binding plus a comparison in the arm's guard (an or-pattern with such a string becomes one Rust
arm per alternative); Rust only sees those arms as guarded, which is sound because a string column can never be
completed by a literal. Ranges are always emitted as inclusive Rust ranges. `is` narrowing is untouched: `match` is for choosing between cases, `is` for
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
translation (in the interface layer) of use cases in the application layer. Navigation resolves names on its own tolerant parse of each file (`semantics/navigation.py`), because the linker renames and merges definitions, and borrows from the checker only the types it alone knows, found by source position, and the VS Code grammar is generated from
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

**Typed JSON is expanded by the checker into ordinary Zylo.** Rather than generic functions, `to_json(x)`, `from_json(T, j)`,
`json_schema(T)` and `json_schema_defs(T)` are built-ins that the checker resolves per type (the parser reads the first
argument of the last three as a type, so `list[int]`, `T?` and `mod.Struct` work and the linker mangles it like any type
name). For each type it needs, the checker generates the source of a helper function (`_zj_to_*`, `_zj_from_*`,
`_zj_def_*`), parses it, appends it to the program and rewrites the call into a plain call of that helper; the helper
is then type-checked and compiled like user code. This reuses the type system, borrow inference and code generator, works
across modules (the generator sees mangled names), terminates on recursive types (a helper's name is registered before
its body is generated), and costs compile time only for the types actually converted. The runtime part (reading
numbers with range checks, error paths, schema pieces) lives in `std.json` as private functions, so the generated code stays
small. The rewrite is idempotent: the checker runs several times over the same tree, and after the first run the call
is an ordinary call of an existing function.

**Encodings chosen for OpenAPI.** Structs are objects by field name. Enums whose variants carry no data are strings;
the others are objects tagged with `type` (`{"type": "Circle", "value": 2.0}`), because that is a discriminated union
in JSON Schema (`oneOf` with a `const` per variant) and needs no knowledge of Zylo to read. `Result` is rejected: it is
control flow, not data, and no encoding would be a natural wire format. Unknown fields are ignored when reading (the
robustness principle; the schema leaves `additionalProperties` open to match); missing fields use the default or
`none`, otherwise they are errors. `int` travels as a JSON number (a float), which is the only number `std.json` has.
Named types become `$ref`s into `#/components/schemas`, because a recursive type cannot be inlined; the nested
definitions are collected at compile time by `json_schema_defs`, so no runtime graph walk is needed.

**Doc strings.** A string literal as the first line of a struct or enum body, or right after a field or variant,
is its description. It is a plain expression statement position that was otherwise a syntax error, so no existing
program changes meaning, the lexer needs no new token (comments stay invisible), and the formatter, which prints the
tree, keeps them for free.

## Generics

Syntax: `fn first[T](xs: list[T]) -> T`, `struct Box[T]`, `enum Opt[T]`, bounds as `[T: Ord + Show]`, explicit arguments as
`first[int](xs)` and `Box[int]`. The square brackets are free: they were only used after the built-in container names.

**Monomorphization in the checker, not Rust generics.** A generic definition is a template that is never emitted. Each
list of type arguments a program uses makes a *copy* of the template (`first[int]`, `Box[str]`) in which every written `T` is
replaced by that type (the copy's `TypeRef`s carry the resolved type in `fixed`). The copy is registered like any other
function, struct or enum and appended to the program, so borrow inference, `derive`, the module system and the code
generator see ordinary concrete code and needed no new cases beyond skipping templates and giving Rust-safe names to
instances (`Box_5b_int_5d_`). Rust generics were rejected: they need trait bounds on every operation (`PartialEq`,
`PartialOrd`, `Clone`, `Display`...), the inferred borrow decisions (a parameter is `&T` or `T` depending on how *this* function
uses it) would have to be made once for all `T` instead of per type, and the generated `Debug`/`PartialEq` impls depend on the
fields. The price is code size (one copy per instance) and compile time; speed is that of hand-specialised code. Instances
are named by their arguments, so `Box[int]` written twice is one type (`StructT`/`EnumT` compare by name and also carry
`args` and `base` so inference can take them apart).

**The template is checked once, for all types.** Besides the copies, the checker checks the template with its parameters
as opaque `ParamT` types, so a mistake in a generic is reported once at its own line and also when it is never used. A
`ParamT` can be stored, passed and returned; every other operation asks for a capability the parameter must declare. The
fixed set of bounds is `Eq` (`==`, `in`), `Show` (printing, f-strings), `Ord` (`<`, `min`, `sorted`), `Num` (arithmetic) and
`Key` (dict keys, set elements); `Num` and `Key` imply `Ord`, `Eq` and `Show`. A bound is satisfied by an argument type with
the same rules the operation uses on concrete types (`Ord` is exactly what `<` accepts: `int`, `float`, `str`). User-defined
bounds are traits (see [Traits](#traits)). When the template has a mistake, its copies are not checked again (the same error
would be repeated per type).

**Inference is unification at the call.** The declared parameter types (in terms of `ParamT`) are unified with the argument
types: the first occurrence binds `T`, later ones must agree. What no argument decides comes from the expected type
(`e: Opt[str] = Opt.Empty`, `return Opt.Empty` in a function returning `Opt[T]`), which is only a hint: arguments win and the
consumer reports a disagreement. Arguments that cannot be typed alone (`none`, `[]`, `{}`, anonymous functions) wait until the
others are checked, so `or_default(none, 3)` and `map_all(xs, fn(x) => ...)` work; an anonymous function's result type binds
the remaining parameters (`U`). Each argument is checked exactly once. There is no overloading and no subtyping, so one
solution is the only solution. A parameter nobody determines is an error naming it, as is the wrong number of arguments.

**Rerunning the checker.** The checker is rerun over the same tree until the set of mutating methods settles; copies are
tagged (`instance=True`) and dropped at the start of every run, and calls remember their copy in an annotation (`Call.inst`,
`MethodCall.variant`), never by rewriting the name, so a rerun starts from the templates and gets the same names again. The
template's own nodes keep the types of the all-types check, which is what lets the language server hover inside a generic
and rename fields of a generic struct; its copies are not part of any module's tree, so the editor never sees them.

**Cost limits.** A generic that instantiates itself with ever bigger types (`f[T]` calling `f[list[T]]`) would never finish,
so the type arguments' nesting is limited to 12 levels and a program to 500 instances; the error names the instance. Copies
that fail are reported at the template's line (copies keep the source positions of the template).

**Parsing `name[...]`.** `first[int](xs)` looks like indexing followed by a call. The parser reads the brackets as type
arguments when they parse as types, are followed by `(` or `.`, and either the name is declared generic in the same file or
every type in them cannot be a variable (a built-in type name or a capitalised name). So `table[key](x)` still calls an
element, and `first[point](xs)` with a lowercase struct from another module needs inference instead. Parsing with scope
knowledge would be exact but the parser knows nothing about imports.

**Interactions.** Typed JSON works on instances (`to_json(Box(value=1))`, `from_json(Pair[int, Point], j)`) because an
instance is an ordinary struct to `derive`; schema components are named `Pair_int_Point`. Re-exports keep one identity: the
template's linked name is the base of every instance. `match` on a generic enum takes its type arguments from the value
(`Opt.Some(x)` or `Opt[int].Some(x)`).

**Generic methods are templates inside the struct.** `fn map[U](self, f: fn(T) -> U) -> Box[U]` is kept in the struct's
`gmethods` and never emitted. A call infers its parameters like a generic function's, with one difference: the signature
used is the one of the *template* (in terms of the struct's own `T` and the method's `U`) with the struct's parameters
bound up front to the receiver's type arguments. Substituting the receiver first would leave an outer parameter that
happens to be called `U` (`fn f[U](b: Box[U])` calling `b.map(...)`) indistinguishable from the method's own `U`. The call
then makes a copy of the method (named `map[str]`) and appends it to the struct instance's `methods`; copies are checked
from a queue that the main loop drains, and dropped at the start of every rerun like the other copies (`MethodCall.inst`
remembers the copy). The template is checked once for all types, so a mistake in it is reported once and its copies are not
checked when it fails. Rust sees ordinary methods whose names are made Rust-safe (`map_5b_str_5d_`).

**Generic functions as values reuse the call machinery.** A `Name` that refers to a generic function and is not a call gets
its type arguments from the written ones (`first[int]`) or by unifying the function's signature with the expected function
type (or just its parameter types when an argument of a generic call or a built-in method is checked, since the result may be
what that call is about to learn). The instance is recorded in `Name.inst` and the code generator wraps it like any
top-level function value. A parameter that nothing decides is an error naming it.

**A literal takes its type from the other operand.** In a template an integer literal checked against an expected `T` with
the `Num` bound has type `T`; `1 + a` looks at `a` first. In the copy for `float` the same literal sees `float` expected, and
that is only accepted while checking a copy (a template with a plain `float` that met an `int` literal would already have
been rejected, so copies of working templates never hide a mistake). The code generator writes it as `1.0`. Float literals
cannot be a `T` (they would be wrong for `int`), and `Ord` or no bound does not give the literal a type: the error says so.

**`first[point](xs)`.** The parser still only decides when it can (the name is generic in the same file, or every type
argument is built in or capitalised); otherwise `first[point]` stays an index and the *linker*, which knows what each name is,
turns an index of a function (`first[...]`, `util.first[...]`, with a comma list for several types) into type arguments. A
function cannot be indexed, so this never takes anything away; if the brackets hold something that is not a type, the error
says so. A local variable named like the function still wins (`first[0](5)` calls an element).

**Hover.** The checker leaves on each template the types its parameters were instantiated with (`tinst`, plain bookkeeping
like `origin`). The editor, which only sees the original tree, shows for a name whose type mentions a type parameter the
parameter with its bounds and those types, or says it is not instantiated.

**Default type arguments.** `struct Counter[T = int]`, `fn zero[T: Num = int]() -> T`: a parameter may name the type it takes
when nothing decides it, and the parameters with defaults come last. The default is a plain type (it cannot mention another
parameter) and must satisfy the parameter's bounds, which is checked once with the definition. It applies where a type is
written with fewer arguments (`c: Counter` is `Counter[int]`) and, at a call, after the arguments and the expected type have
had their say (`Counter()` with an empty list field is `Counter[int]`, but `Counter(items=["a"])` is `Counter[str]`). Methods'
own parameters take no defaults: a method call has the receiver to decide them, and the rule would only add a way to be
surprised.

Not done: float literals of type `T`, and inferring a generic function value whose own type parameters appear only in a
result that the context does not name.

## Traits

Syntax:

```
trait Shape:
    fn area(self) -> float
    fn scale(self, by: float) -> Self

struct Circle(Shape):            # enum Light(Shape): works the same
    r: float
    fn area(self) -> float: ...
    fn scale(self, by: float) -> Circle: ...

fn total[T: Shape + Show](xs: list[T]) -> float
```

**Where a type says it implements a trait.** Three shapes were weighed.
`impl Shape for Circle:` (Rust) puts the methods in a second block, so a type's methods live in two kinds of places, the
checker has to merge them into the struct and a second pass has to forbid two `impl`s of the same pair, across modules and
re-exports too. Structural conformance (Go) needs no declaration, but a type satisfies a trait by accident of naming, a
missing method is reported far from where the author meant to implement the trait, and there is nothing to attach the
"implements" relation to for the editor. So a trait is listed after the name, like the base classes of a Python class:
`struct Circle(Shape):`. The methods are the ones already written inside the struct, the declaration is one token away from
the name (hover, go to definition and rename see it), the trait is looked up by the linker like any imported name (so
`from shapes import Shape`, `pub from` re-exports and `shapes.Shape` work), and a type can list a trait once only. The price
is that a trait cannot be implemented for a type the author does not own: not for `int` or `list[T]`, not for a struct of a
package. That is the first thing to revisit (an `impl` block for foreign types), and it would sit next to this form.

**A trait is a list of signatures.** `fn name(self, ...) -> type` without a body; no default methods, no generic methods, no
default values, no type parameters on the trait, no associated types, no supertraits. `Self` stands for the implementing type
and may be used only in a trait's signatures. An implementation has to define every method with the same parameter names
(callers may use named arguments through the bound), the same parameter types and the same result, `Self` replaced by the type
(`fn scale(self, by: float) -> Circle`). Whether a method modifies `self` is not part of the signature. Traits are checked where the
struct is declared: a missing method, a wrong signature, a trait that is unknown or is really a struct, and a trait listed
twice are errors at the declaration, so a type that fails is reported once and not at every generic that uses it.

**A bound is a name in the bound list.** `T: Shape + Show` mixes traits and built-in bounds (a trait cannot be called
`Eq`, `Show`, `Ord`, `Num` or `Key`). The linker rewrites a trait's name in a bound exactly as it does for a type, so the
checker sees linked names. A `ParamT` keeps the traits among its `bounds`, and `satisfies(type, trait)` is "the struct or enum
lists it": a type parameter satisfies the traits it is bounded by (so a generic can call another generic with the same
bound), and `int`, lists and the like never do.

**Calls on `T`.** Inside the template a method call on a `T` looks the name up in the traits that bound `T` and checks the
call against the trait's signature with `Self` replaced by `T`; a name no bound declares is an error that names the trait to add
when one declares it. If two bounds declare a method of the same name with different signatures the call is ambiguous and says so.
In a copy `T` is a real type and the call is an ordinary method call on it, so *nothing about traits reaches the code generator
or Rust*: no vtable, no dynamic dispatch, no `dyn`, the same speed as a hand-written copy. This also means trait objects
(a `list[Shape]` of mixed types) do not exist, and are out of scope: a trait is only usable as a bound.

**Traits and the rest.** A generic struct may implement a trait (`struct Team[T](Named)`; each instance is checked against it,
the template is checked once with `T` opaque). Typed JSON does not see traits (a derived function reads fields). The editor
treats a trait's method and the methods that implement it as one family: rename changes them together, hover and go to
definition on a call through a bound lead to the trait's declaration, and completion after a dot on a `T` offers the bounds'
methods. Renaming a parameter of such a method is refused (the names are part of the signature) rather than done in one place.
`trait` is a keyword, so a variable or function called `trait` has to be renamed.

Not done: trait objects, default methods, supertraits (`trait A(B)`), generic traits, `impl` for types the author does not own
(including built-ins), associated types and constants.

## Known limitations

- Values are still cloned when stored (assigned to another variable, put in a struct or a list) or returned.
- `is` narrowing works on plain variables and field paths (`p.name`, `self.a.b`), with a single `is` condition per `if` (no `and` chains) and not on call results or indexed elements. A narrowed value is already unwrapped: calling `unwrap`, `unwrap_or` or `unwrap_err` on it is the error "already narrowed" (an error rather than a no-op, so a stray `.unwrap()` is not mistaken for a second check, and the message says what to drop). Closures do not see a field narrowing.
- `match` has guards, or-patterns, nested patterns, integer ranges and tuple scrutinees (exhaustiveness is exact), but no struct patterns (`Point(x=0, y)`), no `x @ pattern` bindings and no bare-name catch-all at the top of an arm (use `_`).
- Generics are monomorphized. The bounds are a fixed set (`Eq`, `Ord`, `Num`...) and the traits a program declares: no trait objects, default methods, supertraits or generic traits, no implementing a trait for a type you do not own (`int`, a package's struct), no float literals of type `T`, no generic `extern fn`.
- Arguments are passed by value; a modified parameter whose changes nobody could see is an error (see above). The variable of a `for` loop is a copy as well, and modifying it without using it afterwards is not flagged.
- Rust generics and traits are not exposed (Zylo's traits are checked in the compiler and leave no trace in the generated Rust). Rust can call Zylo function values passed to an `extern fn` (see rust-interop.md).
- No list comprehensions yet; closures are limited (see above).
- The language server's types and member lookups need the file to parse and link (a syntax error elsewhere removes them), and its rename refuses rather than guess. I/O is synchronous and text-only (no binary files, sockets or subprocesses).
- No central registry, version ranges or dependency resolver: dependencies are pinned to exact refs.
- Diagnostics are in English only.
