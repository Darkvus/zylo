"""Module linker: load the entry file and everything it imports, and merge them into one Program.

Every top-level `fn`/`struct` of an imported module is renamed `module·name` and every reference to
it is rewritten, so the merged program goes through the normal checker and code generator. See
docs/packages.md for the language-level rules (resolution order, privacy, cycles...).
"""
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from zylo.domain import ast
from zylo.domain.builtins import BUILTINS, SHADOWABLE, TYPE_ARG_BUILTINS
from zylo.domain.errors import ZyloError
from zylo.domain.types import PRIMS
from zylo.packaging.manifest import package_layout
from zylo.packaging.stdlib import STD, stdlib_dir, stdlib_module
from zylo.syntax.parser import parse, parse_collect

SEP = "·"  # `·`
RESERVED_TYPES = set(PRIMS) | {"list", "unit", "Result", "Cell"}


@dataclass
class Module:
    """One loaded source file.

    Attributes:
        id: Dotted module id (empty for the entry file).
        path: The file.
        program: Its parsed statements.
        defs: Top-level names it defines, mapped to "fn" or "struct".
        aliases: Names bound by `import a.b [as c]`.
        froms: Names bound by `from a import x [as y]`.
        pubs: The names in `froms` that `pub from` re-exports as part of this module's public surface.
    """
    id: str  # "" for the entry module
    path: Path
    program: ast.Program
    defs: dict[str, str] = field(default_factory=dict)  # top-level name -> "fn" | "struct"
    aliases: dict[str, "Module"] = field(default_factory=dict)  # `import a.b` -> {"b": module}
    froms: dict[str, tuple["Module", str]] = field(default_factory=dict)  # local name -> (module, original)
    pubs: set[str] = field(default_factory=set)  # names re-exported with `pub from`

    @property
    def is_entry(self) -> bool:
        """Whether this is the program's entry file (its names are not mangled)."""
        return self.id == ""

    @property
    def display(self) -> str:
        """The module's name for messages."""
        return self.id or self.path.stem

    def mangled(self, name: str) -> str:
        """Return the linked name of one of this module's definitions.

        Args:
            name: The definition's own name.

        Returns:
            `name` for the entry module, otherwise `module·name`.
        """
        return name if self.is_entry else f"{self.id.replace('.', SEP)}{SEP}{name}"


def local_names(stmt: ast.Stmt) -> set[str]:
    """The variable names a top-level definition (or statement) declares, so they are not mistaken for functions.

    Args:
        stmt: A top-level statement.

    Returns:
        Parameter, assigned, loop, pattern and closure-parameter names found anywhere inside it.
    """
    names: set[str] = set()
    for n in ast.walk(stmt):
        if isinstance(n, ast.Param):
            names.add(n.name)
        elif isinstance(n, ast.For):
            names.update(n.targets or [n.var])
        elif isinstance(n, ast.Assign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, ast.Unpack):
            names.update(t.id for t in n.targets)
        elif isinstance(n, ast.MatchArm):
            names.update(n.binds)
    return names


def err(msg: str, node: ast.Node, path: Path | None = None) -> ZyloError:
    """Build a positioned error.

    Args:
        msg: The message.
        node: The node the problem is about.
        path: File to report when the node carries none.

    Returns:
        The error, ready to be raised.
    """
    return ZyloError(msg, node.line, node.col, node.file or (str(path) if path else None))


class Linker:
    """Resolves `import`s and merges the program and its modules into one.

    Example:
        >>> program = Linker(packages_dir).link(Path("main.zy"))
    """
    def __init__(self, packages_dir: Path | None = None, trusted: set[str] | list[str] = (),
                 overrides: dict[Path, str] | None = None, tolerant: bool = False):
        """Create a linker.

        Args:
            packages_dir: Directory of installed packages (an environment's `packages/`), or None when no environment is active.
            trusted: Package names the project allows to declare `extern fn`.
            overrides: Source text to use instead of the file's contents, by resolved path (an editor's unsaved
                buffers).
            tolerant: Keep going past syntax errors, dropping the statements that do not parse (and, in a library
                module, the statements a library may not hold), as an editor wants on half-typed text.
        """
        self.tolerant = tolerant
        self.packages_dir = packages_dir.resolve() if packages_dir else None
        self.trusted = set(trusted)  # packages the project allows to call Rust (`extern fn`)
        self.overrides = overrides or {}
        self.by_path: dict[Path, Module] = {}
        self.by_id: dict[str, Path] = {}
        self.order: list[Module] = []  # dependencies before dependents
        self.entry_dir = Path.cwd()
        self.locals: set[str] = set()  # variables of the definition being rewritten: they hide functions of the same name
        self.tparams: set[str] = set()  # type parameters of the definition being rewritten: they hide types of the same name

    # ---- public entry point ---------------------------------------------------------------

    def link(self, entry: Path) -> ast.Program:
        """Load the entry file and everything it imports.

        Args:
            entry: The program's entry file.

        Returns:
            One program: library definitions (renamed and rewritten) followed by the entry file's statements.

        Raises:
            ZyloError: On a missing module, an import cycle, a private or unknown name, or an untrusted `extern fn`.
        """
        self.entry_dir = entry.resolve().parent
        main = self.load(entry, is_entry=True, stack=[])
        self.load_json_if_used()
        body: list[ast.Stmt] = []
        for m in self.order:
            if m is not main:
                body += [s for s in m.program.body if isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef))]
        body += [s for s in main.program.body if not isinstance(s, (ast.Import, ast.FromImport))]
        return ast.Program(body, line=1, col=1, file=str(entry))

    def load_json_if_used(self) -> None:
        """Load `std.json` when a program uses the typed JSON built-ins: the code generated for them needs its `Json`."""
        if any(m.id == f"{STD}.json" for m in self.order):
            return
        names = TYPE_ARG_BUILTINS | {"to_json"}
        if any(isinstance(n, ast.Call) and n.func in names for m in self.order for n in ast.walk(m.program)):
            found = stdlib_module([STD, "json"])
            if found is not None:
                self.load(found, False, [])

    # ---- loading --------------------------------------------------------------------------------

    def load(self, path: Path, is_entry: bool, stack: list[Path]) -> Module:
        """Parse one file, load its imports first, then rewrite its references; each file is loaded once."""
        key = path.resolve()
        if key in self.by_path:
            return self.by_path[key]
        if key in stack:
            chain = " -> ".join(p.stem for p in [*stack[stack.index(key):], key])
            raise ZyloError(f"import cycle: {chain}", file=str(path))
        try:
            src = self.overrides[key] if key in self.overrides else path.read_text()
        except OSError as e:
            raise ZyloError(f"cannot read {path}: {e.strerror}", file=str(path)) from None
        except UnicodeDecodeError:
            raise ZyloError(f"{path} is not valid UTF-8 text", file=str(path)) from None
        if self.tolerant:
            prog, _ = parse_collect(src, str(path))
        else:
            prog = parse(src, str(path))
        mod = Module("" if is_entry else self.module_id(path), path, prog)
        if not is_entry:
            self.register_id(mod, path)
            if self.tolerant:
                prog.body = [s for s in prog.body if isinstance(s, (ast.Import, ast.FromImport, ast.FnDef, ast.StructDef, ast.EnumDef))]
            for s in prog.body:
                if not isinstance(s, (ast.Import, ast.FromImport, ast.FnDef, ast.StructDef, ast.EnumDef)):
                    raise err("library modules can only contain imports, functions, structs and enums", s, path)
        self.check_trust(mod)
        self.collect_defs(mod)
        for s in prog.body:
            if isinstance(s, ast.Import):
                self.bind_import(mod, s, stack + [key])
            elif isinstance(s, ast.FromImport):
                self.bind_from(mod, s, stack + [key])
        self.rewrite(mod)
        self.by_path[key] = mod
        self.order.append(mod)
        return mod

    def check_trust(self, mod: Module) -> None:
        """A package from the community is plain Zylo unless the project trusts it with Rust access."""
        if not (self.packages_dir and self.packages_dir in mod.path.resolve().parents):
            return
        pkg = mod.path.resolve().relative_to(self.packages_dir).parts[0]
        for s in mod.program.body:
            if isinstance(s, ast.ExternFn) and pkg not in self.trusted:
                raise err(
                    f"package {pkg!r} declares `extern fn` (it calls Rust directly), but the project does not trust it; "
                    f"add it to `[rust] trusted-packages` in zylo.toml if you do", s, mod.path)

    def register_id(self, mod: Module, path: Path) -> None:
        """Reject two different files that would get the same module id."""
        other = self.by_id.get(mod.id)
        if other is not None and other != path.resolve():
            raise ZyloError(f"two different modules are both named {mod.id!r}: {other} and {path}", file=str(path))
        self.by_id[mod.id] = path.resolve()

    def module_id(self, path: Path) -> str:
        """Compute the dotted id of a module file, relative to the entry directory or to its package."""
        rp = path.resolve()
        if stdlib_dir() in rp.parents:
            return ".".join([STD, *rp.relative_to(stdlib_dir()).with_suffix("").parts])
        if self.packages_dir and self.packages_dir in rp.parents:
            pkg = rp.relative_to(self.packages_dir).parts[0]
            pkg_dir = self.packages_dir / pkg
            entry, src = package_layout(pkg_dir)
            if rp == entry:
                return pkg
            base = src if src in rp.parents else pkg_dir
            return ".".join([pkg, *rp.relative_to(base).with_suffix("").parts])
        if self.entry_dir in rp.parents:
            return ".".join(rp.relative_to(self.entry_dir).with_suffix("").parts)
        return rp.stem

    def resolve_path(self, dotted: str, importer: Module, node: ast.Node) -> Path:
        """Find the file an `import` names.

        Local modules (relative to the importing file) win over installed packages.

        Args:
            dotted: The dotted module name.
            importer: The module containing the import.
            node: The import statement, for error positions.

        Returns:
            The module's file.

        Raises:
            ZyloError: If no file matches.
        """
        parts = dotted.split(".")
        if parts[0] == STD:  # the bundled library wins over local modules and packages: nothing can shadow `std`
            found = stdlib_module(parts)
            if found is None:
                raise err(f"module {dotted!r} not found in the standard library (see docs/stdlib.md)", node, importer.path)
            return found
        cand = importer.path.parent.joinpath(*parts).with_suffix(".zy")  # parts are identifiers: no `..`
        if cand.is_file():
            return cand
        tried = [str(cand)]
        if self.packages_dir and (self.packages_dir / parts[0]).is_dir():
            pkg = self.packages_dir / parts[0]
            entry, src = package_layout(pkg)
            cand = entry if len(parts) == 1 else src.joinpath(*parts[1:]).with_suffix(".zy")
            if cand.is_file():
                return cand
            tried.append(str(cand))
        hint = "" if self.packages_dir else " (no environment is active, so only local modules can be imported)"
        raise err(f"module {dotted!r} not found; looked for {', '.join(tried)}{hint}", node, importer.path)

    # ---- definitions and bindings ------------------------------------------------------------------

    def collect_defs(self, mod: Module) -> None:
        """Record a module's top-level functions and structs, rejecting duplicates and reserved names."""
        for s in mod.program.body:
            if not isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef)):
                continue
            kind = "fn" if isinstance(s, ast.FnDef) else "struct" if isinstance(s, ast.StructDef) else "enum"
            if s.name in mod.defs:
                raise err(f"{s.name!r} is already defined in this module", s, mod.path)
            if not mod.is_entry and (s.name in BUILTINS - SHADOWABLE or s.name in RESERVED_TYPES):
                raise err(f"{s.name!r} is a reserved name", s, mod.path)
            mod.defs[s.name] = kind

    def bind_name(self, mod: Module, name: str, node: ast.Node) -> None:
        """Reject a name that is already used by a definition or another import in the module."""
        if name in mod.defs or name in mod.aliases or name in mod.froms:
            raise err(f"{name!r} is already defined in this module", node, mod.path)

    def bind_import(self, mod: Module, s: ast.Import, stack: list[Path]) -> None:
        """Load the module of an `import` and bind its alias."""
        dep = self.load(self.resolve_path(s.module, mod, s), False, stack)
        alias = s.alias or s.module.rsplit(".", 1)[-1]
        self.bind_name(mod, alias, s)
        mod.aliases[alias] = dep

    def bind_from(self, mod: Module, s: ast.FromImport, stack: list[Path]) -> None:
        """Load the module of a `from ... import` and bind each imported name, which must exist and be public."""
        dep = self.load(self.resolve_path(s.module, mod, s), False, stack)
        if s.pub and mod.is_entry:
            raise err("`pub from` re-exports names of a library module; the entry file has nothing to export", s, mod.path)
        for name, alias in s.names:
            self.export(dep, name, s, mod)  # must exist and be public
            local = alias or name
            self.bind_name(mod, local, s)
            mod.froms[local] = (dep, name)
            if s.pub:
                if local.startswith("_"):
                    raise err(f"cannot re-export {local!r}: names starting with `_` are private", s, mod.path)
                mod.pubs.add(local)

    def find(self, dep: Module, name: str) -> tuple[Module, str, str] | None:
        """Follow re-exports to where a public name is really defined.

        Args:
            dep: The module whose public surface is asked.
            name: A name as `dep` exposes it.

        Returns:
            The defining module, the name there and its kind ("fn", "struct" or "enum"), or None if `dep` has no such name.
            The linked name always comes from the defining module, so a re-exported type keeps one identity.
        """
        if name in dep.defs:
            return dep, name, dep.defs[name]
        if name in dep.pubs:
            src, orig = dep.froms[name]
            return self.find(src, orig)
        return None

    def kind_of(self, dep: Module, name: str) -> str | None:
        """The kind ("fn", "struct" or "enum") of a public name of `dep`, re-exports included."""
        hit = self.find(dep, name)
        return hit[2] if hit else None

    def export(self, dep: Module, name: str, node: ast.Node, importer: Module, kind: str | tuple[str, ...] | None = None) -> str:
        """Mangled name of `dep.name`, checking that it exists, is public and is the right kind."""
        hit = self.find(dep, name)
        found = hit[2] if hit else None
        kinds = (kind,) if isinstance(kind, str) else kind
        if found is None:
            what = "struct or enum" if kinds == ("struct", "enum") else {"struct": "struct", "enum": "enum", "fn": "function"}.get(kind or "", "function, struct or enum")
            raise err(f"module {dep.display!r} has no {what} {name!r}", node, importer.path)
        if kinds and found not in kinds:
            raise err(f"{name!r} in module {dep.display!r} is a {found}, not a {' or '.join(kinds)}", node, importer.path)
        if name.startswith("_"):
            raise err(f"{name!r} is private to module {dep.display!r}", node, importer.path)
        assert hit is not None
        return hit[0].mangled(hit[1])

    # ---- rewriting ---------------------------------------------------------------------------------

    def rewrite(self, mod: Module) -> None:
        """Rename a module's own definitions and rewrite every reference to imported names."""
        for n in ast.walk(mod.program):  # a variable must not hide an imported module
            names = []
            if isinstance(n, ast.Param):
                names = [n.name]
            elif isinstance(n, ast.For):
                names = n.targets or [n.var]
            elif isinstance(n, ast.Assign) and isinstance(n.target, ast.Name):
                names = [n.target.id]
            for name in names:
                if name in mod.aliases:
                    raise err(f"{name!r} is also the name of an imported module", n, mod.path)
        for s in mod.program.body:
            if isinstance(s, (ast.Import, ast.FromImport)):
                continue
            self.locals = local_names(s)
            self.tparams = {p.name for p in getattr(s, "tparams", [])}
            self.rw(s, mod)
            if isinstance(s, (ast.FnDef, ast.StructDef, ast.EnumDef)):
                if isinstance(s, (ast.StructDef, ast.EnumDef)) and not mod.is_entry:
                    s.display = s.name
                s.name = mod.mangled(s.name)

    def rw(self, node: ast.Node, mod: Module) -> ast.Node:
        """Rewrite one node (and its children) for linking.

        Turns `alias.fn(...)` into a call of the mangled name, renames calls and types that refer to the module's own or
        imported definitions, and rejects a module used as a value.

        Args:
            node: The node to rewrite.
            mod: The module it belongs to.

        Returns:
            The node, or its replacement.
        """
        if isinstance(node, ast.CallValue) and isinstance(node.callee, ast.Index):
            named = self.instantiation(node.callee, mod)  # `first[point](xs)`: type arguments after all, not an index
            if named is not None:
                args = [self.rw(a, mod) for a in node.args]
                return ast.Call(named.id, args, targs=named.targs, line=node.line, col=node.col, file=node.file)
        if isinstance(node, ast.Index):
            named = self.instantiation(node, mod)  # `g = first[point]`
            if named is not None:
                return named
        if isinstance(node, ast.MethodCall) and isinstance(node.value, ast.Name) and node.value.id in mod.aliases:
            dep = mod.aliases[node.value.id]
            target = self.export(dep, node.name, node, mod)
            args = [self.rw(a, mod) for a in node.args]
            named = [self.rw(n, mod) for n in node.named]
            targs = [self.rw(t, mod) for t in node.targs]
            return ast.Call(target, args, named=named, targs=targs, line=node.line, col=node.col, file=node.file)
        if isinstance(node, ast.Attr) and isinstance(node.value, ast.Name) and node.value.id in mod.aliases:
            dep = mod.aliases[node.value.id]
            if self.kind_of(dep, node.name) == "enum":  # `module.Enum` in `module.Enum.Variant`
                return ast.Name(self.export(dep, node.name, node, mod, kind="enum"), targs=[self.rw(t, mod) for t in node.targs],
                                line=node.line, col=node.col, file=node.file)
            if self.kind_of(dep, node.name) == "fn":  # `module.function` used as a value
                return ast.Name(self.export(dep, node.name, node, mod, kind="fn"), targs=[self.rw(t, mod) for t in node.targs],
                                line=node.line, col=node.col, file=node.file)
            raise err(f"module {node.value.id!r} has no values; call a function: {node.value.id}.name(...)", node, mod.path)
        if isinstance(node, ast.Name) and node.id in mod.aliases:
            raise err(f"{node.id!r} is a module, not a value", node, mod.path)
        if isinstance(node, ast.Name):  # an enum of this module or imported by name, used as `Enum.Variant`
            node.targs = [self.rw(t, mod) for t in node.targs]
            if mod.defs.get(node.id) == "enum":
                node.id = mod.mangled(node.id)
            elif node.id in mod.froms and self.kind_of(*mod.froms[node.id]) == "enum":
                dep, orig = mod.froms[node.id]
                node.id = self.export(dep, orig, node, mod, kind="enum")
            elif node.id not in self.locals:  # a function of this module, or imported by name, used as a value
                if mod.defs.get(node.id) == "fn":
                    node.id = mod.mangled(node.id)
                elif node.id in mod.froms and self.kind_of(*mod.froms[node.id]) == "fn":
                    dep, orig = mod.froms[node.id]
                    node.id = self.export(dep, orig, node, mod, kind="fn")
            return node
        for f in dataclasses.fields(node):
            if f.name in ast._ANNOTATIONS:
                continue
            v = getattr(node, f.name)
            if isinstance(v, ast.Node):
                setattr(node, f.name, self.rw(v, mod))
            elif isinstance(v, list):
                setattr(node, f.name, [self.rw(x, mod) if isinstance(x, ast.Node) else x for x in v])
        if isinstance(node, ast.Call) and node.func not in self.locals:
            if node.func in mod.defs:
                node.func = mod.mangled(node.func)
            elif node.func in mod.froms:
                dep, orig = mod.froms[node.func]
                node.func = self.export(dep, orig, node, mod)
        elif isinstance(node, ast.TypeRef):
            self.rw_type(node, mod)
        return node

    def instantiation(self, node: ast.Index, mod: Module) -> ast.Name | None:
        """Read `fn_name[type, ...]` as a generic function with written type arguments, when `fn_name` is a function.

        The parser cannot tell `first[point]` from an index (it knows nothing about imports); here the name is known. A
        function cannot be indexed, so whenever the name is a function the brackets are type arguments.

        Returns:
            A name carrying the type arguments (already rewritten), or None when this is an ordinary index.
        """
        base = node.value
        if isinstance(base, ast.Name) and base.id not in self.locals:
            if mod.defs.get(base.id) == "fn":
                target = mod.mangled(base.id)
            elif base.id in mod.froms and self.kind_of(*mod.froms[base.id]) == "fn":
                dep, orig = mod.froms[base.id]
                target = self.export(dep, orig, base, mod, kind="fn")
            else:
                return None
        elif (isinstance(base, ast.Attr) and isinstance(base.value, ast.Name) and base.value.id in mod.aliases
              and base.value.id not in self.locals and self.kind_of(mod.aliases[base.value.id], base.name) == "fn"):
            target = self.export(mod.aliases[base.value.id], base.name, base, mod, kind="fn")
        else:
            return None
        targs = [self.rw(t, mod) for t in self.type_args_of(node.index, mod)]
        return ast.Name(target, targs=targs, line=node.line, col=node.col, file=node.file)

    def type_args_of(self, e: ast.Expr, mod: Module) -> list[ast.TypeRef]:
        """Read the expression between brackets as a list of types (`point`, `list[point]`, `mod.point`, `a, b`)."""
        if isinstance(e, ast.TupleLit) and e.bare:
            return [self.type_of(x, mod) for x in e.items]
        return [self.type_of(e, mod)]

    def type_of(self, e: ast.Expr, mod: Module) -> ast.TypeRef:
        """Read an expression as a written type, or fail with a message that says what a type argument looks like."""
        pos = {"line": e.line, "col": e.col, "file": e.file}
        if isinstance(e, ast.Name) and not e.targs:
            return ast.TypeRef(e.id, [], **pos)
        if isinstance(e, ast.Attr) and isinstance(e.value, ast.Name) and not e.targs:
            return ast.TypeRef(f"{e.value.id}.{e.name}", [], **pos)
        if isinstance(e, ast.Index):
            base = self.type_of(e.value, mod)
            if not base.args:
                return ast.TypeRef(base.name, self.type_args_of(e.index, mod), **pos)
        if isinstance(e, ast.Try):
            return ast.TypeRef("?", [self.type_of(e.value, mod)], **pos)
        raise err("these brackets name a function, so they must hold type arguments (`first[int]`, `first[list[Point]]`)",
                  e, mod.path)

    def rw_type(self, t: ast.TypeRef, mod: Module) -> None:
        """Rewrite a type name that refers to a struct of this or an imported module."""
        if t.name in self.tparams and not t.args:
            return  # a type parameter of the generic definition being rewritten
        if "." in t.name:
            head, _, name = t.name.rpartition(".")
            dep = mod.aliases.get(head)
            if dep is None:
                raise err(f"unknown module {head!r} in type {t.name!r}", t, mod.path)
            t.name = self.export(dep, name, t, mod, kind=("struct", "enum"))
        elif mod.defs.get(t.name) in ("struct", "enum"):
            t.name = mod.mangled(t.name)
        elif t.name in mod.froms:
            dep, orig = mod.froms[t.name]
            t.name = self.export(dep, orig, t, mod, kind=("struct", "enum"))
