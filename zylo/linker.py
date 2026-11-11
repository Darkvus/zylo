"""Module linker: load the entry file and everything it imports, and merge them into one Program.

Every top-level `fn`/`struct` of an imported module is renamed `module·name` and every reference to
it is rewritten, so the merged program goes through the normal checker and code generator. See
docs/packages.md for the language-level rules (resolution order, privacy, cycles...).
"""
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from . import ast
from .checker import BUILTINS, SHADOWABLE
from .errors import ZyloError
from .manifest import package_layout
from .parser import parse
from .types import PRIMS

SEP = "·"  # `·`
RESERVED_TYPES = set(PRIMS) | {"list", "unit", "Result"}


@dataclass
class Module:
    id: str  # "" for the entry module
    path: Path
    program: ast.Program
    defs: dict[str, str] = field(default_factory=dict)  # top-level name -> "fn" | "struct"
    aliases: dict[str, "Module"] = field(default_factory=dict)  # `import a.b` -> {"b": module}
    froms: dict[str, tuple["Module", str]] = field(default_factory=dict)  # local name -> (module, original)

    @property
    def is_entry(self) -> bool:
        return self.id == ""

    @property
    def display(self) -> str:
        return self.id or self.path.stem

    def mangled(self, name: str) -> str:
        return name if self.is_entry else f"{self.id.replace('.', SEP)}{SEP}{name}"


def err(msg: str, node: ast.Node, path: Path | None = None) -> ZyloError:
    return ZyloError(msg, node.line, node.col, node.file or (str(path) if path else None))


class Linker:
    def __init__(self, packages_dir: Path | None = None, trusted: set[str] | list[str] = ()):
        self.packages_dir = packages_dir.resolve() if packages_dir else None
        self.trusted = set(trusted)  # packages the project allows to call Rust (`extern fn`)
        self.by_path: dict[Path, Module] = {}
        self.by_id: dict[str, Path] = {}
        self.order: list[Module] = []  # dependencies before dependents
        self.entry_dir = Path.cwd()

    # ---- public entry point ---------------------------------------------------------------

    def link(self, entry: Path) -> ast.Program:
        self.entry_dir = entry.resolve().parent
        main = self.load(entry, is_entry=True, stack=[])
        body: list[ast.Stmt] = []
        for m in self.order:
            if m is not main:
                body += [s for s in m.program.body if isinstance(s, (ast.FnDef, ast.StructDef))]
        body += [s for s in main.program.body if not isinstance(s, (ast.Import, ast.FromImport))]
        return ast.Program(body, line=1, col=1, file=str(entry))

    # ---- loading --------------------------------------------------------------------------------

    def load(self, path: Path, is_entry: bool, stack: list[Path]) -> Module:
        key = path.resolve()
        if key in self.by_path:
            return self.by_path[key]
        if key in stack:
            chain = " -> ".join(p.stem for p in [*stack[stack.index(key):], key])
            raise ZyloError(f"import cycle: {chain}", file=str(path))
        try:
            src = path.read_text()
        except OSError as e:
            raise ZyloError(f"cannot read {path}: {e.strerror}", file=str(path)) from None
        except UnicodeDecodeError:
            raise ZyloError(f"{path} is not valid UTF-8 text", file=str(path)) from None
        prog = parse(src, str(path))
        mod = Module("" if is_entry else self.module_id(path), path, prog)
        if not is_entry:
            self.register_id(mod, path)
            for s in prog.body:
                if not isinstance(s, (ast.Import, ast.FromImport, ast.FnDef, ast.StructDef)):
                    raise err("library modules can only contain imports, functions and structs", s, path)
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
        other = self.by_id.get(mod.id)
        if other is not None and other != path.resolve():
            raise ZyloError(f"two different modules are both named {mod.id!r}: {other} and {path}", file=str(path))
        self.by_id[mod.id] = path.resolve()

    def module_id(self, path: Path) -> str:
        rp = path.resolve()
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
        parts = dotted.split(".")
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
        for s in mod.program.body:
            if not isinstance(s, (ast.FnDef, ast.StructDef)):
                continue
            kind = "fn" if isinstance(s, ast.FnDef) else "struct"
            if s.name in mod.defs:
                raise err(f"{s.name!r} is already defined in this module", s, mod.path)
            if not mod.is_entry and (s.name in BUILTINS - SHADOWABLE or s.name in RESERVED_TYPES):
                raise err(f"{s.name!r} is a reserved name", s, mod.path)
            mod.defs[s.name] = kind

    def bind_name(self, mod: Module, name: str, node: ast.Node) -> None:
        if name in mod.defs or name in mod.aliases or name in mod.froms:
            raise err(f"{name!r} is already defined in this module", node, mod.path)

    def bind_import(self, mod: Module, s: ast.Import, stack: list[Path]) -> None:
        dep = self.load(self.resolve_path(s.module, mod, s), False, stack)
        alias = s.alias or s.module.rsplit(".", 1)[-1]
        self.bind_name(mod, alias, s)
        mod.aliases[alias] = dep

    def bind_from(self, mod: Module, s: ast.FromImport, stack: list[Path]) -> None:
        dep = self.load(self.resolve_path(s.module, mod, s), False, stack)
        for name, alias in s.names:
            self.export(dep, name, s, mod)  # must exist and be public
            local = alias or name
            self.bind_name(mod, local, s)
            mod.froms[local] = (dep, name)

    def export(self, dep: Module, name: str, node: ast.Node, importer: Module, kind: str | None = None) -> str:
        """Mangled name of `dep.name`, checking that it exists, is public and is the right kind."""
        found = dep.defs.get(name)
        if found is None:
            what = {"struct": "struct", "fn": "function"}.get(kind or "", "function or struct")
            raise err(f"module {dep.display!r} has no {what} {name!r}", node, importer.path)
        if kind and found != kind:
            raise err(f"{name!r} in module {dep.display!r} is a {found}, not a {kind}", node, importer.path)
        if name.startswith("_"):
            raise err(f"{name!r} is private to module {dep.display!r}", node, importer.path)
        return dep.mangled(name)

    # ---- rewriting ---------------------------------------------------------------------------------

    def rewrite(self, mod: Module) -> None:
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
            self.rw(s, mod)
            if isinstance(s, (ast.FnDef, ast.StructDef)):
                if isinstance(s, ast.StructDef) and not mod.is_entry:
                    s.display = s.name
                s.name = mod.mangled(s.name)

    def rw(self, node: ast.Node, mod: Module) -> ast.Node:
        if isinstance(node, ast.MethodCall) and isinstance(node.value, ast.Name) and node.value.id in mod.aliases:
            dep = mod.aliases[node.value.id]
            target = self.export(dep, node.name, node, mod)
            args = [self.rw(a, mod) for a in node.args]
            return ast.Call(target, args, line=node.line, col=node.col, file=node.file)
        if isinstance(node, ast.Attr) and isinstance(node.value, ast.Name) and node.value.id in mod.aliases:
            raise err(f"module {node.value.id!r} has no values; call a function: {node.value.id}.name(...)", node, mod.path)
        if isinstance(node, ast.Name) and node.id in mod.aliases:
            raise err(f"{node.id!r} is a module, not a value", node, mod.path)
        for f in dataclasses.fields(node):
            if f.name in ast._ANNOTATIONS:
                continue
            v = getattr(node, f.name)
            if isinstance(v, ast.Node):
                setattr(node, f.name, self.rw(v, mod))
            elif isinstance(v, list):
                setattr(node, f.name, [self.rw(x, mod) if isinstance(x, ast.Node) else x for x in v])
        if isinstance(node, ast.Call):
            if node.func in mod.defs:
                node.func = mod.mangled(node.func)
            elif node.func in mod.froms:
                dep, orig = mod.froms[node.func]
                node.func = self.export(dep, orig, node, mod)
        elif isinstance(node, ast.TypeRef):
            self.rw_type(node, mod)
        return node

    def rw_type(self, t: ast.TypeRef, mod: Module) -> None:
        if "." in t.name:
            head, _, name = t.name.rpartition(".")
            dep = mod.aliases.get(head)
            if dep is None:
                raise err(f"unknown module {head!r} in type {t.name!r}", t, mod.path)
            t.name = self.export(dep, name, t, mod, kind="struct")
        elif mod.defs.get(t.name) == "struct":
            t.name = mod.mangled(t.name)
        elif t.name in mod.froms:
            dep, orig = mod.froms[t.name]
            t.name = self.export(dep, orig, t, mod, kind="struct")
