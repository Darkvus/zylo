"""Codegen: AST tipado (ya pasado por el checker) -> Rust.

Modelo de memoria: todo se comporta como valor. `int`/`float`/`bool` son `Copy`; `str`
(`String`) y `list` (`Vec<T>`) se clonan al leerlos como valor (`x.clone()`). Las lecturas
that do not need ownership (len, print, indexing, comparisons, `for` over range) take
referencia y no clonan.

Structs: `#[derive(Clone, PartialEq)] struct` + an `impl` with their methods. `self` is `&self` or
`&mut self` depending on what the checker inferred (`FnDef.self_mut`). `Debug` is written by hand to
imprimir `Point(x=1.0, y=2.0)`.
"""
import re

from zylo.domain import ast
from zylo.domain.builtins import BUILTINS as BUILTIN_NAMES
from zylo.domain.errors import ZyloError
from zylo.domain.types import (
    BOOL,
    FLOAT,
    INT,
    STR,
    UNIT,
    CellT,
    DictT,
    EnumT,
    FnT,
    ListT,
    OptionT,
    ResultT,
    SetT,
    StructT,
    TupleT,
    Type,
    is_copy_type,
)

PRELUDE = """#![allow(unused, unused_parens, while_true, unused_mut)]
use std::collections::{BTreeMap, BTreeSet};

struct __ZyCell<T>(std::rc::Rc<std::cell::RefCell<T>>);

impl<T> __ZyCell<T> {
    fn new(v: T) -> Self {
        __ZyCell(std::rc::Rc::new(std::cell::RefCell::new(v)))
    }
    fn set(&self, v: T) {
        *self.0.borrow_mut() = v;
    }
    fn get(&self) -> T where T: Clone {
        T::clone(&self.0.borrow())
    }
    fn update(&self, f: &std::rc::Rc<dyn Fn(T) -> T>) where T: Clone {
        let v = self.get();
        self.set(f(v));
    }
}

impl<T> Clone for __ZyCell<T> {
    fn clone(&self) -> Self {
        __ZyCell(std::rc::Rc::clone(&self.0))
    }
}

impl<T: PartialEq> PartialEq for __ZyCell<T> {
    fn eq(&self, other: &Self) -> bool {
        *self.0.borrow() == *other.0.borrow()
    }
}

impl<T: std::fmt::Debug> std::fmt::Debug for __ZyCell<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "Cell({:?})", self.0.borrow())
    }
}

fn __zy_i(i: i64, n: usize) -> usize {
    if i < 0 {
        let j = i + n as i64;
        if j < 0 { usize::MAX } else { j as usize }
    } else {
        i as usize
    }
}

fn __zy_at<T>(v: &mut Vec<T>, i: i64) -> &mut T {
    let k = __zy_i(i, v.len());
    &mut v[k]
}

fn __zy_input(prompt: &str) -> String {
    use std::io::Write;
    print!("{}", prompt);
    std::io::stdout().flush().ok();
    match __zy_read_line() {
        Some(l) => l,
        None => panic!("input(): end of input"),
    }
}

fn __zy_read_line() -> Option<String> {
    let mut s = String::new();
    match std::io::stdin().read_line(&mut s) {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            while s.ends_with('\\n') || s.ends_with('\\r') { s.pop(); }
            Some(s)
        }
    }
}

fn __zy_read_stdin() -> String {
    use std::io::Read;
    let mut s = String::new();
    std::io::stdin().read_to_string(&mut s).ok();
    s
}

fn __zy_read_file(p: &str) -> Result<String, String> {
    std::fs::read_to_string(p).map_err(|e| format!("{}: {}", p, e))
}

fn __zy_write_file(p: &str, t: &str) -> Result<i64, String> {
    std::fs::write(p, t).map(|_| t.chars().count() as i64).map_err(|e| format!("{}: {}", p, e))
}

fn __zy_append_file(p: &str, t: &str) -> Result<i64, String> {
    use std::io::Write;
    std::fs::OpenOptions::new().create(true).append(true).open(p)
        .and_then(|mut f| f.write_all(t.as_bytes()))
        .map(|_| t.chars().count() as i64)
        .map_err(|e| format!("{}: {}", p, e))
}

fn __zy_list_dir(p: &str) -> Result<Vec<String>, String> {
    let rd = std::fs::read_dir(p).map_err(|e| format!("{}: {}", p, e))?;
    let mut v: Vec<String> = rd.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().to_string()).collect();
    v.sort();
    Ok(v)
}

fn __zy_time() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn __zy_range(len: usize, lo: Option<i64>, hi: Option<i64>) -> (usize, usize) {
    let n = len as i64;
    let norm = |v: i64| -> i64 { (if v < 0 { v + n } else { v }).max(0).min(n) };
    let a = norm(lo.unwrap_or(0));
    let b = norm(hi.unwrap_or(n));
    (a as usize, if b < a { a as usize } else { b as usize })
}

fn __zy_char(s: &str, i: i64) -> String {
    let n = s.chars().count();
    s.chars().nth(__zy_i(i, n)).map(|c| c.to_string()).expect("string index out of range")
}

fn __zy_find(s: &str, sub: &str) -> Option<i64> {
    s.find(sub).map(|b| s[..b].chars().count() as i64)
}

fn __zy_insert<T>(v: &mut Vec<T>, i: i64, x: T) {
    let n = v.len() as i64;
    let k = if i < 0 { (i + n).max(0) } else { i.min(n) };
    v.insert(k as usize, x);
}

fn __zy_remove<T: PartialEq>(v: &mut Vec<T>, x: &T) -> bool {
    match v.iter().position(|y| y == x) {
        Some(p) => { v.remove(p); true }
        None => false,
    }
}
"""

RUST_KEYWORDS = {
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum",
    "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod", "move",
    "mut", "pub", "ref", "return", "self", "Self", "static", "struct", "super", "trait", "true",
    "type", "unsafe", "use", "where", "while", "abstract", "become", "box", "do", "final",
    "macro", "override", "priv", "try", "typeof", "unsized", "virtual", "yield", "main",
}


# Type and method names that would clash with the Rust prelude or with traits we rely on.
RUST_TYPE_NAMES = {
    "String", "Vec", "Option", "Result", "Box", "Some", "None", "Ok", "Err", "Default", "Clone",
    "PartialEq", "Debug", "Display", "Copy", "Send", "Sync", "Drop", "Fn", "Iterator", "Ord", "Eq",
}
METHOD_CLASHES = {
    "clone", "eq", "ne", "fmt", "to_string", "into", "from", "drop", "default", "cmp",
    "partial_cmp", "hash", "borrow", "to_owned", "as_ref", "as_mut",
}


def ident(name: str) -> str:
    """Make a Zylo name a valid Rust identifier.

    Args:
        name: A variable or function name (possibly linker-mangled).

    Returns:
        The name, with `·` turned into `__` and a trailing `_` if it is a Rust keyword.
    """
    if name == "self":
        return name
    name = name.replace("\u00b7", "__")  # linker-mangled `module·name`
    return name + "_" if name in RUST_KEYWORDS else name


def mident(name: str) -> str:
    """Method name: besides keywords, avoid shadowing trait methods (`clone`, `eq`...)."""
    return name + "_" if name in RUST_KEYWORDS or name in METHOD_CLASHES else name


def tname(name: str) -> str:
    """Make a Zylo struct name a valid Rust type name.

    Args:
        name: The struct's name.

    Returns:
        The name, with a trailing `_` if it clashes with a Rust prelude type or keyword.
    """
    name = name.replace("\u00b7", "__")
    return name + "_" if name in RUST_TYPE_NAMES or name in RUST_KEYWORDS else name


def rust_type(t: Type) -> str:
    """Return the Rust spelling of a Zylo type.

    Args:
        t: The type.

    Returns:
        For example `Vec<i64>` for `list[int]`.

    Raises:
        ZyloError: If the type has no Rust equivalent.
    """
    if t == INT:
        return "i64"
    if t == FLOAT:
        return "f64"
    if t == BOOL:
        return "bool"
    if t == STR:
        return "String"
    if t == UNIT:
        return "()"
    if isinstance(t, ListT):
        return f"Vec<{rust_type(t.elem)}>"
    if isinstance(t, (StructT, EnumT)):
        return tname(t.name)
    if isinstance(t, DictT):
        return f"BTreeMap<{rust_type(t.key)}, {rust_type(t.val)}>"
    if isinstance(t, SetT):
        return f"BTreeSet<{rust_type(t.elem)}>"
    if isinstance(t, TupleT):
        return "(" + ", ".join(rust_type(x) for x in t.elems) + ")"
    if isinstance(t, OptionT):
        return f"Option<{rust_type(t.inner)}>"
    if isinstance(t, ResultT):
        return f"Result<{rust_type(t.ok)}, {rust_type(t.err)}>"
    if isinstance(t, CellT):
        return f"__ZyCell<{rust_type(t.inner)}>"
    if isinstance(t, FnT):
        ret = "" if t.ret == UNIT else f" -> {rust_type(t.ret)}"
        return f"std::rc::Rc<dyn Fn({', '.join(rust_type(x) for x in t.params)}){ret}>"
    raise ZyloError(f"type has no Rust equivalent: {t}")


def holds_fn(t: Type, opaque: set[str]) -> bool:
    """Whether a type holds a function (directly, or through a struct or enum in `opaque`).

    Args:
        t: The type.
        opaque: Names of the structs and enums already known to hold a function.

    Returns:
        True if values of the type cannot derive `PartialEq` or `Debug` in Rust.
    """
    if isinstance(t, FnT):
        return True
    if isinstance(t, (StructT, EnumT)):
        return t.name in opaque
    if isinstance(t, (ListT, SetT)):
        return holds_fn(t.elem, opaque)
    if isinstance(t, (OptionT, CellT)):
        return holds_fn(t.inner, opaque)
    if isinstance(t, ResultT):
        return holds_fn(t.ok, opaque) or holds_fn(t.err, opaque)
    if isinstance(t, DictT):
        return holds_fn(t.val, opaque)
    if isinstance(t, TupleT):
        return any(holds_fn(x, opaque) for x in t.elems)
    return False


def fn_holders(structs: list[ast.StructDef], enums: list[ast.EnumDef]) -> set[str]:
    """Find the structs and enums that hold a function, directly or through another one.

    Args:
        structs: The program's structs.
        enums: The program's enums.

    Returns:
        Their names.
    """
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for sd in structs:
            if sd.name not in found and any(holds_fn(f.ty, found) for f in sd.fields):
                found.add(sd.name)
                changed = True
        for ed in enums:
            if ed.name not in found and any(holds_fn(t, found) for v in ed.variants for t in v.tys):
                found.add(ed.name)
                changed = True
    return found


def is_copy(t: Type) -> bool:
    """Whether values of this type are `Copy` in Rust, so reading them never needs `.clone()`."""
    return is_copy_type(t)


def str_lit(v: str) -> str:
    """Return a Rust string literal for a string value.

    Args:
        v: The text.

    Returns:
        The quoted literal with the necessary escapes.
    """
    out = []
    for ch in v:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\0":
            out.append("\\0")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


class Gen:
    """Emits the Rust source of one program.

    Statements are written line by line with indentation; expressions are rendered by module-level helpers. Use
    `generate()` rather than this class directly.
    """
    def __init__(self) -> None:
        """Create an empty generator."""
        self.out: list[str] = []
        self.depth = 0
        self.tmp = 0
        self.opaque: set[str] = set()  # structs and enums that hold a function: no `PartialEq`, opaque `Debug`

    def line(self, text: str) -> None:
        """Append one line at the current indentation."""
        self.out.append("    " * self.depth + text)

    # ---- program ----------------------------------------------------------------------

    def program(self, prog: ast.Program) -> str:
        """Generate the whole Rust program.

        Args:
            prog: The checked program.

        Returns:
            The Rust source: struct definitions, functions, then `main` holding the top-level statements.
        """
        self.out.append(PRELUDE)
        structs = [s for s in prog.body if isinstance(s, ast.StructDef)]
        fns = [s for s in prog.body if isinstance(s, ast.FnDef) and not isinstance(s, ast.ExternFn)]
        enums = [s for s in prog.body if isinstance(s, ast.EnumDef)]
        rest = [s for s in prog.body if not isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef))]
        self.opaque = fn_holders(structs, enums)
        for ed in enums:
            self.enum(ed)
            self.out.append("")
        for sd in structs:
            self.struct(sd)
            self.out.append("")
        for f in fns:
            self.fn(f)
            self.out.append("")
        self.line("fn main() {")
        self.block(rest)
        self.line("}")
        return "\n".join(self.out) + "\n"

    def enum(self, ed: ast.EnumDef) -> None:
        """Emit an enum with its variants; `Debug` prints a value like `Circle(2.0)`."""
        opaque = ed.name in self.opaque
        self.line("#[derive(Clone)]" if opaque else "#[derive(Clone, PartialEq, Debug)]")
        self.line(f"enum {tname(ed.name)} {{")
        for v in ed.variants:
            data = f"({', '.join(rust_type(t) for t in v.tys)})" if v.tys else ""
            self.line(f"    {v.name}{data},")
        self.line("}")
        if opaque:
            self.line(f"impl std::fmt::Debug for {tname(ed.name)} {{")
            self.line("    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {")
            self.line(f'        write!(f, "{ed.display or ed.name}(..)")')
            self.line("    }")
            self.line("}")
        if ed.methods:
            self.out.append("")
            self.line(f"impl {tname(ed.name)} {{")
            self.depth += 1
            for i, m in enumerate(ed.methods):
                if i:
                    self.out.append("")
                self.fn(m)
            self.depth -= 1
            self.line("}")

    def match(self, s: ast.Match) -> None:
        """Emit a `match` as a Rust `match` on an owned copy of the value, binding each arm's data by value.

        Strings are matched as `&str`; `Option` and `Result` arms become `Some`/`None`/`Ok`/`Err` patterns.
        """
        scrutinee = f"{emit_ref(s.value)}.as_str()" if s.value.ty == STR else emit(s.value)
        self.line(f"match {scrutinee} {{")
        self.depth += 1
        for arm in s.arms:
            self.line(f"{self.arm_pattern(s, arm)} => {{")
            self.block(arm.body)
            self.line("}")
        self.depth -= 1
        self.line("}")

    ARM_WRAPPERS = {"some": "Some", "ok": "Ok", "err": "Err"}

    def arm_pattern(self, s: ast.Match, arm: ast.MatchArm) -> str:
        """Return the Rust pattern of one `match` arm."""
        if arm.variant == "_":
            return "_"
        if arm.variant == "none":
            return "None"
        if arm.literal is not None:
            lit = arm.literal
            if isinstance(lit, ast.Str):
                return str_lit(lit.value)
            if isinstance(lit, ast.Bool):
                return "true" if lit.value else "false"
            return emit_ref(lit)
        if not arm.binds:
            return f"{tname(s.enum)}::{arm.variant}"
        binds = ", ".join("_" if v is None else f"{'mut ' if v.mutable else ''}{ident(v.name)}" for v in arm.vars)
        head = self.ARM_WRAPPERS.get(arm.variant) if arm.type is None else f"{tname(s.enum)}::{arm.variant}"
        return f"{head}({binds})"

    def struct(self, sd: ast.StructDef) -> None:
        """Emit a struct, its `Debug` implementation and its methods."""
        name = tname(sd.name)
        opaque = sd.name in self.opaque
        self.line("#[derive(Clone)]" if opaque else "#[derive(Clone, PartialEq)]")
        self.line(f"struct {name} {{")
        for f in sd.fields:
            self.line(f"    {ident(f.name)}: {rust_type(f.ty)},")
        self.line("}")
        self.out.append("")
        shown = ", ".join(f"{f.name}={{:?}}" for f in sd.fields)
        args = ", ".join(f"self.{ident(f.name)}" for f in sd.fields)
        self.line(f"impl std::fmt::Debug for {name} {{")
        self.line("    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {")
        if opaque:  # a function cannot be shown, so neither can the fields around it (the checker never prints it)
            self.line(f'        write!(f, "{sd.display or sd.name}(..)")')
        else:
            self.line(f'        write!(f, "{sd.display or sd.name}({shown})", {args})')
        self.line("    }")
        self.line("}")
        if sd.methods:
            self.out.append("")
            self.line(f"impl {name} {{")
            self.depth += 1
            for i, m in enumerate(sd.methods):
                if i:
                    self.out.append("")
                self.fn(m)
            self.depth -= 1
            self.line("}")

    def fn(self, f: ast.FnDef) -> None:
        """Emit a function or method; lent parameters are written `&T` and `self` as `&self` or `&mut self`."""
        params = []
        if f.has_self:
            params.append("&mut self" if f.self_mut else "&self")
        params += [
            f"{'mut ' if p.info and p.info.mutable else ''}{ident(p.name)}: {'&' if p.info.by_ref else ''}{rust_type(p.info.ty)}"
            for p in f.params
        ]
        ret = "" if f.ty.ret == UNIT else f" -> {rust_type(f.ty.ret)}"
        name = mident(f.name) if f.has_self else ident(f.name)
        self.line(f"fn {name}({', '.join(params)}){ret} {{")
        self.block(f.body)
        self.line("}")

    def block(self, body: list[ast.Stmt]) -> None:
        """Emit the statements of an indented block."""
        self.depth += 1
        for s in body:
            self.stmt(s)
        self.depth -= 1

    # ---- statements --------------------------------------------------------------------

    def stmt(self, s: ast.Stmt) -> None:
        """Emit one statement."""
        match s:
            case ast.ExprStmt(e):
                self.line(f"{emit_ref(e)};")
            case ast.Assign():
                self.assign(s)
            case ast.Unpack():
                self.unpack(s)
            case ast.If():
                self.if_(s, "if")
            case ast.While(cond, body):
                self.line(f"while {emit_ref(cond)} {{")
                self.block(body)
                self.line("}")
            case ast.For():
                self.for_(s)
            case ast.Match():
                self.match(s)
            case ast.Return(value):
                self.line("return;" if value is None else f"return {emit(value)};")
            case ast.Break():
                self.line("break;")
            case ast.Continue():
                self.line("continue;")
            case _:
                raise ZyloError(f"{type(s).__name__} is not supported by codegen", s.line, s.col)

    def unpack(self, s: ast.Unpack) -> None:
        """Emit a tuple unpacking through a temporary, declaring or assigning each target."""
        self.tmp += 1
        tmp = f"__t{self.tmp}"
        self.line(f"let {tmp}: {rust_type(s.value.ty)} = {emit(s.value)};")
        for i, (t, declares, var) in enumerate(zip(s.targets, s.declares, s.vars)):
            val = f"{tmp}.{i}" + ("" if is_copy(var.ty) else ".clone()")
            if declares:
                self.line(f"let {'mut ' if var.mutable else ''}{ident(t.id)}: {rust_type(var.ty)} = {val};")
            else:
                self.line(f"{ident(t.id)} = {val};")

    def assign(self, s: ast.Assign) -> None:
        """Emit an assignment, a declaration, an index or field store, or a dict insertion."""
        if isinstance(s.target, ast.Index) and isinstance(s.target.value.ty, DictT):
            base = emit_mut(s.target.value)
            self.line(f"{base}.insert({emit(s.target.index)}, {emit(s.value)});")
        elif isinstance(s.target, (ast.Index, ast.Attr)):
            self.line(f"{emit_mut(s.target)} = {emit(s.value)};")
        elif s.declares:
            mut = "mut " if s.var.mutable else ""
            self.line(f"let {mut}{ident(s.target.id)}: {rust_type(s.var.ty)} = {emit(s.value)};")
        elif (isinstance(s.value, ast.BinOp) and s.value.op == "+" and s.value.left.ty == STR
              and isinstance(s.value.left, ast.Name) and s.value.left.id == s.target.id
              and not any(isinstance(n, ast.Name) and n.id == s.target.id for n in ast.walk(s.value.right))):
            # `s += x` appends in place: rebuilding the string each time would make building text quadratic
            self.line(f"{ident(s.target.id)}.push_str(&{emit_ref(s.value.right)});")
        else:
            self.line(f"{ident(s.target.id)} = {emit(s.value)};")

    PATTERNS = {"some": "Some", "ok": "Ok", "err": "Err"}
    OPPOSITE = {"some": "none", "none": "some", "ok": "err", "err": "ok"}

    def pattern(self, kind: str, bind) -> str:
        """Return a Rust match pattern for an `is` check.

        Args:
            kind: "some", "none", "ok" or "err".
            bind: The narrowed variable bound by the pattern, or None.

        Returns:
            For example `Some(x)` or `None`.
        """
        if kind == "none":
            return "None"
        inner = f"{'mut ' if bind.mutable else ''}{ident(bind.name)}" if bind else "_"
        return f"{self.PATTERNS[kind]}({inner})"

    def narrowed_if(self, s: ast.If) -> None:
        """Emit an `if x is K:` that narrows `x`.

        It becomes `match x { K(x) => {...}, other(x) => {...} }`. With an early exit and no `else` it becomes
        `let x = match x { K(x) => {exit}, other(x) => x };`, which narrows `x` for the rest of the block.

        Args:
            s: The `if` statement.
        """
        kind, other = s.cond.kind, self.OPPOSITE[s.cond.kind]
        scrutinee = emit(s.cond.value)
        name = ident(s.cond.value.id)
        if s.after_bind:
            mut = "mut " if s.after_bind.mutable else ""
            self.line(f"let {mut}{name}: {rust_type(s.after_bind.ty)} = match {scrutinee} {{")
            self.depth += 1
            self.line(f"{self.pattern(kind, s.then_bind)} => {{")
            self.block(s.then)
            self.line("}")
            self.line(f"{self.pattern(other, s.after_bind)} => {name},")
            self.depth -= 1
            self.line("};")
            return
        self.line(f"match {scrutinee} {{")
        self.depth += 1
        self.line(f"{self.pattern(kind, s.then_bind)} => {{")
        self.block(s.then)
        self.line("}")
        self.line(f"{self.pattern(other, s.else_bind)} => {{")
        self.block(s.else_)
        self.line("}")
        self.depth -= 1
        self.line("}")

    def if_(self, s: ast.If, kw: str) -> None:
        """Emit an `if`; an `if` that narrows an Option or Result is emitted as a `match`."""
        if s.after_bind or s.then_bind or s.else_bind:
            self.narrowed_if(s)
            return
        self.line(f"{kw} {emit_ref(s.cond)} {{")
        self.block(s.then)
        self._else_tail(s)

    def _else_tail(self, s: ast.If) -> None:
        if len(s.else_) == 1 and isinstance(s.else_[0], ast.If):
            nested = s.else_[0]
            self.line(f"}} else if {emit_ref(nested.cond)} {{")
            self.block(nested.then)
            self._else_tail(nested)
            return
        if s.else_:
            self.line("} else {")
            self.block(s.else_)
        self.line("}")

    def for_(self, s: ast.For) -> None:
        """Emit a `for` loop, borrowing the list when nothing can modify it and cloning it otherwise."""
        mut = "mut " if s.info and s.info.mutable else ""
        it = s.iter
        if isinstance(it, ast.Call) and it.func == "range":
            lo, hi = ("0i64", emit_ref(it.args[0])) if len(it.args) == 1 else (emit_ref(it.args[0]), emit_ref(it.args[1]))
            src = f"({lo})..({hi})"
        elif isinstance(it.ty, DictT):
            src = f"{emit_ref(it)}.keys().cloned().collect::<Vec<_>>()"
        elif isinstance(it.ty, SetT):
            src = f"{emit_ref(it)}.iter().cloned().collect::<Vec<_>>()"
        elif it.ty == STR:
            src = f"{emit_ref(it)}.chars().map(|c| c.to_string()).collect::<Vec<String>>()"
        elif is_readonly_place(it):
            src = f"{emit_ref(it)}.iter().cloned()"  # nothing can modify it: borrow instead of cloning the list
        else:
            src = emit(it)  # owned: clones the list once, so the body may mutate the original like in Python
        if s.targets:
            pat = "(" + ", ".join(f"{'mut ' if i.mutable else ''}{ident(n)}" for n, i in zip(s.targets, s.infos)) + ")"
            self.line(f"for {pat} in {src} {{")
        else:
            self.line(f"for {mut}{ident(s.var)} in {src} {{")
        self.block(s.body)
        self.line("}")


# ---- expressions ----------------------------------------------------------------------------

def emit(e: ast.Expr) -> str:
    """Expression as an owned value (clones non-Copy values read from a variable)."""
    code = emit_ref(e)
    if isinstance(e, ast.Name) and e.var is None:
        return code  # a top-level function used as a value: already a fresh function value
    if isinstance(e, (ast.Name, ast.Index, ast.Attr)) and not is_copy(e.ty) and not getattr(e, "variant", None):
        return f"{code}.clone()"
    return code


def is_readonly_place(e: ast.Expr) -> bool:
    """A variable (or a field/index path from one) that is never modified anywhere in its scope."""
    while isinstance(e, (ast.Attr, ast.Index)):
        e = e.value
    return isinstance(e, ast.Name) and e.var is not None and not e.var.mutable and e.var.name != "self"


def is_place(e: ast.Expr) -> bool:
    """A side-effect-free place (variable, field or index chain): safe to evaluate twice."""
    if isinstance(e, ast.Name):
        return True
    if isinstance(e, ast.Attr):
        return is_place(e.value)
    if isinstance(e, ast.Index):
        return is_place(e.value) and isinstance(e.index, (ast.Int, ast.Name))
    return False


def emit_mut(e: ast.Expr) -> str:
    """A place expression that can be modified: like `emit_ref`, but `d[k]` on a dict becomes `get_mut`."""
    match e:
        case ast.Index(v, i) if isinstance(v.ty, DictT):
            return f'{emit_mut(v)}.get_mut(&{emit_ref(i)}).expect("key not found")'
        case ast.Index(v, i) if not isinstance(v.ty, TupleT):
            return f"(*__zy_at(&mut {emit_mut(v)}, {emit_ref(i)}))"
        case ast.Index(v, i):
            return f"{emit_mut(v)}.{i.value}"
        case ast.Attr(v, name):
            return f"{emit_mut(v)}.{ident(name)}"
    return emit_ref(e)


def emit_ref(e: ast.Expr) -> str:
    """Expression without cloning: reads without consuming (or an assignable place)."""
    match e:
        case ast.Int(v):
            return f"{v}i64"
        case ast.Float(v):
            return f"{v!r}f64"
        case ast.Str(v):
            return f"String::from({str_lit(v)})"
        case ast.Bool(v):
            return "true" if v else "false"
        case ast.Name(i) if e.var is None and isinstance(e.ty, FnT):
            return fn_value(i)
        case ast.Name(i):
            return f"(*{ident(i)})" if e.var is not None and e.var.by_ref else ident(i)
        case ast.Lambda():
            return lambda_value(e)
        case ast.CallValue(callee, args):
            return f"({emit_ref(callee)})({', '.join(emit(a) for a in args)})"
        case ast.TupleLit(items):
            return "(" + ", ".join(emit(x) for x in items) + ")"
        case ast.DictLit(keys, values):
            if isinstance(e.ty, SetT):
                return "BTreeSet::new()"
            if not keys:
                return "BTreeMap::new()"
            return "BTreeMap::from([" + ", ".join(f"({emit(k)}, {emit(v)})" for k, v in zip(keys, values)) + "])"
        case ast.SetLit(items):
            return "BTreeSet::from([" + ", ".join(emit(x) for x in items) + "])"
        case ast.Index(v, i) if isinstance(v.ty, DictT):
            return f"{emit_ref(v)}[&{emit_ref(i)}]"
        case ast.Index(v, i) if isinstance(v.ty, TupleT):
            return f"{emit_ref(v)}.{i.value}"
        case ast.Index(v, i) if v.ty == STR:
            return f"__zy_char(&{emit_ref(v)}, {emit_ref(i)})"
        case ast.Slice(v, lo, hi):
            def b(x: ast.Expr | None) -> str:
                return "None" if x is None else f"Some({emit_ref(x)})"

            if v.ty == STR:
                return (f"{{ let __b: Vec<char> = {emit_ref(v)}.chars().collect(); "
                        f"let (__l, __h) = __zy_range(__b.len(), {b(lo)}, {b(hi)}); "
                        f"__b[__l..__h].iter().collect::<String>() }}")
            return (f"{{ let __b = &{emit_ref(v)}; let (__l, __h) = __zy_range(__b.len(), {b(lo)}, {b(hi)}); "
                    f"__b[__l..__h].to_vec() }}")
        case ast.Index(v, i):
            base = emit_ref(v)
            return f"{base}[__zy_i({emit_ref(i)}, {base}.len())]" if is_place(v) else f"{base}[({emit_ref(i)}) as usize]"
        case ast.Attr() if e.variant:
            return f"{tname(e.variant[0])}::{e.variant[1]}"
        case ast.Attr(v, name):
            return f"{emit_ref(v)}.{ident(name)}"
        case ast.NoneLit():
            return "None"
        case ast.FString(parts):
            return fstring(parts)
        case ast.IsCheck(v, kind):
            return f"{emit_ref(v)}.is_{kind}()"
        case ast.Try(v):
            return f"({emit(v)}?)"
        case ast.MethodCall() if e.variant:
            return f"{tname(e.variant[0])}::{e.variant[1]}({', '.join(emit(a) for a in e.args)})"
        case ast.MethodCall(v, name, args) if e.field_call:
            return f"({emit_ref(v)}.{ident(name)})({', '.join(emit(a) for a in args)})"
        case ast.MethodCall(v, name, args) if isinstance(v.ty, (OptionT, ResultT)):
            return f"{emit(v)}.{name}({', '.join(emit(a) for a in args)})"
        case ast.MethodCall(v, name, args) if v.ty == STR or isinstance(v.ty, (ListT, DictT, SetT, CellT)):
            return type_method(e)
        case ast.MethodCall(v, name, args):
            target = _METHODS.get((v.ty.name, name))
            recv = emit_mut(v) if target is not None and target.self_mut else emit_ref(v)
            # a method that borrows `self` mutably cannot also lend out parts of it: pass those arguments by value
            code = call_args(target.params, args, by_ref_ok=not target.self_mut) if target else ", ".join(emit(a) for a in args)
            return f"{recv}.{mident(name)}({code})"
        case ast.ListLit(items):
            if not items:
                return "Vec::new()"
            return f"vec![{', '.join(emit(i) for i in items)}]"
        case ast.UnaryOp("not", x):
            return f"(!{emit_ref(x)})"
        case ast.UnaryOp(op, x):
            return f"({op}{emit_ref(x)})"
        case ast.BinOp(op, l, r):
            return binop(e, op, l, r)
        case ast.Call():
            return call(e)
    raise ZyloError(f"unsupported node: {type(e).__name__}", e.line, e.col)


def fn_value(name: str) -> str:
    """A top-level function used as a value: a function value wrapping a call (lent parameters stay lent inside)."""
    fd = _FNS[name]
    names = [f"__a{i}" for i in range(len(fd.params))]
    params = ", ".join(f"{n}: {rust_type(t)}" for n, t in zip(names, fd.ty.params))
    args = ", ".join(("&" if p.info.by_ref else "") + n for n, p in zip(names, fd.params))
    return f"(std::rc::Rc::new(|{params}| {ident(name)}({args})) as {rust_type(fd.ty)})"


def lambda_value(e: ast.Lambda) -> str:
    """An anonymous function: a function value that owns a copy of everything it captured."""
    ft = e.ty
    params = ", ".join(f"{'mut ' if p.info.mutable else ''}{ident(p.name)}: {rust_type(p.info.ty)}" for p in e.params)
    ret = "" if ft.ret == UNIT else f" -> {rust_type(ft.ret)}"
    if e.expr is not None:
        body = f"{emit(e.expr)}" if ft.ret != UNIT else f"{emit_ref(e.expr)};"
    else:
        g = Gen()
        g.block(e.body)
        body = "\n".join(g.out)
    copies = "".join(
        f"let {ident(inner.name)} = {emit(ast.Name(outer.name, var=outer, ty=outer.ty))}; "
        for outer, inner in e.captures)
    return (f"{{ {copies}std::rc::Rc::new(move |{params}|{ret} {{ {body} }}) as {rust_type(ft)} }}")


def fstring(parts: list) -> str:
    """Render an f-string as a Rust `format!` call.

    Args:
        parts: The literal strings and `FormatPart`s of the f-string.

    Returns:
        The Rust expression.
    """
    fmt, args = [], []
    for p in parts:
        if isinstance(p, str):
            fmt.append(p.replace("{", "{{").replace("}", "}}"))
            continue
        spec = p.spec[:-1] if p.spec and p.spec[-1] in "fds" else p.spec
        t = p.value.ty
        if t == FLOAT or t not in (INT, BOOL, STR):
            spec += "?"  # Debug: `2.0` for floats, and the container/struct representation
        fmt.append("{" + (f":{spec}" if spec else "") + "}")
        args.append(emit_ref(p.value))
    return f"format!({str_lit(''.join(fmt))}" + "".join(f", {a}" for a in args) + ")"


_METHODS: dict[tuple[str, str], ast.FnDef] = {}
_FNS: dict[str, ast.FnDef] = {}  # the program's top-level functions by (linked) name
_EXTERNS: dict[str, ast.ExternFn] = {}


def extern_call(x: ast.ExternFn, args: list[ast.Expr]) -> str:
    """Expand an `extern fn`: a template with {0}, {1}... or a Rust path called with the arguments (by value)."""
    vals = [emit(a) for a in args]
    if re.search(r"\{\d+\}", x.template):
        return "(" + re.sub(r"\{(\d+)\}", lambda m: f"({vals[int(m.group(1))]})", x.template) + ")"
    return f"({x.template}({', '.join(vals)}))"


def call_args(params: list[ast.Param], args: list[ast.Expr], by_ref_ok: bool = True) -> str:
    """Call arguments: parameters the callee never modifies are lent (`&x`) instead of cloned."""
    out = []
    for p, a in zip(params, args):
        out.append("&" + emit_ref(a) if by_ref_ok and p.info is not None and p.info.by_ref else emit(a))
    return ", ".join(out)


HIGHER_ORDER = {"map", "filter", "fold", "reduce", "sort_by", "any", "all", "find"}


def higher_order(e: ast.MethodCall) -> str:
    """Render `map`, `filter`, `fold`, `reduce`, `sort_by`, `any`, `all` and `find` on a list.

    The function argument is bound once (`__f`, by reference so a variable holding it is not cloned) and called for
    each element.

    Args:
        e: The method call.

    Returns:
        The Rust expression.
    """
    v, n, args = e.value, e.name, e.args
    fn = args[-1]
    bound = f"let __f = &{emit_ref(fn)};"
    if n == "sort_by":
        body = f"{emit_mut(v)}.sort_by(|__a, __b| (__f)(__a.clone()).partial_cmp(&(__f)(__b.clone())).unwrap())"
        return f"{{ {bound} {body} }}"
    it = f"{emit_ref(v)}.iter().cloned()"
    body = {
        "map": f"{it}.map(|__x| (__f)(__x)).collect::<Vec<_>>()",
        "filter": f"{it}.filter(|__x| (__f)(__x.clone())).collect::<Vec<_>>()",
        "any": f"{it}.any(|__x| (__f)(__x))",
        "all": f"{it}.all(|__x| (__f)(__x))",
        "find": f"{it}.find(|__x| (__f)(__x.clone()))",
        "reduce": f"{it}.reduce(|__acc, __x| (__f)(__acc, __x))",
        "fold": f"{it}.fold({emit(args[0])}, |__acc, __x| (__f)(__acc, __x))",
    }[n]
    return f"{{ {bound} {body} }}"


def type_method(e: ast.MethodCall) -> str:
    """Render a method call on a built-in str, list, dict or set.

    Args:
        e: The method call.

    Returns:
        The Rust expression.
    """
    v, n, args = e.value, e.name, e.args
    a, t = emit_ref(v), v.ty
    m = emit_mut(v)
    r = [emit_ref(x) for x in args]
    if t == STR:
        return {
            "strip": lambda: f"{a}.trim().to_string()",
            "upper": lambda: f"{a}.to_uppercase()",
            "lower": lambda: f"{a}.to_lowercase()",
            "split": lambda: (f"{a}.split({r[0]}.as_str()).map(|x| x.to_string()).collect::<Vec<String>>()" if r
                              else f"{a}.split_whitespace().map(|x| x.to_string()).collect::<Vec<String>>()"),
            "join": lambda: f"{r[0]}.join({a}.as_str())",
            "replace": lambda: f"{a}.replace({r[0]}.as_str(), {r[1]}.as_str())",
            "startswith": lambda: f"{a}.starts_with({r[0]}.as_str())",
            "endswith": lambda: f"{a}.ends_with({r[0]}.as_str())",
            "find": lambda: f"__zy_find(&{a}, &{r[0]})",
            "count": lambda: f"({a}.matches({r[0]}.as_str()).count() as i64)",
            "lines": lambda: f"{a}.lines().map(|x| x.to_string()).collect::<Vec<String>>()",
            "chars": lambda: f"{a}.chars().map(|c| c.to_string()).collect::<Vec<String>>()",
            "repeat": lambda: f"{a}.repeat(({r[0]}) as usize)",
        }[n]()
    if isinstance(t, CellT):
        return {
            "get": lambda: f"{a}.get()",
            "set": lambda: f"{a}.set({emit(args[0])})",
            "update": lambda: f"{a}.update(&{r[0]})",
        }[n]()
    if isinstance(t, ListT) and n in HIGHER_ORDER:
        return higher_order(e)
    if isinstance(t, ListT):
        sort = f"{m}.sort_by(|a, b| a.partial_cmp(b).unwrap())" if t.elem == FLOAT else f"{m}.sort()"
        return {
            "append": lambda: f"{m}.push({emit(args[0])})",
            "pop": lambda: f"{m}.pop()",
            "insert": lambda: f"__zy_insert(&mut {m}, {r[0]}, {emit(args[1])})",
            "remove": lambda: f"__zy_remove(&mut {m}, &{r[0]})",
            "clear": lambda: f"{m}.clear()",
            "reverse": lambda: f"{m}.reverse()",
            "sort": lambda: sort,
            "extend": lambda: f"{m}.extend({emit(args[0])})",
            "index": lambda: f"{a}.iter().position(|y| y == &{r[0]}).map(|p| p as i64)",
            "count": lambda: f"({a}.iter().filter(|y| *y == &{r[0]}).count() as i64)",
        }[n]()
    if isinstance(t, DictT):
        return {
            "get": lambda: (f"{a}.get(&{r[0]}).cloned().unwrap_or({emit(args[1])})" if len(args) == 2
                            else f"{a}.get(&{r[0]}).cloned()"),
            "keys": lambda: f"{a}.keys().cloned().collect::<Vec<_>>()",
            "values": lambda: f"{a}.values().cloned().collect::<Vec<_>>()",
            "items": lambda: f"{a}.iter().map(|(k, v)| (k.clone(), v.clone())).collect::<Vec<_>>()",
            "pop": lambda: f"{m}.remove(&{r[0]})",
            "clear": lambda: f"{m}.clear()",
        }[n]()
    return {
        "add": lambda: f"{m}.insert({emit(args[0])})",
        "remove": lambda: f"{m}.remove(&{r[0]})",
        "union": lambda: f"{a}.union(&{r[0]}).cloned().collect::<BTreeSet<_>>()",
        "intersection": lambda: f"{a}.intersection(&{r[0]}).cloned().collect::<BTreeSet<_>>()",
        "difference": lambda: f"{a}.difference(&{r[0]}).cloned().collect::<BTreeSet<_>>()",
        "clear": lambda: f"{m}.clear()",
    }[n]()


def binop(e: ast.BinOp, op: str, left: ast.Expr, right: ast.Expr) -> str:
    """Render a binary operation, including string concatenation and `in`.

    Args:
        e: The operation node.
        op: Its operator.
        left: Left operand.
        right: Right operand.

    Returns:
        The Rust expression, fully parenthesised.
    """
    a, b = emit_ref(left), emit_ref(right)
    if op in ("in", "not in"):
        if right.ty == STR:
            test = f"{b}.contains({a}.as_str())"
        elif isinstance(right.ty, DictT):
            test = f"{b}.contains_key(&{a})"
        else:
            test = f"{b}.contains(&{a})"
        return f"(!{test})" if op == "not in" else f"({test})"
    if op == "+" and left.ty == STR:
        return f'format!("{{}}{{}}", {a}, {b})'
    rust_op = {"and": "&&", "or": "||"}.get(op, op)
    return f"({a} {rust_op} {b})"


def call(e: ast.Call) -> str:
    """Render a call of a built-in, an `extern fn`, a struct constructor or a user function.

    Args:
        e: The call.

    Returns:
        The Rust expression.
    """
    args = e.args
    if e.var is not None:  # calling a variable that holds a function
        callee = f"(*{ident(e.func)})" if e.var.by_ref else ident(e.func)
        return f"({callee})({', '.join(emit(a) for a in args)})"
    if e.ctor_fields is not None:
        fields = ", ".join(f"{ident(n)}: {emit(a)}" for n, a in zip(e.ctor_fields, args))
        return f"{tname(e.func)} {{ {fields} }}"
    if e.func in _EXTERNS:
        return extern_call(_EXTERNS[e.func], args)
    if e.func in _FNS and e.func in BUILTIN_NAMES:  # a user function shadowing a builtin name
        return f"{ident(e.func)}({call_args(_FNS[e.func].params, args)})"
    match e.func:
        case "print":
            if not args:
                return 'println!()'
            fmts = ["{:?}" if a.ty == FLOAT or isinstance(a.ty, (ListT, StructT, EnumT, OptionT, ResultT, TupleT, DictT, SetT, CellT)) else "{}"
                    for a in args]
            return f'println!("{" ".join(fmts)}", {", ".join(emit_ref(a) for a in args)})'
        case "cell":
            return f"__ZyCell::new({emit(args[0])})"
        case "len":
            x = emit_ref(args[0])
            return f"({x}.chars().count() as i64)" if args[0].ty == STR else f"({x}.len() as i64)"
        case "append":
            return f"{emit_mut(args[0])}.push({emit(args[1])})"
        case "argv":
            return "std::env::args().skip(1).collect::<Vec<String>>()"
        case "input":
            return f"__zy_input(&{emit_ref(args[0])})" if args else '__zy_input("")'
        case "read_line":
            return "__zy_read_line()"
        case "read_stdin":
            return "__zy_read_stdin()"
        case "read_file" | "write_file" | "append_file" | "list_dir":
            return f"__zy_{e.func}(" + ", ".join(f"&{emit_ref(a)}" for a in args) + ")"
        case "file_exists":
            return f"std::path::Path::new(&{emit_ref(args[0])}).exists()"
        case "env":
            return f"std::env::var(&{emit_ref(args[0])}).ok()"
        case "time":
            return "__zy_time()"
        case "sleep":
            return f"std::thread::sleep(std::time::Duration::from_secs_f64(({emit_ref(args[0])}).max(0.0)))"
        case "eprint":
            if not args:
                return 'eprintln!()'
            fmts = ["{:?}" if a.ty == FLOAT or isinstance(a.ty, (ListT, StructT, EnumT, OptionT, ResultT, TupleT, DictT, SetT, CellT)) else "{}"
                    for a in args]
            return f'eprintln!("{" ".join(fmts)}", {", ".join(emit_ref(a) for a in args)})'
        case "sum":
            return f"{emit_ref(args[0])}.iter().sum::<{rust_type(e.ty)}>()"
        case "min" | "max":
            x = emit_ref(args[0])
            if len(args) == 2:
                y = emit_ref(args[1])
                return f"(({x}).{e.func}({y}))" if e.ty == FLOAT else f"std::cmp::{e.func}({x}, {y})"
            msg = f'"{e.func}() of an empty list"'
            if e.ty == FLOAT:
                return f"{x}.iter().cloned().reduce(f64::{e.func}).expect({msg})"
            return f"{x}.iter().cloned().{e.func}().expect({msg})"
        case "abs":
            return f"({emit_ref(args[0])}).abs()"
        case "sorted":
            cmp = "__v.sort_by(|a, b| a.partial_cmp(b).unwrap())" if e.ty.elem == FLOAT else "__v.sort()"
            return f"{{ let mut __v = {emit(args[0])}; {cmp}; __v }}"
        case "reversed":
            return f"{{ let mut __v = {emit(args[0])}; __v.reverse(); __v }}"
        case "enumerate":
            return f"{emit_ref(args[0])}.iter().cloned().enumerate().map(|(i, x)| (i as i64, x)).collect::<Vec<_>>()"
        case "zip":
            return f"{emit_ref(args[0])}.iter().cloned().zip({emit_ref(args[1])}.iter().cloned()).collect::<Vec<_>>()"
        case "sqrt":
            return f"({emit_ref(args[0])}).sqrt()"
        case "pow":
            return f"({emit_ref(args[0])}).powf({emit_ref(args[1])})"
        case "round" | "floor" | "ceil":
            return f"(({emit_ref(args[0])}).{e.func}() as i64)"
        case "int":
            x, t = emit_ref(args[0]), args[0].ty
            if t == INT:
                return x
            if t == FLOAT:
                return f"({x} as i64)"
            return f'({x}.trim().parse::<i64>().expect("int(): not a numeric string"))'
        case "float":
            x, t = emit_ref(args[0]), args[0].ty
            if t == FLOAT:
                return x
            if t == INT:
                return f"({x} as f64)"
            return f'({x}.trim().parse::<f64>().expect("float(): not a numeric string"))'
        case "str":
            x, t = emit_ref(args[0]), args[0].ty
            if t == STR:
                return f"{x}.clone()"
            if t == FLOAT:
                return f'format!("{{:?}}", {x})'
            return f"{x}.to_string()"
        case "some":
            return f"Some({emit(args[0])})"
        case "ok":
            return f"Ok({emit(args[0])})"
        case "err":
            return f"Err({emit(args[0])})"
        case "parse_int":
            return f"{emit_ref(args[0])}.trim().parse::<i64>().ok()"
        case "parse_float":
            return f"{emit_ref(args[0])}.trim().parse::<f64>().ok()"
        case "exit":
            return f"std::process::exit(({emit_ref(args[0])}) as i32)"
        case "range":
            raise ZyloError("range(...) can only be used in a `for`", e.line, e.col)
        case name:
            fd = _FNS.get(name)
            return f"{ident(name)}({call_args(fd.params, args) if fd else ', '.join(emit(a) for a in args)})"


def generate(prog: ast.Program) -> str:
    """Generate Rust source for a checked program.

    Args:
        prog: The program, annotated by the checker.

    Returns:
        The Rust source text, ready for `rustc`.
    """
    _METHODS.clear()
    _EXTERNS.clear()
    _EXTERNS.update({s.name: s for s in prog.body if isinstance(s, ast.ExternFn)})
    _FNS.clear()
    _FNS.update({s.name: s for s in prog.body if isinstance(s, ast.FnDef)})
    for s in prog.body:
        if isinstance(s, (ast.StructDef, ast.EnumDef)):
            _METHODS.update({(s.name, m.name): m for m in s.methods})
    return Gen().program(prog)
