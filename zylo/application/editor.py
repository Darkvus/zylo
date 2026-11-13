"""Use cases for editors: symbols, hover, go to definition, rename and signature help.

These are independent of any editor protocol; `interface/lsp.py` turns them into Language Server Protocol messages.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from zylo.application.compile import project_context
from zylo.domain import ast
from zylo.domain.builtins import FUNCTIONS, METHODS, Builtin, describe, receiver_kind
from zylo.domain.errors import PackageError, ZyloError
from zylo.domain.types import Type
from zylo.packaging.manifest import find_manifest
from zylo.semantics.navigation import Entity, Project, Ref, RenameError, Resolution, deref, doc_of, show_type
from zylo.syntax.formatter import expr_text, tparams_text, type_text
from zylo.syntax.lexer import Token, tokenize
from zylo.syntax.parser import parse_collect


@dataclass
class Symbol:
    """A named thing defined in a document.

    Attributes:
        name: Its name.
        kind: "function", "struct", "enum", "method", "field", "variant" or "variable".
        line: 1-based line where it starts.
        col: 1-based column of its name.
        end_line: 1-based last line it occupies.
        detail: A short description, such as a function's signature.
        children: Nested symbols (a struct's fields and methods).
    """

    name: str
    kind: str
    line: int
    col: int
    end_line: int
    detail: str = ""
    children: list["Symbol"] = field(default_factory=list)


def signature_text(f: ast.FnDef) -> str:
    """A function as written in a signature: `fn name(a: int, b: str = "x") -> bool` (methods list `self` first)."""
    params = (["self"] if f.has_self else []) + [
        f"{p.name}: {type_text(p.type)}" + (f" = {expr_text(p.default)}" if p.default is not None else "")
        for p in f.params
    ]
    return f"fn {f.name}{tparams_text(f.tparams)}({', '.join(params)})" + (f" -> {type_text(f.ret)}" if f.ret else "")


def _name_col(line_text: str, name: str, fallback: int) -> int:
    """1-based column of `name` as a whole word in a source line."""
    m = re.search(rf"\b{re.escape(name)}\b", line_text)
    return m.start() + 1 if m else fallback


def document_symbols(source: str) -> list[Symbol]:
    """List the functions, structs, enums and top-level variables of a document.

    The document may have syntax errors: whatever parsed is listed.

    Args:
        source: The document's text.

    Returns:
        The symbols in source order; a struct holds its fields and methods as children.
    """
    program, _ = parse_collect(source)
    lines = source.splitlines()
    total = max(len(lines), 1)
    items = [s for s in program.body if isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef)) or
             (isinstance(s, ast.Assign) and isinstance(s.target, ast.Name))]
    starts = [s.line for s in items]

    def end_of(i: int) -> int:
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else total
        while end > starts[i] and not lines[end - 1].strip():
            end -= 1
        return end

    def at(line: int) -> str:
        return lines[line - 1] if 0 < line <= len(lines) else ""

    symbols: list[Symbol] = []
    for i, s in enumerate(items):
        if isinstance(s, ast.StructDef):
            sym = Symbol(s.name, "struct", s.line, _name_col(at(s.line), s.name, s.col), end_of(i))
            sym.children += [Symbol(f.name, "field", f.line, _name_col(at(f.line), f.name, f.col), f.line,
                                    type_text(f.type) + (f" = {expr_text(f.default)}" if f.default is not None else ""))
                             for f in s.fields]
            for j, m in enumerate(s.methods):
                nxt = s.methods[j + 1].line - 1 if j + 1 < len(s.methods) else sym.end_line
                sym.children.append(Symbol(m.name, "method", m.line, _name_col(at(m.line), m.name, m.col), max(nxt, m.line),
                                           signature_text(m)))
        elif isinstance(s, ast.EnumDef):
            sym = Symbol(s.name, "enum", s.line, _name_col(at(s.line), s.name, s.col), end_of(i))
            sym.children += [Symbol(v.name, "variant", v.line, _name_col(at(v.line), v.name, v.col), v.line,
                                    f"({', '.join(type_text(t) for t in v.fields)})" if v.fields else "")
                             for v in s.variants]
            for j, m in enumerate(s.methods):
                nxt = s.methods[j + 1].line - 1 if j + 1 < len(s.methods) else sym.end_line
                sym.children.append(Symbol(m.name, "method", m.line, _name_col(at(m.line), m.name, m.col), max(nxt, m.line),
                                           signature_text(m)))
        elif isinstance(s, ast.FnDef):
            kind_line = at(s.line)
            sym = Symbol(s.name, "function", s.line, _name_col(kind_line, s.name, s.col), end_of(i), signature_text(s))
        else:
            sym = Symbol(s.target.id, "variable", s.line, s.col, s.line)
        symbols.append(sym)
    return symbols


# ---- navigation ---------------------------------------------------------------------------------------------

@dataclass
class Place:
    """A stretch of text in a file.

    Attributes:
        path: The file.
        line: 1-based line.
        col: 1-based column where it starts.
        length: Its length in characters (0 for a point).
    """

    path: Path
    line: int
    col: int
    length: int


@dataclass
class Edit:
    """A replacement of the text at a place.

    Attributes:
        place: What to replace.
        new_text: What to put there.
    """

    place: Place
    new_text: str


@dataclass
class Hover:
    """What to show for a name under the cursor.

    Attributes:
        markdown: The text, in Markdown.
        place: The name it is about.
    """

    markdown: str
    place: Place


@dataclass
class Signature:
    """The signature of a callable, for the call being typed.

    Attributes:
        label: The whole signature as text.
        parameters: `(start, end)` of each parameter inside `label`.
        documentation: The callable's description, or None.
        active: The index of the parameter being typed, or None when it is not one of them.
    """

    label: str
    parameters: list[tuple[int, int]]
    documentation: str | None
    active: int | None


def open_project(path: Path, overrides: dict[Path, str] | None = None) -> Project:
    """The project a file belongs to: its manifest's directory (else the file's own), its environment and trust list.

    Args:
        path: A file of the project.
        overrides: Text of the files that differ from disk (an editor's unsaved buffers).

    Returns:
        The project (a broken manifest or environment only means the project has no packages).
    """
    path = path.resolve()
    manifest = find_manifest(path.parent)
    root = manifest.parent if manifest else path.parent
    try:
        mf, active = project_context(path)
    except (PackageError, ZyloError):
        mf, active = None, None
    return Project(root, overrides, active.packages_dir if active else None, mf.trusted_packages if mf else [])


def _place(e: Entity) -> Place | None:
    """Where an entity's name is declared."""
    return Place(e.path, e.line, e.col, len(e.name)) if e.path is not None and e.line else None


def _fn_text(f: ast.FnDef, owner: str | None = None) -> str:
    sig = signature_text(f)
    return f"fn {owner}.{sig[3:]}" if owner else sig


def _doc_block(node: ast.Node | None) -> str:
    doc = doc_of(node)
    return f"\n\n{doc}" if doc else ""


def variable_type(ent: Entity, ref: Ref, res: Resolution) -> str | None:
    """The type of a variable: where it is used (it may be narrowed there), else where it is declared."""
    t = None
    if res.types is not None:
        path = res.index.path
        if not ref.decl or ent.type_key is None:
            t = res.types.at(path, ref.line, ref.col)
        if t is None and ent.path == path:
            t = res.types.binder(path, ent.type_key) or res.types.at(path, ent.line, ent.col)
    if t is not None:
        return show_type(t)
    if isinstance(ent.node, ast.Param) and ent.node.type is not None:
        return type_text(ent.node.type)
    return None


def _param_note(res: Resolution, templates: list, shown: str | None, always: bool = False) -> str:
    """What hover adds about the type parameters in a shown type: their bounds and what the program instantiated them with.

    Args:
        res: The resolution of the file.
        templates: The generic definitions that declare the parameters in question, innermost first.
        shown: The text of a type; only parameters it mentions are described (None or `always`: all of them).
        always: Describe every parameter of `templates`.

    Returns:
        Markdown lines (empty when no type parameter is involved).
    """
    words = set(re.findall(r"\w+", shown or ""))
    lines: list[str] = []
    for tpl in templates:
        for name, bounds in tpl.params:
            if not (always or name in words):
                continue
            head = f"`{name}: {' + '.join(bounds)}`" if bounds else f"`{name}`"
            seen = tpl.insts.get(name)
            used = f"instantiated with {', '.join(f'`{t}`' for t in seen)}" if seen else "not instantiated anywhere in this program"
            lines.append(f"{head}, a type parameter of `{tpl.name}`: {used}")
    return "\n\n" + "\n\n".join(lines) if lines else ""


def _template_note(ent: Entity, res: Resolution, node: ast.Node | None, shown: str | None = None) -> str:
    """The type-parameter note for a definition: its own parameters, and those of the struct or enum around a method."""
    if res.types is None or node is None or ent is None:
        return ""
    found = []
    for n in (node, getattr(ent.owner, "node", None) if ent.owner is not None and ent.owner.node is not node else None):
        tpl = res.types.template_at(ent.path or res.index.path, n.line, n.col) if n is not None else None
        if tpl is not None:
            found.append(tpl)
    return _param_note(res, found, shown, always=shown is None)


def _describe(project: Project, ent: Entity, ref: Ref, res: Resolution) -> str:
    """The Markdown shown when hovering a name."""
    ent = deref(ent) or ent
    node = ent.node
    owner = ent.owner.name if ent.owner is not None else None
    if ent.kind in ("variable", "parameter", "self"):
        ty = variable_type(ent, ref, res) if ent.kind != "self" else None
        if ent.kind == "self":
            ty = variable_type(ent, ref, res) or (ent.target.name if ent.target else None)
        label = {"variable": "variable", "parameter": "parameter", "self": "self"}[ent.kind]
        note = _param_note(res, res.types.enclosing(res.index.path, ref.line), ty) if res.types is not None else ""
        return f"```zylo\n({label}) {ent.name}{': ' + ty if ty else ''}\n```{note}"
    if ent.kind == "function" and isinstance(node, ast.FnDef):
        return f"```zylo\n{signature_text(node)}\n```{_doc_block(node)}{_template_note(ent, res, node)}"
    if ent.kind == "method" and isinstance(node, ast.FnDef):
        return f"```zylo\n{_fn_text(node, owner)}\n```{_doc_block(node)}{_template_note(ent, res, node)}"
    if ent.kind == "struct" and isinstance(node, ast.StructDef):
        fields = "".join(f"\n    {f.name}: {type_text(f.type)}" + (f" = {expr_text(f.default)}" if f.default is not None else "")
                         for f in node.fields)
        return f"```zylo\nstruct {ent.name}{tparams_text(node.tparams)}{fields}\n```{_doc_block(node)}{_template_note(ent, res, node)}"
    if ent.kind == "enum" and isinstance(node, ast.EnumDef):
        variants = "".join(f"\n    {v.name}" + (f"({', '.join(type_text(t) for t in v.fields)})" if v.fields else "")
                           for v in node.variants)
        return f"```zylo\nenum {ent.name}{tparams_text(node.tparams)}{variants}\n```{_doc_block(node)}{_template_note(ent, res, node)}"
    if ent.kind == "variant" and isinstance(node, ast.Variant):
        data = f"({', '.join(type_text(t) for t in node.fields)})" if node.fields else ""
        return f"```zylo\n{owner}.{ent.name}{data}\n```{_doc_block(node)}"
    if ent.kind == "field" and isinstance(node, ast.Field):
        default = f" = {expr_text(node.default)}" if node.default is not None else ""
        note = _template_note(ent.owner, res, ent.owner.node, type_text(node.type)) if ent.owner is not None else ""
        return f"```zylo\n(field) {owner}.{ent.name}: {type_text(node.type)}{default}\n```{_doc_block(node)}{note}"
    if ent.kind == "module":
        where = f"\n\n{ent.module_path}" if ent.module_path else ""
        return f"```zylo\nmodule {ent.name}\n```{where}"
    if ent.kind == "builtin":
        b = FUNCTIONS.get(ent.name)
        return f"```zylo\n{builtin_text(b)}\n```\n\n{b.doc}" if b else f"```zylo\n(built-in) {ent.name}\n```"
    if ent.kind == "builtin_method":
        b = METHODS[receiver_name(ent)][ent.name]
        return f"```zylo\n{builtin_text(b, ent.receiver, str(show_type(ent.receiver)))}\n```\n\n{b.doc}"
    return f"```zylo\n{ent.name}\n```"


def receiver_name(ent: Entity) -> str:
    """The key of `METHODS` for a built-in method's receiver."""
    kind = receiver_kind(ent.receiver)
    assert kind is not None
    return kind


def builtin_text(b: Builtin, receiver: Type | None = None, owner: str | None = None) -> str:
    """A built-in function or method as a signature: `fn len(x: str | list | dict | set) -> int`.

    Args:
        b: The built-in.
        receiver: For a method, the type of the value it is called on (fills in the element types).
        owner: For a method, the receiver's type as shown to the user (`list[int]`).

    Returns:
        The signature text.
    """
    params, ret = describe(b, receiver)
    head = f"(method) {owner}.{b.name}" if owner else f"fn {b.name}"
    return f"{head}({', '.join(params)})" + (f" -> {ret}" if ret else "")


def hover(project: Project, path: Path, line: int, col: int) -> Hover | None:
    """Describe the name at a position: the type of a variable, the signature and description of a function, a type...

    Args:
        project: The project.
        path: The file.
        line: 1-based line.
        col: 1-based column.

    Returns:
        The description, or None when there is no known name there.
    """
    hit = project.ref_at(path, line, col, touching=False)
    if hit is None or hit[0].entity is None:
        return None
    ref, res = hit
    return Hover(_describe(project, ref.entity, ref, res), Place(path, ref.line, ref.col, ref.length))


def definition(project: Project, path: Path, line: int, col: int) -> list[Place]:
    """Where the name at a position is defined (through aliases and re-exports, into other files).

    Args:
        project: The project.
        path: The file.
        line: 1-based line.
        col: 1-based column.

    Returns:
        The place of the definition (empty for a built-in or an unknown name).
    """
    hit = project.ref_at(path, line, col)
    if hit is None or hit[0].entity is None:
        return []
    ent = deref(hit[0].entity)
    if ent is None or ent.kind in ("builtin", "builtin_method"):
        return []
    if ent.kind == "module":
        return [Place(ent.module_path, 1, 1, 0)] if ent.module_path else []
    if ent.kind == "self" and ent.target is not None:
        ent = ent.target
    place = _place(ent)
    return [place] if place else []


def prepare_rename(project: Project, path: Path, line: int, col: int) -> Place | None:
    """The name that a rename at a position would change.

    Args:
        project: The project.
        path: The file.
        line: 1-based line.
        col: 1-based column.

    Returns:
        The place of the name under the cursor, or None when there is no name.

    Raises:
        RenameError: When the name is there but cannot be renamed (a built-in, a module, a package's definition).
    """
    hit = project.ref_at(path, line, col)
    if hit is None or hit[0].entity is None:
        return None
    ref = hit[0]
    project.check_renameable(ref.entity)
    return Place(path, ref.line, ref.col, ref.length)


def rename(project: Project, path: Path, line: int, col: int, new_name: str) -> list[Edit]:
    """Rename the thing under a position everywhere in the workspace.

    Locals and parameters are renamed in their scope only; top-level items, fields, variants, methods and named
    arguments in every file that uses them. Nothing is changed if the new name is invalid or would collide.

    Args:
        project: The project.
        path: The file.
        line: 1-based line.
        col: 1-based column.
        new_name: The new name.

    Returns:
        The edits to apply (empty when the name does not change).

    Raises:
        RenameError: With the reason when the rename cannot be done safely.
    """
    hit = project.ref_at(path, line, col)
    if hit is None or hit[0].entity is None:
        raise RenameError("there is no name to rename here")
    refs = project.plan_rename(hit[0].entity, new_name)
    return [Edit(Place(p, r.line, r.col, r.length), new_name) for p, r in refs]


# ---- signature help ---------------------------------------------------------------------------------------------

_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_DANGLING = {"=", "+", "-", "*", "/", "%", "<", ">", "<=", ">=", "==", "!=", "->", "=>", "|"}


@dataclass
class Call:
    """A call being typed.

    Attributes:
        callee: The token of the callee's name.
        index: How many commas precede the cursor inside the call: the position of the argument being written.
        named: The parameter name when that argument is written `name=...`, otherwise None.
        completion: The text that would finish the call: filler after a dangling operator and the missing brackets.
        used: The parameter names already given as `name=value` arguments.
        at_start: True when the cursor is at the start of an argument (or in the first word of it).
        innermost: The innermost bracket open at the cursor: "(" when the cursor is directly in the call's arguments.
    """

    callee: Token
    index: int
    named: str | None
    completion: str
    used: list[str] = field(default_factory=list)
    at_start: bool = False
    innermost: str = "("


def call_at(source: str, line: int, col: int) -> Call | None:
    """Find the call being typed at a position.

    Args:
        source: The document's text.
        line: 1-based line of the cursor.
        col: 1-based column of the cursor.

    Returns:
        The call, or None when the cursor is not inside the parentheses of a call.
    """
    lines = source.splitlines()
    if not 0 < line <= len(lines) + 1:
        return None
    prefix = "\n".join([*lines[:line - 1], (lines[line - 1] if line <= len(lines) else "")[:col - 1]])
    try:
        toks = [t for t in tokenize(prefix) if t.kind not in ("EOF", "INDENT", "DEDENT")]
    except ZyloError:
        return None
    while toks and toks[-1].kind == "NEWLINE":  # the one the lexer adds to close an open bracket
        toks.pop()
    depth, i, open_at = 0, len(toks) - 1, None
    while i >= 0 and toks[i].kind != "NEWLINE":  # a call does not reach back past the start of its statement
        t = toks[i]
        if t.kind == "OP" and t.value in ")]}":
            depth += 1
        elif t.kind == "OP" and t.value in "([{":
            if depth > 0:
                depth -= 1
            elif t.value == "(" and i > 0 and toks[i - 1].kind == "IDENT" \
                    and not (i > 1 and toks[i - 2].kind == "KW" and toks[i - 2].value == "fn"):
                open_at = i
                break
        i -= 1
    if open_at is None:
        return None
    nested, index, start, used = 0, 0, open_at + 1, []
    for k in range(open_at + 1, len(toks)):
        t = toks[k]
        if t.kind == "NEWLINE":
            continue
        if t.kind == "OP" and t.value in "([{":
            nested += 1
        elif t.kind == "OP" and t.value in ")]}":
            nested -= 1
        elif t.kind == "OP" and t.value == "," and nested == 0:
            if toks[start].kind == "IDENT" and toks[start + 1].kind == "OP" and toks[start + 1].value == "=":
                used.append(toks[start].value)
            index, start = index + 1, k + 1
    named = None
    if start + 1 < len(toks) and toks[start].kind == "IDENT" and toks[start + 1].kind == "OP" and toks[start + 1].value == "=":
        named = toks[start].value
    stack: list[str] = []
    for t in toks:
        if t.kind == "NEWLINE":
            stack.clear()
        elif t.kind == "OP" and t.value in _BRACKETS:
            stack.append(t.value)
        elif t.kind == "OP" and t.value in ")]}" and stack:
            stack.pop()
    last = toks[-1]
    filler = "x" if last.kind == "OP" and last.value == "." else " 0" if last.kind == "OP" and last.value in _DANGLING else ""
    at_start = len(toks) - start <= 1 and (len(toks) == start or toks[start].kind == "IDENT")
    return Call(toks[open_at - 1], index, named, filler + "".join(_BRACKETS[b] for b in reversed(stack)), used,
                at_start, stack[-1] if stack else "")


def _label(head: str, parts: list[str], tail: str = "") -> tuple[str, list[tuple[int, int]]]:
    """Assemble `head(parts...)tail` and the offset of each part inside it."""
    spans, pos = [], len(head) + 1
    for p in parts:
        spans.append((pos, pos + len(p)))
        pos += len(p) + 2
    return f"{head}({', '.join(parts)}){tail}", spans


def builtin_signature(ent: Entity) -> tuple[str, list[tuple[int, int]], list[str], str | None] | None:
    """The label, parameter spans, parameter names and description of a built-in function or method."""
    if ent.kind == "builtin":
        b, receiver, owner = FUNCTIONS.get(ent.name), None, None
    else:
        b, receiver, owner = METHODS[receiver_name(ent)][ent.name], ent.receiver, show_type(ent.receiver)
    if b is None:
        return None
    params, ret = describe(b, receiver)
    head = f"{owner}.{b.name}" if owner else f"fn {b.name}"
    label, spans = _label(head, params, f" -> {ret}" if ret else "")
    return label, spans, [p.split(":")[0].strip() for p in params], b.doc


def signature_of(ent: Entity) -> tuple[str, list[tuple[int, int]], list[str], str | None] | None:
    """The label, parameter spans, parameter names and description of something that can be called."""
    ent = deref(ent)
    if ent is not None and ent.kind in ("builtin", "builtin_method"):
        return builtin_signature(ent)
    if ent is None or ent.node is None:
        return None
    node = ent.node
    if ent.kind in ("function", "method") and isinstance(node, ast.FnDef):
        parts = [f"{p.name}: {type_text(p.type)}" + (f" = {expr_text(p.default)}" if p.default is not None else "")
                 for p in node.params if p.type is not None]
        head = f"fn {ent.owner.name}.{ent.name}" if ent.owner is not None else f"fn {ent.name}{tparams_text(node.tparams)}"
        label, spans = _label(head, parts, f" -> {type_text(node.ret)}" if node.ret else "")
        return label, spans, [p.name for p in node.params], doc_of(node)
    if ent.kind == "struct" and isinstance(node, ast.StructDef):
        parts = [f"{f.name}: {type_text(f.type)}" + (f" = {expr_text(f.default)}" if f.default is not None else "")
                 for f in node.fields]
        label, spans = _label(ent.name + tparams_text(node.tparams), parts)
        return label, spans, [f.name for f in node.fields], doc_of(node)
    if ent.kind == "variant" and isinstance(node, ast.Variant) and node.fields:
        label, spans = _label(f"{ent.owner.name}.{ent.name}" if ent.owner else ent.name, [type_text(t) for t in node.fields])
        return label, spans, [], doc_of(node)
    return None


def callee_entity(path: Path, source: str, line: int, col: int, project: Project, call: Call) -> Entity | None:
    """What the callee of an unfinished call is, completing the text with its missing brackets when needed.

    Args:
        path: The file.
        source: Its current text.
        line: 1-based line of the cursor.
        col: 1-based column of the cursor.
        project: The project, whose buffer for `path` is `source`.
        call: The call being typed, from `call_at`.

    Returns:
        The entity of the callee (a function, method, struct, variant or built-in that can be called), or None.
    """
    callee = call.callee
    hit = project.ref_at(path, callee.line, callee.col)
    ent = hit[0].entity if hit else None
    if ent is None or signature_of(ent) is None:
        lines = source.splitlines() or [""]
        row = min(line, len(lines)) - 1
        fixed = [*lines[:row], lines[row][:col - 1] + call.completion + lines[row][col - 1:], *lines[row + 1:]]
        repaired = Project(project.root, {**project.overrides, path.resolve(): "\n".join(fixed) + "\n"},
                           project.packages_dir, project.trusted)
        hit = repaired.ref_at(path, callee.line, callee.col)
        ent = hit[0].entity if hit else None
    return ent


def signature_help(path: Path, source: str, line: int, col: int, project: Project) -> Signature | None:
    """Describe the call being typed at a position and say which parameter the cursor is in.

    Works while the call is still unfinished: the text is completed with the missing brackets (and a filler value
    after a dangling operator) before the callee is resolved. Built-in functions and methods are described too.

    Args:
        path: The file.
        source: Its current text.
        line: 1-based line of the cursor.
        col: 1-based column of the cursor.
        project: The project, whose buffer for `path` is `source`.

    Returns:
        The signature, or None when the cursor is not in a call of a known function, method, struct or variant.
    """
    call = call_at(source, line, col)
    if call is None:
        return None
    ent = callee_entity(path, source, line, col, project, call)
    info = signature_of(ent) if ent is not None else None
    if info is None:
        return None
    label, spans, names, doc = info
    active: int | None = call.index
    if call.named is not None:
        active = names.index(call.named) if call.named in names else None
    elif names and names[-1].startswith("*"):  # `print(*values)`: every further argument is the last parameter
        active = min(call.index, len(spans) - 1)
    elif call.index >= len(spans):
        active = None
    return Signature(label, spans, doc, active)
