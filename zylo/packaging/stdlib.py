"""Where the bundled standard library lives.

The `.zy` files sit in the `zylo/stdlib` package next to this layer, in a git checkout, in an installed wheel and
inside the PyInstaller standalone binary alike (which unpacks the data files beside the modules).
"""
from pathlib import Path

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
