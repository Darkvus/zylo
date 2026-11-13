"""Code actions: quick fixes an editor offers for the code at a position.

- *Add an import* for a name the file uses but does not define: it is looked for in the other files of the workspace
  and in the standard library (a function, struct or enum, or a module written in front of a dot).
- *Add the missing arms* of a `match` on an enum that does not cover every variant.

Each action is a list of text edits, so the editor shows and applies it like any other change.
"""

from dataclasses import dataclass
from pathlib import Path

from zylo.application.completion import indent_of, module_files
from zylo.application.editor import Edit, Place
from zylo.domain import ast
from zylo.domain.types import EnumT
from zylo.semantics.linker import RESERVED_TYPES
from zylo.semantics.navigation import BUILTIN_TYPES, FileIndex, Project, Types, deref
from zylo.syntax.lexer import KEYWORDS


@dataclass
class CodeAction:
    """A quick fix.

    Attributes:
        title: What the editor shows in its menu.
        edits: The changes that make the fix.
    """

    title: str
    edits: list[Edit]


# ---- add an import ------------------------------------------------------------------------------------------------

def import_line(fx: FileIndex) -> int:
    """The line to insert a new import at: right after the last import of the file, else the first line."""
    last = max((s.line for s in fx.program.body if isinstance(s, (ast.Import, ast.FromImport))), default=0)
    return last + 1 if last else 1


def add_from_import(fx: FileIndex, dotted: str, name: str) -> Edit:
    """The edit that makes `name` importable from a module: it joins an existing `from` line of that module, else adds one."""
    path = fx.path
    existing = [b for b in fx.froms.values() if b.dotted == dotted and b.name_tok is not None]
    tok = max((b.alias_tok or b.name_tok for b in existing), key=lambda t: (t.line, t.col), default=None)
    if tok is not None:
        return Edit(Place(path, tok.line, tok.col + len(tok.value), 0), f", {name}")
    line = import_line(fx)
    spaced = "\n" if not any(isinstance(s, (ast.Import, ast.FromImport)) for s in fx.program.body) and fx.lines[:1] != [""] else ""
    return Edit(Place(path, line, 1, 0), f"from {dotted} import {name}\n{spaced}")


def import_fixes(project: Project, path: Path, line: int, col: int) -> list[CodeAction]:
    """Offer to import the name under the cursor when nothing in the file defines it."""
    res = project.resolution(path)
    hit = res.at(line, col) if res else None
    if res is None or hit is None or hit.entity is not None or hit.space != "name" or hit.decl:
        return []
    name, fx = hit.name, res.index
    if name in KEYWORDS or name in BUILTIN_TYPES or name in RESERVED_TYPES or name in fx.top_names:
        return []
    text = fx.lines[hit.line - 1] if hit.line <= len(fx.lines) else ""
    qualifier = text[hit.col - 1 + hit.length:hit.col + hit.length] == "."
    out: list[CodeAction] = []
    for dotted, mod in sorted(module_files(project, path).items(), key=lambda kv: (kv[0].startswith("std."), kv[0])):
        if qualifier and dotted.rsplit(".", 1)[-1] == name:
            out.append(CodeAction(f"Import the module '{dotted}'",
                                  [Edit(Place(path, import_line(fx), 1, 0), f"import {dotted}\n")]))
        elif not qualifier:
            other = project.file(mod)
            if other is not None and not name.startswith("_") and (name in other.defs or name in other.pubs):
                out.append(CodeAction(f"Import '{name}' from '{dotted}'", [add_from_import(fx, dotted, name)]))
    return out


# ---- add the missing match arms -----------------------------------------------------------------------------------

def block_end(lines: list[str], first: int) -> int:
    """The last line of the block that starts at a line: the following lines that are blank or indented deeper."""
    indent, end = indent_of(lines[first - 1]), first
    for n in range(first + 1, len(lines) + 1):
        text = lines[n - 1]
        if not text.strip():
            continue
        if indent_of(text) <= indent:
            break
        end = n
    return end


def enum_qualifier(project: Project, fx: FileIndex, match: ast.Match, owner: tuple[Path, str]) -> str:
    """How the enum is written in this file: as the arms already write it, else by its import, else its bare name."""
    for arm in match.arms:
        if arm.type is not None:
            return arm.type.name
    path, name = owner
    if path == fx.path:
        return name
    for local in fx.froms:
        ent = deref(fx.local_import(local))
        if ent is not None and ent.kind == "enum" and ent.path == path and ent.name == name:
            return local
    for local, b in fx.aliases.items():
        if project.module_path(fx.path, b.dotted) == path:
            return f"{local}.{name}"
    return name


def match_fixes(project: Project, path: Path, line: int) -> list[CodeAction]:
    """Offer the arms a `match` on an enum is missing, when the cursor is in that `match`."""
    fx = project.file(path)
    if fx is None:
        return []
    candidates = []
    for n in ast.walk(fx.program):
        if isinstance(n, ast.Match) and n.arms and n.line <= line <= block_end(fx.lines, n.line):
            candidates.append(n)
    if not candidates:
        return []
    match = max(candidates, key=lambda m: m.line)  # the innermost one
    found = project.link(path)
    mod = found[0].by_path.get(path.resolve()) if found else None
    typed = next((n for n in ast.walk(mod.program) if isinstance(n, ast.Match) and (n.line, n.col) == (match.line, match.col)), None) \
        if mod else None
    if found is None or typed is None or not isinstance(typed.value.ty, EnumT):
        return []
    owner = Types.from_link(found[0]).owner_of(typed.value.ty)
    owner_fx = project.file(owner[0]) if owner else None
    enum = owner_fx.defs.get(owner[1]) if owner_fx and owner else None
    if not isinstance(enum, ast.EnumDef):
        return []
    arms = [a for arm in match.arms for a in (arm, *arm.alts)]
    if any(a.variant == "_" and a.guard is None for a in arms):
        return []
    covered = {a.variant for a in arms if a.guard is None}
    missing = [v for v in enum.variants if v.name not in covered]
    if not missing:
        return []
    qualifier = enum_qualifier(project, fx, match, owner)
    last = match.arms[-1]
    indent = " " * indent_of(fx.lines[last.line - 1])
    body = " " * (indent_of(fx.lines[last.line - 1]) + 4)
    chunks = []
    for v in missing:
        pattern = f"{qualifier}.{v.name}" + (f"({', '.join('_' for _ in v.fields)})" if v.fields else "")
        chunks.append(f"{indent}{pattern}:\n{body}print(\"TODO: handle {qualifier}.{v.name}\")")
    end = block_end(fx.lines, last.line)
    text = "\n".join(chunks)
    if end < len(fx.lines):
        edit = Edit(Place(path, end + 1, 1, 0), text + "\n")
    else:
        edit = Edit(Place(path, end, len(fx.lines[end - 1]) + 1, 0), "\n" + text)
    names = ", ".join(v.name for v in missing)
    title = f"Add the missing match arm for {qualifier}.{names}" if len(missing) == 1 else f"Add the missing match arms ({names})"
    return [CodeAction(title, [edit])]


# ---- the entry point ----------------------------------------------------------------------------------------------

def code_actions(project: Project, path: Path, line: int, col: int) -> list[CodeAction]:
    """The quick fixes available at a position.

    Args:
        project: The project; its buffer for `path` is the document's current text.
        path: The file.
        line: 1-based line of the start of the selection (or of the cursor).
        col: 1-based column of it.

    Returns:
        The actions: imports for an unknown name under the cursor, the missing arms of the `match` the cursor is in.
    """
    path = path.resolve()
    return [*import_fixes(project, path, line, col), *match_fixes(project, path, line)]
