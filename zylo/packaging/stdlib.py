"""Where the bundled standard library lives.

The `.zy` files sit in the `zylo/stdlib` package next to this layer, in a git checkout, in an installed wheel and
inside the PyInstaller standalone binary alike (which unpacks the data files beside the modules).
"""
import tomllib
from pathlib import Path

from zylo.domain.errors import PackageError
from zylo.packaging.rust_deps import validate_rust_dep

STD = "std"  # the reserved first component of a standard library import


def stdlib_dir() -> Path:
    """Return the directory holding the standard library's `.zy` files.

    Returns:
        The `zylo/stdlib` directory.
    """
    return Path(__file__).resolve().parent.parent / "stdlib"


def stdlib_module(parts: list[str]) -> Path | None:
    """Find the file of a `std.*` module.

    Args:
        parts: The dotted module name split on dots, starting with `std`.

    Returns:
        The module's file, or None when the standard library has no such module.
    """
    if parts[0] != STD or len(parts) < 2:
        return None
    cand = stdlib_dir().joinpath(*parts[1:]).with_suffix(".zy")
    return cand if cand.is_file() else None


def stdlib_modules() -> list[str]:
    """List the dotted names of every standard library module.

    Returns:
        Names such as `std.json`, sorted.
    """
    root = stdlib_dir()
    return sorted(".".join([STD, *p.relative_to(root).with_suffix("").parts]) for p in root.rglob("*.zy"))


def stdlib_support(modules: list[str]) -> tuple[str, dict[str, object]]:
    """Collect the Rust a set of standard library modules brings with them.

    A module `std.x` may ship a sidecar `x.rs` (Rust support code its `extern fn` templates call) and `x.toml` (a
    `[rust-dependencies]` table). Both are trusted like the module itself. Only the modules a program links count.

    Args:
        modules: Dotted names of the linked standard library modules, such as `std.http`.

    Returns:
        The Rust source to append to the program, and the crates to build it with (empty for most programs, which
        then keep the direct `rustc` path).

    Raises:
        PackageError: If a sidecar `.toml` is invalid.
    """
    rust: list[str] = []
    crates: dict[str, object] = {}
    for dotted in sorted(set(modules)):
        parts = dotted.split(".")
        if parts[0] != STD or len(parts) < 2:
            continue
        base = stdlib_dir().joinpath(*parts[1:])
        if base.with_suffix(".rs").is_file():
            rust.append(base.with_suffix(".rs").read_text())
        if base.with_suffix(".toml").is_file():
            try:
                table = tomllib.loads(base.with_suffix(".toml").read_text()).get("rust-dependencies", {})
            except tomllib.TOMLDecodeError as e:
                raise PackageError(f"{dotted}: invalid sidecar manifest: {e}") from None
            for name, spec in table.items():
                crates[name] = validate_rust_dep(name, spec, allow_path=False)
    return "\n".join(rust), crates
