"""Navigation: what every name in a project refers to, for hover, go to definition, rename and signature help.

The compiler's own passes cannot answer these questions: the linker renames definitions (`module·name`), drops the
imports and rewrites `alias.f(x)` into a plain call, and the checker stops at the first mistake of a statement. This
module instead resolves names on a fresh, tolerant parse of each file (so it works on a program that is half typed)
and borrows from the checker only what it alone knows: the type of every expression and variable.

The pieces:

- `Entity`: something a name can denote (a variable, a function, a field...), identified by a `key`.
- `Ref`: one written occurrence of a name, located by line, column and length, pointing to its `Entity`.
- `FileIndex`: one parsed file with its top-level definitions and imports.
- `Types`: the checker's types, looked up by source position.
- `Project`: the files of a workspace, which resolves imports (re-exports included), finds every `Ref` of an
  entity and plans renames.
"""

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from zylo.domain import ast
from zylo.domain.builtins import BUILTINS, METHODS, SHADOWABLE, receiver_kind
from zylo.domain.errors import ZyloError
from zylo.domain.types import EnumT, StructT, Type
from zylo.packaging.stdlib import stdlib_dir
from zylo.semantics.checker import check_all
from zylo.semantics.linker import RESERVED_TYPES, Linker, Module
from zylo.syntax.lexer import KEYWORDS, Token, tokenize
from zylo.syntax.parser import parse_collect

BUILTIN_TYPES = {"int", "float", "bool", "str", "list", "set", "dict", "tuple", "Result", "Cell", "unit"}
EXPRESSIONS = (ast.Int, ast.Float, ast.Str, ast.Bool, ast.Name, ast.BinOp, ast.UnaryOp, ast.Call, ast.CallValue,
               ast.Lambda, ast.ListLit, ast.Index, ast.TupleLit, ast.DictLit, ast.SetLit, ast.Slice, ast.FString,
               ast.Attr, ast.MethodCall, ast.NoneLit, ast.IsCheck, ast.Try)
RENAMEABLE = {"variable", "parameter", "function", "struct", "enum", "variant", "field", "method", "alias"}
SKIPPED_DIRS = {"node_modules", "target", "__pycache__", "venv"}
NAME_SHAPE = re.compile(r"[^\W\d]\w*")


class RenameError(Exception):
    """A rename that cannot be done, with the reason to show the user."""


# ---- entities and references --------------------------------------------------------------------------------

class Entity:
    """Something a name can denote.

    Attributes:
        kind: "variable", "parameter", "self", "function", "struct", "enum", "variant", "field", "method", "module",
            "alias" (a name bound by `from m import x as y`), "builtin" (a built-in function) or "builtin_method" (a
            method of `str`, a list, a dict, a set, a `Cell`, an Option or a Result).
        name: The name as written where it is declared.
        key: What makes two entities the same thing; equality and hashing use only this.
        path: The file that declares it (for a module, the file that imports it).
        line: 1-based line of its name where it is declared (0 when it has no place in a file).
        col: 1-based column of that name.
        node: The syntax node that defines it (a function, a struct, a parameter, a field...), when there is one.
        owner: The struct or enum of a field, variant or method; the function of a parameter.
        target: What an alias stands for, or the type a `self` belongs to.
        module_path: For a module, the file it names.
        type_key: Where the checker keeps the type of a variable declared by a loop, a pattern or a parameter.
        receiver: For a built-in method, the type of the value it is called on.
    """

    def __init__(self, kind: str, name: str, key: tuple, path: Path | None = None, line: int = 0, col: int = 0,
                 node: ast.Node | None = None, owner: "Entity | None" = None, target: "Entity | None" = None,
                 module_path: Path | None = None, type_key: tuple | None = None, receiver: Type | None = None):
        """Create an entity; see the class attributes for the arguments."""
        self.kind, self.name, self.key, self.path, self.line, self.col = kind, name, key, path, line, col
        self.node, self.owner, self.target, self.module_path, self.type_key = node, owner, target, module_path, type_key
        self.receiver = receiver

    def __eq__(self, other: object) -> bool:
        """Entities are equal when their keys are."""
        return isinstance(other, Entity) and self.key == other.key

    def __hash__(self) -> int:
        """Hash by key."""
        return hash(self.key)

    def __repr__(self) -> str:
        """Show the kind and the name."""
        return f"<{self.kind} {self.name}>"


def deref(e: Entity | None) -> Entity | None:
    """Follow aliases (`from m import x as y`) to the entity they stand for.

    Args:
        e: An entity or None.

    Returns:
        The first entity that is not an alias, or the last alias when its target is unknown.
    """
    seen = 0
    while e is not None and e.kind == "alias" and e.target is not None and seen < 50:
        e, seen = e.target, seen + 1
    return e


@dataclass(eq=False)
class Scope:
    """A lexical scope: the variables declared in it so far and the scope around it.

    Attributes:
        vars: Variables by name.
        parent: The enclosing scope, or None for the outermost one of a function or of the module.
    """

    vars: dict[str, Entity] = field(default_factory=dict)
    parent: "Scope | None" = None

    def lookup(self, name: str) -> Entity | None:
        """Find a visible variable by name."""
        s: Scope | None = self
        while s is not None:
            if name in s.vars:
                return s.vars[name]
            s = s.parent
        return None

    def chain(self) -> Iterator["Scope"]:
        """This scope and every scope around it, innermost first."""
        s: Scope | None = self
        while s is not None:
            yield s
            s = s.parent

    def names(self) -> set[str]:
        """Every variable name visible here."""
        return {n for s in self.chain() for n in s.vars}


@dataclass
class Ref:
    """One written occurrence of a name.

    Attributes:
        line: 1-based line.
        col: 1-based column.
        length: Length of the name in characters.
        name: The name as written.
        entity: What it denotes, or None when nothing was found.
        decl: True where the name is declared (a definition, a parameter, the first assignment).
        space: "name" for identifiers that live in the scope's namespace, "member" for those after a dot, a field or
            variant name, a named argument.
        needs_type: True for a member access whose receiver type is unknown, so it could not be resolved.
        scope: The scope the occurrence is in.
    """

    line: int
    col: int
    length: int
    name: str
    entity: Entity | None
    decl: bool
    space: str
    needs_type: bool
    scope: Scope | None


@dataclass
class DeclInfo:
    """Where a variable was declared, for checking that a new name does not collide.

    Attributes:
        scope: The scope it was declared in.
        visible: The variable names already visible there.
    """

    scope: Scope
    visible: set[str]


@dataclass
class Resolution:
    """The result of resolving one file.

    Attributes:
        index: The file.
        types: The checker's types for it, or None when the program could not be linked.
        refs: Every occurrence of a name found, in source order.
        lost: Occurrences that could not be located in the text (name, entity).
        decls: Variable declarations by entity key.
    """

    index: "FileIndex"
    types: "Types | None"
    refs: list[Ref] = field(default_factory=list)
    lost: list[tuple[str, Entity | None]] = field(default_factory=list)
    decls: dict[tuple, DeclInfo] = field(default_factory=dict)

    def at(self, line: int, col: int, touching: bool = True) -> Ref | None:
        """The occurrence under a position.

        Args:
            line: 1-based line.
            col: 1-based column.
            touching: Whether a position right after a name counts as inside it (a text cursor does; a mouse does not).

        Returns:
            The occurrence, or None.
        """
        found = None
        for r in self.refs:
            if r.line == line and r.col <= col < r.col + r.length:
                return r
            if touching and r.line == line and col == r.col + r.length and found is None:
                found = r
        return found


# ---- types from the checker ---------------------------------------------------------------------------------

class Types:
    """The types the checker found, by source position.

    Attributes:
        expr: The type of each expression, by `(file, line, col)`.
        binders: The type of each parameter, loop variable and pattern variable, by `(file, kind, line, col, index)`.
        owners: For every struct and enum of the linked program, the file and name that define it, by linked name.
    """

    def __init__(self) -> None:
        """Create an empty table."""
        self.expr: dict[tuple[Path, int, int], Type] = {}
        self.binders: dict[tuple, Type] = {}
        self.owners: dict[str, tuple[Path, str]] = {}

    @classmethod
    def from_link(cls, linker: Linker) -> "Types":
        """Collect the types of a linked and checked program.

        Args:
            linker: The linker that loaded the program, after `check_all` ran on it.

        Returns:
            The table.
        """
        t = cls()
        for m in linker.order:
            f = m.path.resolve()
            for name, kind in m.defs.items():
                if kind in ("struct", "enum"):
                    t.owners[m.mangled(name)] = (f, name)
            for n in ast.walk(m.program):
                t.collect(f, n)
        return t

    def collect(self, f: Path, n: ast.Node) -> None:
        """Record what one node tells about types."""
        if isinstance(n, EXPRESSIONS) and n.ty is not None and n.line:
            self.expr[(f, n.line, n.col)] = n.ty
        elif isinstance(n, ast.Param) and n.info is not None:
            self.binders[(f, "param", n.line, n.col, 0)] = n.info.ty
        elif isinstance(n, ast.For):
            infos = n.infos if n.targets else ([n.info] if n.info is not None else [])
            for i, info in enumerate(infos or []):
                self.binders[(f, "for", n.line, n.col, i)] = info.ty
        elif isinstance(n, ast.MatchArm):
            for i, v in enumerate(n.vars):
                if v is not None:
                    self.binders[(f, "match", n.line, n.col, i)] = v.ty

    def at(self, path: Path, line: int, col: int) -> Type | None:
        """The type of the expression at a position, or None when the checker did not type it."""
        return self.expr.get((path, line, col))

    def binder(self, path: Path, key: tuple | None) -> Type | None:
        """The type of a parameter, loop variable or pattern variable."""
        return self.binders.get((path, *key)) if key else None

    def owner_of(self, t: Type | None) -> tuple[Path, str] | None:
        """The file and name that define a struct or enum type, or None for any other type."""
        if isinstance(t, (StructT, EnumT)):
            return self.owners.get(t.name)
        return None


def show_type(t: Type) -> str:
    """A type as a reader writes it: linked names `module·T` are shown as `T`."""
    return re.sub(r"(?:\w+\u00b7)+", "", str(t))


# ---- one file ---------------------------------------------------------------------------------------------

@dataclass
class ModuleBinding:
    """`import a.b [as c]`: a module bound to a name.

    Attributes:
        dotted: The module as written.
        segments: The tokens of the dotted name.
        alias: The token of the alias, or None.
    """

    dotted: str
    segments: list[Token]
    alias: Token | None


@dataclass
class FromBinding:
    """One name of `from m import x [as y]`.

    Attributes:
        dotted: The module as written.
        segments: The tokens of the dotted module name.
        original: The imported name.
        name_tok: Its token.
        alias_tok: The token of the alias, or None.
    """

    dotted: str
    segments: list[Token]
    original: str
    name_tok: Token | None
    alias_tok: Token | None


def doc_of(node: ast.Node | None) -> str | None:
    """The description of a definition: the string written right after it, or a function's leading string statement."""
    if isinstance(node, (ast.StructDef, ast.EnumDef, ast.Field, ast.Variant)):
        return node.doc
    if isinstance(node, ast.FnDef) and node.body:
        first = node.body[0]
        if isinstance(first, ast.ExprStmt) and isinstance(first.expr, ast.Str):
            return first.expr.value
    return None


class FileIndex:
    """One source file, parsed tolerantly, with its top-level definitions and imports.

    Attributes:
        project: The project it belongs to.
        path: The file (resolved).
        source: Its text.
        lines: The text split into lines.
        tokens: Its tokens (empty when the text does not even lex).
        program: Whatever parsed.
        errors: The syntax errors found (the statements that failed are missing from `program`).
        defs: Top-level functions, structs and enums by name.
        aliases: `import` bindings by the name they introduce.
        froms: `from ... import` bindings by the name they introduce.
        pubs: The names `pub from` re-exports.
        globals: Variables assigned at the top level.
    """

    def __init__(self, project: "Project", path: Path, source: str):
        """Parse a file and collect its top-level names.

        Args:
            project: The project it belongs to.
            path: Its resolved path.
            source: Its text.
        """
        self.project, self.path, self.source = project, path, source
        self.lines = source.splitlines()
        try:
            self.tokens: list[Token] = tokenize(source)
        except ZyloError:
            self.tokens = []
        self.tok_at = {(t.line, t.col): i for i, t in enumerate(self.tokens)}
        self.program, self.errors = parse_collect(source, str(path))
        self.defs: dict[str, ast.FnDef | ast.StructDef | ast.EnumDef] = {}
        self.aliases: dict[str, ModuleBinding] = {}
        self.froms: dict[str, FromBinding] = {}
        self.pubs: set[str] = set()
        self.globals: set[str] = set()
        self.collect()

    # ---- tokens ----

    def tok(self, i: int) -> Token | None:
        """The token at an index, or None past the end."""
        return self.tokens[i] if 0 <= i < len(self.tokens) else None

    def name_pos(self, node: ast.Node, name: str) -> tuple[int, int] | None:
        """Where `name` is written in a definition that starts at `node`'s keyword (a function's name)."""
        i = self.tok_at.get((node.line, node.col))
        if i is None:
            return None
        for t in self.tokens[i:i + 8]:
            if t.kind == "IDENT" and t.value == name:
                return t.line, t.col
        return None

    def dotted(self, i: int) -> tuple[list[Token], int]:
        """Read `a.b.c` at token `i`: the identifier tokens and the index after them."""
        segs: list[Token] = []
        while (t := self.tok(i)) is not None and t.kind == "IDENT":
            segs.append(t)
            i += 1
            nxt = self.tok(i)
            if nxt is not None and nxt.kind == "OP" and nxt.value == ".":
                i += 1
            else:
                break
        return segs, i

    def collect(self) -> None:
        """Record the top-level definitions, imports and variables."""
        for s in self.program.body:
            if isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef)):
                self.defs.setdefault(s.name, s)
            elif isinstance(s, ast.Import):
                i = self.tok_at.get((s.line, s.col))
                if i is None:
                    continue
                segs, j = self.dotted(i + 1)
                as_tok = self.tok(j + 1) if (self.tok(j) and self.tok(j).value == "as") else None
                local = s.alias or s.module.rsplit(".", 1)[-1]
                self.aliases.setdefault(local, ModuleBinding(s.module, segs, as_tok))
            elif isinstance(s, ast.FromImport):
                self.collect_from(s)
            elif isinstance(s, ast.Assign) and isinstance(s.target, ast.Name):
                self.globals.add(s.target.id)
            elif isinstance(s, ast.Unpack):
                self.globals.update(t.id for t in s.targets)

    def collect_from(self, s: ast.FromImport) -> None:
        """Record the names a `from m import x [as y]` binds, with their tokens."""
        i = self.tok_at.get((s.line, s.col))
        if i is None:
            return
        if self.tok(i) and self.tok(i).value == "pub":
            i += 1
        segs, j = self.dotted(i + 1)
        j += 1  # `import`
        for name, alias in s.names:
            name_tok = self.tok(j)
            alias_tok = None
            j += 1
            if alias is not None:
                alias_tok = self.tok(j + 1)
                j += 2
            j += 1  # the comma
            local = alias or name
            self.froms.setdefault(local, FromBinding(s.module, segs, name, name_tok, alias_tok))
            if s.pub:
                self.pubs.add(local)

    @property
    def top_names(self) -> set[str]:
        """Every name the top level of this file binds."""
        return set(self.defs) | set(self.aliases) | set(self.froms) | self.globals

    # ---- entities ----

    def def_entity(self, name: str) -> Entity | None:
        """The entity of a top-level function, struct or enum."""
        node = self.defs.get(name)
        if node is None:
            return None
        if isinstance(node, ast.FnDef):
            kind = "function"
            line, col = self.name_pos(node, name) or (node.line, node.col)
        else:
            kind = "struct" if isinstance(node, ast.StructDef) else "enum"
            line, col = node.line, node.col
        return Entity(kind, name, (kind, self.path, name), self.path, line, col, node)

    def module_entity(self, local: str) -> Entity | None:
        """The entity of a module bound by `import`."""
        b = self.aliases.get(local)
        if b is None:
            return None
        tok = b.alias or (b.segments[-1] if b.segments else None)
        target = self.project.module_path(self.path, b.dotted)
        return Entity("module", local, ("module", self.path, local), self.path, tok.line if tok else 0,
                      tok.col if tok else 0, module_path=target)

    def local_import(self, local: str, seen: tuple = ()) -> Entity | None:
        """The entity a name imported with `from m import x [as y]` denotes here."""
        b = self.froms.get(local)
        if b is None:
            return None
        target = self.project.module_path(self.path, b.dotted)
        original = self.project.export_entity(target, b.original, seen) if target else None
        if b.alias_tok is None:
            return original
        return Entity("alias", local, ("alias", self.path, local), self.path, b.alias_tok.line, b.alias_tok.col,
                      target=original)

    def module_level(self, name: str) -> Entity | None:
        """What a name means outside any function: a definition here, an import, or an imported module."""
        if name in self.defs:
            return self.def_entity(name)
        if name in self.froms:
            return self.local_import(name)
        if name in self.aliases:
            return self.module_entity(name)
        return None


def param_entity(path: Path, p: ast.Param, fn: Entity | None, key_kind: str = "param") -> Entity:
    """The entity of a function parameter."""
    return Entity("parameter", p.name, ("var", path, p.line, p.col), path, p.line, p.col, p, owner=fn,
                  type_key=(key_kind, p.line, p.col, 0))


# ---- the walk ---------------------------------------------------------------------------------------------

class Walker:
    """Walks one file with its scopes and records every occurrence of a name as a `Ref`."""

    def __init__(self, fx: FileIndex, types: "Types | None"):
        """Prepare to resolve a file.

        Args:
            fx: The file.
            types: The checker's types for it, if the program could be linked.
        """
        self.fx, self.types, self.project, self.path = fx, types, fx.project, fx.path
        self.res = Resolution(fx, types)
        self.scope = Scope()
        self.seen: set[tuple[int, int]] = set()

    def run(self) -> Resolution:
        """Resolve the whole file.

        Returns:
            The occurrences found.
        """
        for s in self.fx.program.body:
            self.top(s)
        self.res.refs.sort(key=lambda r: (r.line, r.col))
        return self.res

    # ---- recording ----

    def ref(self, line: int, col: int, name: str, entity: Entity | None, *, decl: bool = False,
            space: str = "name", needs_type: bool = False) -> None:
        """Record one occurrence, after checking that the text at that place really is the name."""
        if (line, col) in self.seen:
            return
        text = self.fx.lines[line - 1] if 0 < line <= len(self.fx.lines) else ""
        if text[col - 1:col - 1 + len(name)] != name:
            self.res.lost.append((name, entity))
            return
        self.seen.add((line, col))
        self.res.refs.append(Ref(line, col, len(name), name, entity, decl, space, needs_type, self.scope))

    def tok_ref(self, t: Token | None, entity: Entity | None, **kw) -> None:
        """Record the occurrence a token is."""
        if t is not None:
            self.ref(t.line, t.col, t.value, entity, **kw)

    def declare(self, entity: Entity, t: Token | tuple[int, int] | None) -> None:
        """Declare a variable in the current scope at its token."""
        line, col = (t.line, t.col) if isinstance(t, Token) else (t or (0, 0))
        if not line:
            return
        self.res.decls[entity.key] = DeclInfo(self.scope, self.scope.names())
        self.scope.vars[entity.name] = entity
        self.ref(line, col, entity.name, entity, decl=True)

    def push(self) -> None:
        """Open a scope inside the current one."""
        self.scope = Scope(parent=self.scope)

    def pop(self) -> None:
        """Close the current scope."""
        assert self.scope.parent is not None
        self.scope = self.scope.parent

    def lookup(self, name: str) -> Entity | None:
        """Resolve a plain name: a variable in scope, else a module-level name, else a built-in."""
        found = self.scope.lookup(name) or self.fx.module_level(name)
        if found is None and name in BUILTINS:
            found = Entity("builtin", name, ("builtin", name))
        return found

    # ---- top level ----

    def top(self, s: ast.Stmt) -> None:
        """Resolve one top-level statement."""
        fx = self.fx
        if isinstance(s, ast.FnDef):
            self.function(s, fx.def_entity(s.name) if fx.defs.get(s.name) is s else None, None)
        elif isinstance(s, ast.StructDef):
            self.struct(s)
        elif isinstance(s, ast.EnumDef):
            self.enum(s)
        elif isinstance(s, ast.Import):
            self.import_(s)
        elif isinstance(s, ast.FromImport):
            self.from_import(s)
        else:
            self.visit(s)

    def import_(self, s: ast.Import) -> None:
        """Resolve the module name of an `import`."""
        local = s.alias or s.module.rsplit(".", 1)[-1]
        b = self.fx.aliases.get(local)
        ent = self.fx.module_entity(local)
        if b is None or ent is None:
            return
        for i, t in enumerate(b.segments):
            self.tok_ref(t, ent, decl=b.alias is None and i == len(b.segments) - 1)
        self.tok_ref(b.alias, ent, decl=True)

    def from_import(self, s: ast.FromImport) -> None:
        """Resolve the module and the names of a `from ... import`."""
        fx = self.fx
        target = self.project.module_path(self.path, s.module)
        for name, alias in s.names:
            local = alias or name
            b = fx.froms.get(local)
            if b is None or b.original != name:
                continue
            if b.segments:
                mod = Entity("module", s.module, ("module-path", target), self.path, b.segments[-1].line,
                             b.segments[-1].col, module_path=target)
                for t in b.segments:
                    self.tok_ref(t, mod)
            original = self.project.export_entity(target, name) if target else None
            self.tok_ref(b.name_tok, original, space="member" if alias else "name")
            if alias:
                self.tok_ref(b.alias_tok, fx.local_import(local), decl=True)

    def function(self, f: ast.FnDef, ent: Entity | None, owner: Entity | None) -> None:
        """Resolve a function or method: its name, parameters and body, in a scope of its own."""
        pos = self.fx.name_pos(f, f.name)
        if pos:
            self.ref(pos[0], pos[1], f.name, ent, decl=True, space="member" if owner else "name")
        saved, self.scope = self.scope, Scope()
        if owner is not None and f.has_self:
            self.scope.vars["self"] = Entity("self", "self", ("self", self.path, f.line, f.col), target=owner)
        for p in f.params:
            self.declare(param_entity(self.path, p, ent), (p.line, p.col))
            if p.type is not None:
                self.visit(p.type)
            if p.default is not None:
                self.visit(p.default)
        if f.ret is not None:
            self.visit(f.ret)
        for s in f.body:
            self.visit(s)
        self.scope = saved

    def struct(self, s: ast.StructDef) -> None:
        """Resolve a struct: its name, fields and methods."""
        ent = self.fx.def_entity(s.name) if self.fx.defs.get(s.name) is s else None
        self.ref(s.line, s.col, s.name, ent, decl=True)
        for f in s.fields:
            fe = self.project.member(ent, f.name) if ent else None
            self.ref(f.line, f.col, f.name, fe, decl=True, space="member")
            self.visit(f.type)
            if f.default is not None:
                self.visit(f.default)
        for m in s.methods:
            self.function(m, self.project.member(ent, m.name) if ent else None, ent or Entity("struct", s.name, ("struct?", s.name)))

    def enum(self, s: ast.EnumDef) -> None:
        """Resolve an enum: its name, variants and methods."""
        ent = self.fx.def_entity(s.name) if self.fx.defs.get(s.name) is s else None
        self.ref(s.line, s.col, s.name, ent, decl=True)
        for v in s.variants:
            ve = self.project.member(ent, v.name) if ent else None
            self.ref(v.line, v.col, v.name, ve, decl=True, space="member")
            for t in v.fields:
                self.visit(t)
        for m in s.methods:
            self.function(m, self.project.member(ent, m.name) if ent else None, ent or Entity("enum", s.name, ("enum?", s.name)))

    # ---- statements and expressions ----

    def block(self, body: list[ast.Stmt]) -> None:
        """Resolve a block of statements in a scope of its own."""
        self.push()
        for s in body:
            self.visit(s)
        self.pop()

    def visit(self, n: ast.Node) -> None:
        """Resolve a statement, an expression or a written type."""
        if isinstance(n, ast.Assign):
            self.assign(n)
        elif isinstance(n, ast.Unpack):
            self.visit(n.value)
            for t in n.targets:
                self.bind(t)
        elif isinstance(n, ast.If):
            self.visit(n.cond)
            self.block(n.then)
            self.block(n.else_)
        elif isinstance(n, ast.While):
            self.visit(n.cond)
            self.block(n.body)
        elif isinstance(n, ast.For):
            self.for_loop(n)
        elif isinstance(n, ast.Match):
            self.visit(n.value)
            for arm in n.arms:
                self.arm(arm)
        elif isinstance(n, (ast.FnDef, ast.StructDef, ast.EnumDef)):
            self.top(n)
        elif isinstance(n, ast.TypeRef):
            self.typeref(n)
        else:
            self.expr(n)

    def bind(self, t: ast.Name) -> None:
        """An assignment target: a use of a visible variable, or the declaration of a new one."""
        existing = self.scope.lookup(t.id)
        if existing is not None:
            self.ref(t.line, t.col, t.id, existing)
        else:
            self.declare(Entity("variable", t.id, ("var", self.path, t.line, t.col), self.path, t.line, t.col), (t.line, t.col))

    def assign(self, s: ast.Assign) -> None:
        """Resolve an assignment (the value first: a variable is declared after its value is computed)."""
        t = s.target
        if isinstance(t, ast.Name) and isinstance(s.value, ast.Lambda) and s.value.name == t.id:
            pos = self.fx.name_pos(s.value, t.id)  # a local `fn name(...)`: the target is positioned at the keyword
            if pos and self.scope.lookup(t.id) is None:
                self.declare(Entity("variable", t.id, ("var", self.path, *pos), self.path, *pos), pos)
            self.visit(s.value)
            return
        if s.annotation is not None:
            self.visit(s.annotation)
        self.visit(s.value)
        if isinstance(t, ast.Name):
            self.bind(t)
        else:
            self.visit(t)

    def for_loop(self, s: ast.For) -> None:
        """Resolve a `for` loop: the iterated value, then the loop variables and the body."""
        self.visit(s.iter)
        names = s.targets or [s.var]
        i = self.fx.tok_at.get((s.line, s.col))
        toks: list[Token] = []
        j = (i or 0) + 1
        while i is not None and len(toks) < len(names) and (t := self.fx.tok(j)) is not None and t.kind == "IDENT":
            toks.append(t)
            j += 2
        self.push()
        for k, name in enumerate(names):
            if k < len(toks) and toks[k].value == name:
                t = toks[k]
                self.declare(Entity("variable", name, ("var", self.path, t.line, t.col), self.path, t.line, t.col,
                                    type_key=("for", s.line, s.col, k)), t)
        for st in s.body:
            self.visit(st)
        self.pop()

    def pattern_tokens(self, arm: ast.MatchArm) -> tuple[list[Token], list[Token]]:
        """The tokens of a pattern's `Enum.Variant` path and of the names it binds."""
        i = self.fx.tok_at.get((arm.line, arm.col))
        t = self.fx.tok(i) if i is not None else None
        if i is None or t is None or t.kind != "IDENT":
            return [], []
        parts, j = self.fx.dotted(i)
        binds: list[Token] = []
        nxt = self.fx.tok(j)
        if nxt is not None and nxt.kind == "OP" and nxt.value == "(":
            j += 1
            while (b := self.fx.tok(j)) is not None and b.kind == "IDENT":
                binds.append(b)
                j += 2
        return parts, binds

    def pattern(self, arm: ast.MatchArm, shared: list[Entity] | None) -> list[Entity]:
        """Resolve one pattern; the first of an or-pattern declares the names, the others use the same variables."""
        if arm.variant == "literal":
            if arm.literal is not None:
                self.visit(arm.literal)
            return []
        parts, binds = self.pattern_tokens(arm)
        if arm.type is not None and len(parts) >= 2:
            enum = deref(self.qualified(parts[:-1]))
            self.tok_ref(parts[-1], self.project.member(enum, arm.variant) if enum else None, space="member")
        made: list[Entity] = []
        for k, t in enumerate(binds):
            if shared is None:
                ent = Entity("variable", t.value, ("var", self.path, t.line, t.col), self.path, t.line, t.col,
                             type_key=("match", arm.line, arm.col, k))
                self.declare(ent, t)
                made.append(ent)
            elif k < len(shared):
                self.tok_ref(t, shared[k], decl=True)
        return made

    def arm(self, arm: ast.MatchArm) -> None:
        """Resolve a `match` arm: its pattern(s), guard and body, in a scope of their own."""
        self.push()
        made = self.pattern(arm, None)
        for alt in arm.alts:
            self.pattern(alt, made)
        if arm.guard is not None:
            self.visit(arm.guard)
        for s in arm.body:
            self.visit(s)
        self.pop()

    def qualified(self, toks: list[Token]) -> Entity | None:
        """Resolve a written `name` or `module.name` made of the given tokens."""
        if not toks:
            return None
        first = self.fx.module_level(toks[0].value)
        self.tok_ref(toks[0], first)
        base = deref(first)
        if len(toks) == 1:
            return first
        if base is not None and base.kind == "module" and base.module_path is not None:
            ent = self.project.export_entity(base.module_path, toks[1].value)
            self.tok_ref(toks[1], ent, space="member")
            return ent
        return None

    def typeref(self, t: ast.TypeRef) -> None:
        """Resolve a written type: the struct or enum it names, and the types inside it."""
        if t.name not in ("?", "fn") and t.name.split(".")[0] not in BUILTIN_TYPES:
            i = self.fx.tok_at.get((t.line, t.col))
            if i is not None:
                toks, _ = self.fx.dotted(i)
                self.qualified(toks[:len(t.name.split("."))])
        for a in t.args:
            self.typeref(a)
        if t.ret is not None:
            self.typeref(t.ret)

    def expr(self, e: ast.Node) -> Entity | None:
        """Resolve an expression.

        Args:
            e: The expression.

        Returns:
            The entity a name or a dotted name denotes, so that its parent can tell a module or an enum from a value.
        """
        if isinstance(e, ast.Name):
            ent = self.lookup(e.id)
            self.ref(e.line, e.col, e.id, ent)
            return ent
        if isinstance(e, ast.Call):
            self.call(e)
        elif isinstance(e, ast.MethodCall):
            self.method_call(e)
        elif isinstance(e, ast.Attr):
            return self.attr(e)
        elif isinstance(e, ast.Lambda):
            self.lambda_(e)
        else:
            for c in ast.children(e):
                self.visit(c)
        return None

    def lambda_(self, e: ast.Lambda) -> None:
        """Resolve an anonymous (or local) function: its parameters and body see the variables around it."""
        self.push()
        for p in e.params:
            self.declare(param_entity(self.path, p, None), (p.line, p.col))
            if p.type is not None:
                self.visit(p.type)
        if e.ret is not None:
            self.visit(e.ret)
        if e.expr is not None:
            self.visit(e.expr)
        for s in e.body:
            self.visit(s)
        self.pop()

    def call(self, e: ast.Call) -> None:
        """Resolve a call of a function, a struct constructor or a variable holding a function."""
        ent = self.lookup(e.func)
        self.ref(e.line, e.col, e.func, ent)
        for a in e.args:
            self.visit(a)
        self.named(e.named, deref(ent))

    def named(self, args: list[ast.NamedArg], callee: Entity | None) -> None:
        """Resolve `name=value` arguments against the parameters (or fields) of the callee."""
        for na in args:
            ent = None
            if callee is not None and callee.kind in ("function", "method") and isinstance(callee.node, ast.FnDef):
                p = next((p for p in callee.node.params if p.name == na.name), None)
                ent = param_entity(callee.path, p, callee) if p is not None and callee.path else None
            elif callee is not None and callee.kind == "struct":
                ent = self.project.member(callee, na.name)
            self.ref(na.line, na.col, na.name, ent, space="member")
            self.visit(na.value)

    def static_member(self, base: Entity | None, name: str) -> tuple[bool, Entity | None]:
        """What `base.name` means when `base` is a module (its export) or an enum (its variant).

        Returns:
            Whether `base` is one of those, and the entity found.
        """
        base = deref(base)
        if base is not None and base.kind == "module":
            return True, (self.project.export_entity(base.module_path, name) if base.module_path else None)
        if base is not None and base.kind == "enum":
            return True, self.project.member(base, name)
        return False, None

    def instance_member(self, e: ast.Attr | ast.MethodCall) -> tuple[Entity | None, bool]:
        """Resolve `value.name` on a value, by the type the checker found for `value`.

        Returns:
            The field or method, and whether the type of `value` was unknown (so the name could not be resolved).
        """
        t = self.types.at(self.path, e.value.line, e.value.col) if self.types else None
        if t is None:
            return None, True
        owner = self.types.owner_of(t) if self.types else None
        if owner is None:
            kind = receiver_kind(t)
            if kind is not None and e.name in METHODS[kind]:
                return Entity("builtin_method", e.name, ("builtin_method", show_type(t), e.name), receiver=t), False
            return None, False
        fx = self.project.file(owner[0])
        ent = fx.def_entity(owner[1]) if fx else None
        return (self.project.member(ent, e.name) if ent else None), False

    def attr(self, e: ast.Attr) -> Entity | None:
        """Resolve `value.name`: a module's export, an enum's variant, or a field."""
        base = self.expr(e.value)
        static, ent = self.static_member(base, e.name)
        missing = False
        if not static:
            ent, missing = self.instance_member(e)
        self.ref(e.line, e.col, e.name, ent, space="member", needs_type=missing)
        return ent if static else None

    def method_call(self, e: ast.MethodCall) -> None:
        """Resolve `value.name(args)`: a module's function, a variant constructor, or a method."""
        base = self.expr(e.value)
        static, ent = self.static_member(base, e.name)
        missing = False
        if not static:
            ent, missing = self.instance_member(e)
        self.ref(e.line, e.col, e.name, ent, space="member", needs_type=missing)
        for a in e.args:
            self.visit(a)
        self.named(e.named, deref(ent))


# ---- the project ------------------------------------------------------------------------------------------

class Project:
    """The files of a workspace, and the questions that cross files.

    Attributes:
        root: The workspace directory, or None.
        overrides: The text of files that differ from disk (an editor's unsaved buffers), by resolved path.
        packages_dir: The active environment's `packages` directory, or None.
        trusted: Package names allowed to call Rust.
    """

    def __init__(self, root: Path | None = None, overrides: dict[Path, str] | None = None,
                 packages_dir: Path | None = None, trusted: list[str] | set[str] = ()):
        """Create a project.

        Args:
            root: The workspace directory (renames only touch files below it).
            overrides: Unsaved text by file.
            packages_dir: Directory of installed packages, or None.
            trusted: Packages the project trusts with Rust access.
        """
        self.root = root.resolve() if root else None
        self.overrides = {p.resolve(): t for p, t in (overrides or {}).items()}
        self.packages_dir = packages_dir.resolve() if packages_dir else None
        self.trusted = list(trusted)
        self.linker = Linker(self.packages_dir, self.trusted)
        self.files_by_path: dict[Path, FileIndex | None] = {}
        self.module_paths: dict[tuple[Path, str], Path | None] = {}
        self.resolutions: dict[Path, Resolution | None] = {}

    # ---- files ----

    def text_of(self, path: Path) -> str | None:
        """The current text of a file: the editor's buffer, else the disk, else None."""
        path = path.resolve()
        if path in self.overrides:
            return self.overrides[path]
        try:
            return path.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def file(self, path: Path | None) -> FileIndex | None:
        """The parsed file at a path, or None when it cannot be read."""
        if path is None:
            return None
        path = path.resolve()
        if path not in self.files_by_path:
            text = self.text_of(path)
            self.files_by_path[path] = FileIndex(self, path, text) if text is not None else None
        return self.files_by_path[path]

    def module_path(self, importer: Path, dotted: str) -> Path | None:
        """The file a module name refers to from a file, or None when there is none."""
        key = (importer, dotted)
        if key not in self.module_paths:
            fx = self.file(importer)
            found = None
            if fx is not None:
                try:
                    found = self.linker.resolve_path(dotted, Module("", importer, fx.program), ast.Node()).resolve()
                except ZyloError:
                    found = None
            self.module_paths[key] = found
        return self.module_paths[key]

    def workspace_files(self) -> list[Path]:
        """Every `.zy` file of the workspace (hidden and build directories are skipped), plus the open buffers."""
        found: set[Path] = set(self.overrides)
        if self.root is not None:
            for dirpath, dirs, names in os.walk(self.root):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIPPED_DIRS]
                found.update(Path(dirpath, n).resolve() for n in names if n.endswith(".zy"))
        return sorted(p for p in found if self.in_workspace(p))

    def in_workspace(self, path: Path) -> bool:
        """Whether a file belongs to the workspace and may be edited (not a package or the standard library)."""
        path = path.resolve()
        if stdlib_dir() in path.parents or (self.packages_dir and self.packages_dir in path.parents):
            return False
        return self.root is None or self.root in path.parents or path in self.overrides

    # ---- entities across files ----

    def export_entity(self, path: Path | None, name: str, seen: tuple = ()) -> Entity | None:
        """What a module exposes under a name, following `pub from` re-exports to the real definition."""
        if path is None or (path, name) in seen:
            return None
        fx = self.file(path)
        if fx is None:
            return None
        if name in fx.defs:
            return fx.def_entity(name)
        if name in fx.pubs:
            return fx.local_import(name, (*seen, (path, name)))
        return None

    def member(self, owner: Entity | None, name: str) -> Entity | None:
        """A field, variant or method of a struct or enum."""
        owner = deref(owner)
        if owner is None or owner.kind not in ("struct", "enum") or not isinstance(owner.node, (ast.StructDef, ast.EnumDef)):
            return None
        node, path = owner.node, owner.path
        if isinstance(node, ast.StructDef):
            for f in node.fields:
                if f.name == name:
                    return Entity("field", name, ("field", path, owner.name, name), path, f.line, f.col, f, owner=owner)
        else:
            for v in node.variants:
                if v.name == name:
                    return Entity("variant", name, ("variant", path, owner.name, name), path, v.line, v.col, v, owner=owner)
        for m in node.methods:
            if m.name == name:
                fx = self.file(path)
                line, col = (fx.name_pos(m, name) if fx else None) or (m.line, m.col)
                return Entity("method", name, ("method", path, owner.name, name), path, line, col, m, owner=owner)
        return None

    def members(self, owner: Entity) -> set[str]:
        """Every name a struct or enum declares: fields, variants and methods."""
        node = owner.node
        names: set[str] = set()
        if isinstance(node, ast.StructDef):
            names |= {f.name for f in node.fields} | {m.name for m in node.methods}
        elif isinstance(node, ast.EnumDef):
            names |= {v.name for v in node.variants} | {m.name for m in node.methods}
        return names

    # ---- resolution ----

    def types_for(self, path: Path) -> Types | None:
        """The checker's types for a file, linking and checking it as the entry of a program; None if it cannot be linked."""
        found = self.link(path)
        return Types.from_link(found[0]) if found else None

    def link(self, path: Path) -> tuple[Linker, ast.Program] | None:
        """Link and check a file as the entry of a program, skipping the statements that do not parse.

        Args:
            path: The entry file.

        Returns:
            The linker (holding every module, annotated with types) and the linked program, or None when the program
            cannot be linked (an unknown import, a cycle...).
        """
        linker = Linker(self.packages_dir, self.trusted, overrides=self.overrides, tolerant=True)
        try:
            program = linker.link(path)
            check_all(program)
            return linker, program
        except (ZyloError, RecursionError, OSError):
            return None

    def probe_type(self, path: Path, line: int) -> tuple[Type, Types] | None:
        """The type of the expression statement written alone on a line of a file.

        Completion uses it to find the type of the value in front of a dot: it rewrites the line being typed into
        that value on its own and asks the checker.

        Args:
            path: The file.
            line: 1-based line of the expression statement.

        Returns:
            The expression's type with the type table of the program (to find what defines a struct), or None when the
            program does not link or the checker did not reach the line.
        """
        found = self.link(path)
        if found is None:
            return None
        linker, _ = found
        mod = linker.by_path.get(path.resolve())
        if mod is None:
            return None
        for n in ast.walk(mod.program):
            if isinstance(n, ast.ExprStmt) and n.line == line and n.expr.ty is not None:
                return n.expr.ty, Types.from_link(linker)
        return None

    def resolution(self, path: Path) -> Resolution | None:
        """Resolve every name of a file (and cache the result); None when the file cannot be read."""
        path = path.resolve()
        if path not in self.resolutions:
            fx = self.file(path)
            self.resolutions[path] = Walker(fx, self.types_for(path)).run() if fx is not None else None
        return self.resolutions[path]

    def ref_at(self, path: Path, line: int, col: int, touching: bool = True) -> tuple[Ref, Resolution] | None:
        """The occurrence of a name under a position, with the resolution of its file.

        Args:
            path: The file.
            line: 1-based line.
            col: 1-based column.
            touching: Whether a position right after a name counts as inside it.

        Returns:
            The occurrence and the file's resolution, or None.
        """
        res = self.resolution(path)
        if res is None:
            return None
        r = res.at(line, col, touching)
        return (r, res) if r is not None else None

    def references(self, entity: Entity) -> list[tuple[Path, Ref]]:
        """Every occurrence of an entity in the workspace, declarations included.

        Raises:
            RenameError: When some occurrence cannot be located or typed, so a rename would miss it.
        """
        local = entity.kind in ("variable", "parameter", "self") and not (entity.kind == "parameter" and entity.owner is not None)
        candidates = [entity.path] if local and entity.path else self.workspace_files()
        pattern = re.compile(rf"(?<!\w){re.escape(entity.name)}(?!\w)")
        out: list[tuple[Path, Ref]] = []
        for p in candidates:
            text = self.text_of(p) if p is not None else None
            if p is None or text is None or (p != entity.path and not pattern.search(text)):
                continue
            res = self.resolution(p)
            if res is None:
                continue
            if res.index.errors:  # the statements that failed to parse may use the name, and cannot be seen
                raise RenameError(f"{p.name} has a syntax error and may use {entity.name!r}; fix it first")
            for name, ent in res.lost:
                if name == entity.name and (ent is None or ent == entity):
                    raise RenameError(f"cannot find every use of {entity.name!r} in {p.name}")
            member = entity.kind in ("field", "method", "variant")
            for r in res.refs:
                if r.needs_type and member and r.name == entity.name:
                    raise RenameError(
                        f"cannot rename {entity.name!r} safely: the type of a value it is used on is unknown in "
                        f"{p.name}:{r.line}; fix the errors in the program first")
                if r.entity == entity:
                    out.append((p, r))
        return out

    # ---- renaming ----

    def check_renameable(self, entity: Entity) -> None:
        """Reject what cannot be renamed at all: built-ins, modules, `self`, and names defined outside the workspace.

        Raises:
            RenameError: With the reason.
        """
        if entity.kind not in RENAMEABLE:
            what = {"builtin": "a built-in name", "builtin_method": "a built-in method", "module": "a module", "self": "`self`"}.get(entity.kind, entity.kind)
            raise RenameError(f"cannot rename {what}")
        if entity.path is None or not self.in_workspace(entity.path):
            raise RenameError(f"{entity.name!r} is defined outside this workspace (a package or the standard library)")

    def check_new_name(self, entity: Entity, new: str) -> None:
        """Reject names that are not identifiers, keywords or reserved.

        Raises:
            RenameError: With the reason.
        """
        if not NAME_SHAPE.fullmatch(new):
            raise RenameError(f"{new!r} is not a valid name")
        if new in KEYWORDS:
            raise RenameError(f"{new!r} is a keyword")
        if new in ("self", "_"):
            raise RenameError(f"{new!r} has a special meaning and cannot be used as a name")
        if entity.kind in ("function", "struct", "enum", "alias") and (new in BUILTINS or new in BUILTIN_TYPES or new in RESERVED_TYPES):
            raise RenameError(f"{new!r} is a built-in name")
        if entity.kind in ("variable", "parameter") and (new in BUILTINS - SHADOWABLE or new in RESERVED_TYPES):
            raise RenameError(f"{new!r} is a built-in name")

    def plan_rename(self, entity: Entity, new: str) -> list[tuple[Path, Ref]]:
        """Work out every place to change to rename an entity, checking that nothing breaks.

        Args:
            entity: What to rename.
            new: The new name.

        Returns:
            The occurrences to rewrite (empty when the name does not change).

        Raises:
            RenameError: If the entity cannot be renamed, the name is invalid, or it would collide with another.
        """
        self.check_renameable(entity)
        self.check_new_name(entity, new)
        if new == entity.name:
            return []
        refs = self.references(entity)
        foreign = {p for p, _ in refs if p != entity.path}
        if new.startswith("_") and foreign and entity.kind in ("function", "struct", "enum", "alias"):
            raise RenameError(f"{new!r} would be private to its module, but other files use {entity.name!r}")
        for p, _ in refs:
            if not self.in_workspace(p):
                raise RenameError(f"{entity.name!r} is also used in {p.name}, which is not part of this workspace")
        if entity.kind in ("field", "method", "variant"):
            if entity.owner is not None and new in self.members(entity.owner):
                raise RenameError(f"{entity.owner.name!r} already has a member named {new!r}")
        elif entity.kind in ("variable", "parameter"):
            self.check_variable(entity, new, refs)
        else:
            self.check_top_level(entity, new, refs)
        return refs

    def check_variable(self, entity: Entity, new: str, refs: list[tuple[Path, Ref]]) -> None:
        """A variable or parameter must not collide with a name visible in its scope or captured inside it."""
        res = self.resolution(entity.path) if entity.path else None
        info = res.decls.get(entity.key) if res else None
        if res is None or info is None:
            raise RenameError(f"cannot find where {entity.name!r} is declared")
        if new in info.visible:
            raise RenameError(f"{new!r} is already a variable here")
        if entity.kind == "parameter" and entity.owner is not None and isinstance(entity.owner.node, ast.FnDef):
            if any(p.name == new for p in entity.owner.node.params):
                raise RenameError(f"the function already has a parameter named {new!r}")
        for r in res.refs:
            if r.space == "name" and r.name == new and r.entity != entity and r.scope is not None \
                    and info.scope in r.scope.chain():
                raise RenameError(f"{new!r} is already used here and would clash with {entity.name!r}")

    def check_top_level(self, entity: Entity, new: str, refs: list[tuple[Path, Ref]]) -> None:
        """A top-level name must not collide with another name of its module, nor with a variable where it is used."""
        for p in {p for p, _ in refs}:
            fx = self.file(p)
            res = self.resolution(p)
            if fx is None or res is None:
                continue
            if new in fx.top_names:
                raise RenameError(f"{new!r} is already defined in {p.name}")
            for _, r in ((p, r) for q, r in refs if q == p):
                if r.space == "name" and r.scope is not None and r.scope.lookup(new) is not None:
                    raise RenameError(f"{new!r} is already a variable where {entity.name!r} is used ({p.name}:{r.line})")
