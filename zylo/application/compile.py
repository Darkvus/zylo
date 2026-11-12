"""Compile use case."""

from pathlib import Path

from zylo.domain import ast
from zylo.domain.errors import PackageError, ZyloError
from zylo.generation.codegen import generate
from zylo.infrastructure import cargo, rustc
from zylo.packaging import env as envmod
from zylo.packaging.manifest import MANIFEST, find_manifest, load_manifest
from zylo.packaging.rust_deps import validate_rust_dep
from zylo.packaging.stdlib import stdlib_support
from zylo.semantics.checker import check, check_all
from zylo.semantics.linker import Linker
from zylo.syntax.parser import parse, parse_collect


def transpile_source(src: str) -> str:
    """Compile a single self-contained source string (no imports)."""
    prog = parse(src)
    if any(isinstance(s, (ast.Import, ast.FromImport)) for s in prog.body):
        raise ZyloError("imports need a source file; compile a file instead of a string")
    return generate(check(prog))


def project_context(path: Path, env: str | Path | None = None):
    """(manifest of the project containing `path`, or None; the active environment, or None)."""
    active = envmod.discover(path.resolve().parent, env)
    mf = find_manifest(path.resolve().parent)
    return (load_manifest(mf) if mf else None), active


def rust_crates(manifest, active) -> dict[str, object]:
    """Crates to build with: the project's, plus those of the packages it trusts (conflicts are an error)."""
    crates = dict(manifest.rust_dependencies) if manifest else {}
    for pkg in (manifest.trusted_packages if manifest else []):
        pm = active.packages_dir / pkg / MANIFEST if active else None
        if pm is None or not pm.is_file():
            continue
        for name, spec in load_manifest(pm).rust_dependencies.items():
            validate_rust_dep(name, spec, allow_path=False)
            if name in crates and crates[name] != spec:
                raise PackageError(f"conflicting requirements for crate {name!r}: {crates[name]!r} and {spec!r} (from package {pkg!r})")
            crates[name] = spec
    return crates


def load_program(path: Path, env: str | Path | None = None) -> ast.Program:
    """Parse a file and everything it imports (using the active environment, if any)."""
    manifest, active = project_context(path, env)
    trusted = manifest.trusted_packages if manifest else []
    return Linker(active.packages_dir if active else None, trusted).link(path)


def compile_file(path: Path, env: str | Path | None = None):
    """(Rust source, crates it needs, the project manifest or None).

    A standard library module that needs Rust support code and crates (`std.http`) adds both here, so the program
    is built with Cargo only when it imports such a module.
    """
    manifest, active = project_context(path, env)
    trusted = manifest.trusted_packages if manifest else []
    linker = Linker(active.packages_dir if active else None, trusted)
    prog = linker.link(path)
    rs = generate(check(prog))
    crates = rust_crates(manifest, active)
    support, std_crates = stdlib_support([m.id for m in linker.order])
    if support:
        rs += "\n" + support
    for name, spec in std_crates.items():
        if name in crates and crates[name] != spec:
            raise PackageError(f"conflicting requirements for crate {name!r}: your zylo.toml asks for {crates[name]!r} "
                               f"but the standard library needs {spec!r}")
        crates[name] = spec
    return rs, crates, manifest


def transpile(path: Path, env: str | Path | None = None) -> str:
    """Compile a file to Rust source.

    Args:
        path: The entry file.
        env: Environment directory to use, or None to discover one.

    Returns:
        The generated Rust source.
    """
    return compile_file(path, env)[0]


def build_executable(rs: str, crates: dict[str, object], manifest, out: Path, checks: bool, offline: bool) -> None:
    """`rustc` when there are no Rust dependencies (fast), otherwise a Cargo project next to zylo.toml (or in the user cache)."""
    if crates:
        cargo.build(rs, crates, out, manifest.root if manifest else cargo.cache_dir(crates), checks, offline)
    else:
        rustc.compile_rust(rs, out, checks)



def diagnose_source(source: str, path: Path, env: str | Path | None = None) -> list[ZyloError]:
    """Find every mistake in a program, as an editor or `zylo check` wants them.

    Syntax errors come first and stop the run (type checking a program that did not parse only produces noise).
    Otherwise the program and its imports are linked and every type error is reported.

    Args:
        source: The entry file's text (it may differ from what is on disk, as in an editor with unsaved changes).
        path: The entry file's path, used to find its imports and its project.
        env: Environment directory to use, or None to discover one.

    Returns:
        The mistakes, in source order; empty when the program is correct.

    Raises:
        PackageError: If the project's manifest or environment is invalid.
    """
    _, syntax_errors = parse_collect(source, str(path))
    if syntax_errors:
        return syntax_errors
    manifest, active = project_context(path, env)
    trusted = manifest.trusted_packages if manifest else []
    linker = Linker(active.packages_dir if active else None, trusted, overrides={path.resolve(): source})
    try:
        program = linker.link(path)
    except ZyloError as e:
        return [e]
    return check_all(program)
