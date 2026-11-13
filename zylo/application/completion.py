"""Completion: what can be typed at a position of a document, even while the line is unfinished.

The position decides what is offered:

- after a dot, the members of what is in front of it: the exports of a module, the variants of an enum, the fields and
  methods of a struct (by the type the checker finds for the value) or the methods of `str`, lists, dicts, sets, cells,
  options and results;
- on the `import` and `from ... import` lines, modules and the names a module exports;
- where a type is expected (after `->`, after a parameter's or a field's `:`), types;
- inside the parentheses of a call, the parameters (or struct fields) not yet given by name, as `name=`;
- anywhere else, the variables and parameters in scope, the names of the file (definitions, imports, modules), the
  built-in functions and the keywords.

A line being typed rarely parses, so nothing here needs the cursor's statement to be valid: the text before the cursor
is analysed on its own, and the type of a value in front of a dot is found by asking the checker about that value
alone in place of the unfinished line.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from zylo.application.editor import Call, call_at, callee_entity, signature_of, signature_text, variable_type
from zylo.domain import ast
from zylo.domain.builtins import FUNCTIONS, METHODS, Builtin, describe, receiver_kind
from zylo.packaging.stdlib import stdlib_module, stdlib_modules
from zylo.semantics.navigation import Entity, FileIndex, Project, Resolution, deref, doc_of
from zylo.syntax.formatter import expr_text, type_text
from zylo.syntax.lexer import KEYWORDS

TYPE_NAMES = {"int": "A whole number.", "float": "A decimal number.", "bool": "`true` or `false`.", "str": "Text.",
              "list": "An ordered collection: `list[int]`.", "set": "A collection without duplicates: `set[int]`.",
              "dict": "A map from keys to values: `dict[str, int]`.", "tuple": "A fixed group of values: `tuple[int, str]`.",
              "Result": "A success or an error: `Result[int, str]`.", "Cell": "A shared mutable box: `Cell[int]`."}
TYPE_CHARS = r"[\w\[\], .?|]*"
IDENT = r"[^\W\d]\w*"
DEF_HEADER = re.compile(r"(?:pub\s+)?(?:extern\s+)?(fn|struct|enum)\b")
BLOCK_HEADERS = {"if", "elif", "while", "for", "match"}


@dataclass
class Completion:
    """A completion candidate.

    Attributes:
        label: The text shown (and inserted, unless `insert_text` says otherwise).
        kind: "keyword", "function", "method", "field", "struct", "enum", "variant", "variable", "parameter", "module"
            or "type".
        detail: A short description, such as a signature or a type.
        documentation: The longer description (Markdown), or "".
        insert_text: What to insert when it differs from the label.
        rank: Orders the candidates: the lower, the nearer the top.
    """

    label: str
    kind: str
    detail: str = ""
    documentation: str = ""
    insert_text: str | None = None
    rank: int = 5


# ---- reading the text before the cursor ---------------------------------------------------------------------------

def code_start(prefix: str) -> int | None:
    """Where the code of a line's text begins, or None when the end of the text is inside a string or a comment.

    A string is code again inside the braces of an f-string.

    Args:
        prefix: The text of the line before the cursor.

    Returns:
        The index the code before the cursor starts at (0, or just after an f-string's `{`), or None.
    """
    i, n = 0, len(prefix)
    quote, fstring, depth, start = "", False, 0, 0
    while i < n:
        c = prefix[i]
        if quote:
            if c == "\\" and not (fstring and depth):
                i += 2
                continue
            if fstring and c == "{" and prefix[i + 1:i + 2] == "{" and not depth:
                i += 2
                continue
            if fstring and c == "{" and not depth:
                depth, start = 1, i + 1
            elif fstring and depth and c == "{":
                depth += 1
            elif fstring and depth and c == "}":
                depth -= 1
            elif c == quote and not depth:
                quote, fstring, start = "", False, 0
        elif c == "#":
            return None
        elif c in "\"'":
            quote = c
            fstring = i > 0 and prefix[i - 1] in "fF" and (i < 2 or not prefix[i - 2].isalnum())
        i += 1
    if quote:
        return start if fstring and depth else None
    return 0


def receiver_start(code: str, dot: int) -> int:
    """Where the value in front of a dot begins: a name, a call, an index, a string, and the chain of members before it.

    Args:
        code: The text of the code before the cursor.
        dot: The index of the dot.

    Returns:
        The index the value starts at (equal to `dot` when there is none).
    """
    i = dot
    while i > 0:
        c = code[i - 1]
        if c == "?":
            i -= 1
        elif c in ")]":
            depth, j = 0, i - 1
            while j >= 0:
                if code[j] in ")]":
                    depth += 1
                elif code[j] in "([":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            if j < 0:
                return dot
            i = j
            if i > 0 and (code[i - 1].isalnum() or code[i - 1] == "_"):
                continue
            if code[i] == "[" and i > 0 and code[i - 1] in ")]\"'":
                continue
            return i
        elif c in "\"'":
            j = code.rfind(c, 0, i - 1)
            return j if j >= 0 else dot
        elif c.isalnum() or c == "_":
            j = i
            while j > 0 and (code[j - 1].isalnum() or code[j - 1] == "_"):
                j -= 1
            i = j
            if i > 0 and code[i - 1] == ".":
                i -= 1
                continue
            return i
        else:
            return i
    return i


def indent_of(text: str) -> int:
    """The number of leading spaces of a line."""
    return len(text) - len(text.lstrip(" "))


def in_type_position(code: str) -> bool:
    """Whether a type is expected at the end of the code: after `->`, or after the `:` of a parameter or an annotation."""
    if re.search(rf"->\s*{TYPE_CHARS}$", code):
        return True
    if re.search(rf"\bfn\b[^)=]*[(,]\s*{IDENT}\s*:\s*{TYPE_CHARS}$", code):
        return True
    m = re.match(rf"\s*({IDENT})\s*:\s*{TYPE_CHARS}$", code)
    return bool(m) and m.group(1) not in KEYWORDS


# ---- describing things ------------------------------------------------------------------------------------------

def method_detail(m: ast.FnDef) -> str:
    """A method's signature without `self`: `(dx: float, dy: float = 0.0) -> Point`."""
    params = [f"{p.name}: {type_text(p.type)}" + (f" = {expr_text(p.default)}" if p.default is not None else "")
              for p in m.params]
    return f"({', '.join(params)})" + (f" -> {type_text(m.ret)}" if m.ret else "")


def entity_item(label: str, ent: Entity | None, rank: int = 5) -> Completion:
    """The candidate for a name that denotes an entity: its kind, a signature or type, and its description.

    Args:
        label: The name to offer.
        ent: What it denotes (None when unknown: it is offered as a plain variable).
        rank: The ordering rank.

    Returns:
        The candidate.
    """
    ent = deref(ent)
    if ent is None:
        return Completion(label, "variable", rank=rank)
    node = ent.node
    doc = doc_of(node) or ""
    if ent.kind == "function" and isinstance(node, ast.FnDef):
        return Completion(label, "function", signature_text(node), doc, rank=rank)
    if ent.kind == "method" and isinstance(node, ast.FnDef):
        return Completion(label, "method", method_detail(node), doc, rank=rank)
    if ent.kind == "field" and isinstance(node, ast.Field):
        default = f" = {expr_text(node.default)}" if node.default is not None else ""
        return Completion(label, "field", type_text(node.type) + default, doc, rank=rank)
    if ent.kind == "variant" and isinstance(node, ast.Variant):
        data = f"({', '.join(type_text(t) for t in node.fields)})" if node.fields else ""
        return Completion(label, "variant", data, doc, rank=rank)
    if ent.kind in ("struct", "enum"):
        return Completion(label, ent.kind, ent.kind, doc, rank=rank)
    if ent.kind == "module":
        return Completion(label, "module", rank=rank)
    return Completion(label, "variable", rank=rank)


def builtin_item(b: Builtin, kind: str, rank: int, receiver=None, owner: str | None = None) -> Completion:
    """The candidate for a built-in function or method."""
    params, ret = describe(b, receiver)
    detail = f"({', '.join(params)})" + (f" -> {ret}" if ret else "")
    return Completion(b.name, kind, detail, f"{b.doc}", rank=rank)


# ---- what is in scope ---------------------------------------------------------------------------------------------

def visible_locals(res: Resolution, line: int, indent: int) -> list[tuple[Entity, str]]:
    """The variables and parameters usable at a position, found by indentation.

    A variable belongs to the block where it was first assigned, and blocks are indented, so a declaration is visible when
    it is above the cursor and no line between them leaves its block. This needs nothing from the cursor's own line, which is
    usually unfinished.

    Args:
        res: The resolution of the file.
        line: 1-based line of the cursor.
        indent: The indentation of the cursor's line.

    Returns:
        Each visible variable's entity and the description of its type, the innermost declaration of a name winning.
    """
    lines = res.index.lines
    code_lines = {i + 1: indent_of(t) for i, t in enumerate(lines) if t.strip() and not t.lstrip().startswith("#")}
    in_def = in_function(res, line, indent)
    found: dict[str, tuple[Entity, str]] = {}
    for r in res.refs:
        ent = r.entity
        if not r.decl or ent is None or ent.kind not in ("variable", "parameter") or r.line >= line:
            continue
        if ent.kind == "parameter" and ent.owner is None:
            continue  # a closure's parameter: only its own body sees it
        if ent.kind == "parameter":
            floor = code_lines.get(ent.owner.line, 0) + 1
        elif ent.type_key is not None:  # a loop or pattern variable: visible in the block under its line
            floor = code_lines.get(r.line, 0) + 1
        else:
            floor = code_lines.get(r.line, 0)
            if floor == 0 and in_def:
                continue  # a variable of the top level is not seen from a function
        if indent < floor or any(code_lines[n] < floor for n in range(r.line + 1, line) if n in code_lines):
            continue
        found[ent.name] = (ent, variable_type(ent, r, res) or "")
    return list(found.values())


def in_function(res: Resolution, line: int, indent: int) -> bool:
    """Whether the cursor is inside a function, struct or enum definition (the top level's variables are not visible there)."""
    lines = res.index.lines
    top = next((n for n in range(min(line, len(lines) + 1) - 1, 0, -1)
                if lines[n - 1].strip() and not lines[n - 1].startswith((" ", "#"))), None)
    return indent > 0 and top is not None and DEF_HEADER.match(lines[top - 1]) is not None


def enclosing_self(res: Resolution, line: int) -> str | None:
    """The name of the struct or enum when the cursor is inside one of its methods that take `self`."""
    lines = res.index.lines
    cursor_indent = indent_of(lines[line - 1]) if 0 < line <= len(lines) else 0
    method = None
    for n in range(min(line, len(lines)) - 1, 0, -1):
        text = lines[n - 1]
        if text.strip() and indent_of(text) < cursor_indent and re.match(r"\s*fn\b", text):
            method = n
            break
    if method is None or not re.search(r"\(\s*self\b", lines[method - 1]):
        return None
    for n in range(method - 1, 0, -1):
        m = re.match(rf"(?:struct|enum)\s+({IDENT})", lines[n - 1])
        if m:
            return m.group(1)
    return None


def file_names(fx: FileIndex, types_only: bool, with_variables: bool = True) -> list[Completion]:
    """The names a file binds at its top level: its definitions, the names it imports and the modules it imports.

    Args:
        fx: The file.
        types_only: Whether only structs and enums (and modules, to reach theirs) are wanted.
        with_variables: Whether to include the variables of the top level (a function does not see them).

    Returns:
        The candidates.
    """
    out: list[Completion] = []
    for name in fx.defs:
        ent = fx.def_entity(name)
        if not types_only or (ent is not None and ent.kind in ("struct", "enum")):
            out.append(entity_item(name, ent, 2))
    for local in fx.froms:
        ent = fx.local_import(local)
        if not types_only or (deref(ent) is not None and deref(ent).kind in ("struct", "enum")):
            out.append(entity_item(local, ent, 2))
    for local in fx.aliases:
        out.append(Completion(local, "module", fx.aliases[local].dotted, rank=2))
    if with_variables and not types_only:
        out += [Completion(g, "variable", rank=2) for g in sorted(fx.globals)]
    return out


# ---- members ------------------------------------------------------------------------------------------------------

def module_exports(project: Project, mod: Path, types_only: bool) -> list[Completion]:
    """What a module exposes: its functions, structs and enums, and the names it re-exports (never the private ones)."""
    fx = project.file(mod)
    if fx is None:
        return []
    out = []
    for name in [*fx.defs, *sorted(fx.pubs)]:
        if name.startswith("_"):
            continue
        ent = project.export_entity(mod, name)
        if types_only and (deref(ent) is None or deref(ent).kind not in ("struct", "enum")):
            continue
        out.append(entity_item(name, ent, 1))
    return out


def static_members(project: Project, fx: FileIndex, names: set[str], recv: str, types_only: bool) -> list[Completion] | None:
    """The members of `recv` when it names a module or an enum (`shapes`, `shapes.Shape`, `Shape`), else None.

    Args:
        project: The project.
        fx: The file being edited.
        names: The variables visible at the cursor: one of them hides a module or definition of the same name.
        recv: The text in front of the dot.
        types_only: Whether only types are wanted (in a type annotation).

    Returns:
        The candidates, or None when `recv` is not a module or an enum (so it is a value).
    """
    segs = recv.split(".")
    if not all(re.fullmatch(IDENT, s) for s in segs) or segs[0] in names:
        return None
    cur = deref(fx.module_level(segs[0]))
    for seg in segs[1:]:
        if cur is None:
            return None
        if cur.kind == "module" and cur.module_path:
            cur = deref(project.export_entity(cur.module_path, seg))
        elif cur.kind == "enum":
            cur = project.member(cur, seg)
        else:
            return None
    if cur is None:
        return None
    if cur.kind == "module" and cur.module_path:
        return module_exports(project, cur.module_path, types_only)
    if cur.kind == "enum" and isinstance(cur.node, ast.EnumDef) and not types_only:
        return [entity_item(v.name, project.member(cur, v.name), 1) for v in cur.node.variants]
    return None


def probe(project: Project, path: Path, lines: list[str], line: int, recv: str):
    """The type of a value written in front of a dot, with the type table; None when it cannot be found.

    The line being typed is replaced by the value alone (inside a block header, by a harmless header and the value as
    its first statement) and the checker is asked about it.
    """
    text = lines[line - 1]
    indent = " " * indent_of(text)
    word = text.strip().split(" ", 1)[0].rstrip(":(")
    new = list(lines)
    if word in BLOCK_HEADERS:
        nxt = next((t for t in lines[line:] if t.strip()), "")
        body = " " * indent_of(nxt) if indent_of(nxt) > len(indent) else indent + "    "
        header = {"for": "for _zc in [0]:", "match": "if true:"}.get(word, f"{word} true:")
        new[line - 1:line] = [indent + header, body + recv]
        at = line + 1
    else:
        new[line - 1] = indent + recv
        at = line
    probe_project = Project(project.root, {**project.overrides, path.resolve(): "\n".join(new) + "\n"},
                            project.packages_dir, project.trusted)
    return probe_project.probe_type(path, at)


def value_members(project: Project, path: Path, lines: list[str], line: int, recv: str) -> list[Completion]:
    """The fields and methods of a value, from its type: a struct's or enum's own, or those of a built-in type."""
    found = probe(project, path, lines, line, recv)
    if found is None:
        return []
    t, types = found
    kind = receiver_kind(t)
    if kind is not None:
        return [builtin_item(b, "method", 1, t) for b in METHODS[kind].values()]
    owner = types.owner_of(t)
    fx = project.file(owner[0]) if owner else None
    ent = fx.def_entity(owner[1]) if fx and owner else None
    if ent is None or not isinstance(ent.node, (ast.StructDef, ast.EnumDef)):
        return []
    names = [f.name for f in ent.node.fields] if isinstance(ent.node, ast.StructDef) else []
    names += [m.name for m in ent.node.methods]
    return [entity_item(n, project.member(ent, n), 0 if i < len(ent.node.fields if isinstance(ent.node, ast.StructDef) else []) else 1)
            for i, n in enumerate(names)]


# ---- imports ------------------------------------------------------------------------------------------------------

def module_files(project: Project, path: Path) -> dict[str, Path]:
    """The modules a file can import, by the dotted name it would write: the standard library and the workspace's files."""
    found = {name: stdlib_module(name.split(".")) for name in stdlib_modules()}
    for other in project.workspace_files():
        if other == path.resolve():
            continue
        try:
            rel = other.relative_to(path.resolve().parent).with_suffix("")
        except ValueError:
            continue
        dotted = ".".join(rel.parts)
        if all(re.fullmatch(IDENT, p) for p in rel.parts) and project.module_path(path, dotted) == other:
            found[dotted] = other
    return {name: f for name, f in found.items() if f is not None}


def import_items(project: Project, path: Path, code: str) -> list[Completion] | None:
    """The candidates on an `import` or `from ... import` line, or None when the line is not one."""
    m = re.match(r"\s*(?:pub\s+)?from\s+([\w.]*)$", code) or re.match(r"\s*import\s+([\w.]*)$", code)
    if m:
        typed = m.group(1)
        parent = typed.rsplit(".", 1)[0] + "." if "." in typed else ""
        out, seen = [], set()
        for name in module_files(project, path):
            if name.startswith(parent):
                label = name[len(parent):].split(".")[0]
                if label not in seen:
                    seen.add(label)
                    out.append(Completion(label, "module", name if name == parent + label else "", rank=1))
        return out
    m = re.match(r"\s*(?:pub\s+)?from\s+([\w.]+)\s+import\s+(.*?)\w*$", code)
    if m:
        target = project.module_path(path, m.group(1))
        done = {n.strip().split(" ")[0] for n in m.group(2).split(",")}
        return [c for c in module_exports(project, target, False) if c.label not in done] if target else []
    return None


# ---- calls --------------------------------------------------------------------------------------------------------

def named_arguments(project: Project, path: Path, source: str, line: int, col: int, call: Call) -> list[Completion]:
    """The parameters (or struct fields) that the call being typed has not given yet, as `name=`."""
    ent = callee_entity(path, source, line, col, project, call)
    info = signature_of(ent) if ent is not None else None
    ent = deref(ent)
    if info is None or ent is None or ent.kind not in ("function", "method", "struct"):
        return []
    node = ent.node
    if isinstance(node, ast.StructDef):
        items = [(f.name, type_text(f.type) + (f" = {expr_text(f.default)}" if f.default is not None else ""), f.doc or "", "field")
                 for f in node.fields]
    elif isinstance(node, ast.FnDef):
        items = [(p.name, type_text(p.type) + (f" = {expr_text(p.default)}" if p.default is not None else "") if p.type else "", "", "parameter")
                 for p in node.params]
    else:
        return []
    given = call.index if not call.used else 0  # positional arguments fill the first parameters
    return [Completion(f"{n}=", kind, detail, doc, rank=0)
            for i, (n, detail, doc, kind) in enumerate(items) if n not in call.used and (call.used or i >= given)]


# ---- the entry point ----------------------------------------------------------------------------------------------

def complete(project: Project, path: Path, line: int, col: int) -> list[Completion]:
    """Suggest what can be typed at a position.

    Works on incomplete code: the document needs no valid statement at the cursor, and a syntax error elsewhere in the
    file only hides what the broken statement would have defined.

    Args:
        project: The project; its buffer for `path` is the document's current text.
        path: The file.
        line: 1-based line of the cursor.
        col: 1-based column of the cursor (the text before it is what counts).

    Returns:
        The candidates, best first; the editor filters them by what has been typed.
    """
    path = path.resolve()
    source = project.text_of(path) or ""
    lines = source.split("\n")
    text = lines[line - 1] if 0 < line <= len(lines) else ""
    prefix = text[:col - 1]
    start = code_start(prefix)
    if start is None:
        return []
    code = prefix[start:]
    fx = project.file(path)
    if fx is None:
        return []
    imports = import_items(project, path, code)
    if imports is not None:
        return imports
    partial = re.search(r"\w*$", code).group(0)
    head = code[:len(code) - len(partial)]
    types_only = in_type_position(code)
    if head.endswith("."):
        dot = len(head) - 1
        recv = code[receiver_start(code, dot):dot]
        if not recv or recv[-1].isdigit() and recv.replace(".", "").isdigit():
            return []
        res = project.resolution(path)
        names = {e.name for e, _ in visible_locals(res, line, indent_of(text))} if res else set()
        if enclosing_self(res, line) if res else None:
            names.add("self")
        found = static_members(project, fx, names, recv, types_only)
        if found is not None:
            return found
        return [] if types_only else value_members(project, path, lines, line, recv)
    out: list[Completion] = []
    call = call_at(source, line, col)
    if call is not None and call.innermost == "(" and call.at_start and not types_only:
        out += named_arguments(project, path, source, line, col, call)
    if types_only:
        out += [Completion(n, "type", doc, rank=1) for n, doc in TYPE_NAMES.items()]
        out += file_names(fx, True)
        return unique(out)
    res = project.resolution(path)
    if res is not None:
        out += [Completion(e.name, "parameter" if e.kind == "parameter" else "variable", ty, rank=1)
                for e, ty in visible_locals(res, line, indent_of(text))]
        owner = enclosing_self(res, line)
        if owner:
            out.append(Completion("self", "variable", owner, rank=1))
    out += file_names(fx, False, not (res is not None and in_function(res, line, indent_of(text))))
    out += [builtin_item(b, "function", 3) for b in sorted(FUNCTIONS.values(), key=lambda b: b.name)]
    out += [Completion(k, "keyword", rank=4) for k in sorted(KEYWORDS)]
    return unique(out)


def unique(items: list[Completion]) -> list[Completion]:
    """Drop the candidates whose label an earlier (nearer) one already uses, and order by rank."""
    seen: set[str] = set()
    out = []
    for c in items:
        if c.label not in seen:
            seen.add(c.label)
            out.append(c)
    return sorted(out, key=lambda c: c.rank)
