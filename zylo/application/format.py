"""Use case: format Zylo source files."""

from dataclasses import dataclass, field
from pathlib import Path

from zylo.domain.errors import ZyloError
from zylo.syntax.formatter import format_source

SKIPPED_DIRS = {".zylo-env", ".zylo-build", ".git", "node_modules", "__pycache__"}


@dataclass
class FormatResult:
    """What formatting a set of files did.

    Attributes:
        changed: Files whose text differs from the formatted text (rewritten, or only reported with `check`).
        unchanged: Files already formatted.
        errors: Files that could not be formatted because they have a syntax error.
    """

    changed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    errors: list[ZyloError] = field(default_factory=list)


def format_text(source: str) -> str:
    """Format a source text.

    Args:
        source: The program's text.

    Returns:
        The formatted text.

    Raises:
        ZyloError: If the program has a syntax error.
    """
    return format_source(source)


def find_sources(paths: list[Path]) -> list[Path]:
    """Expand files and directories into the `.zy` files to format.

    Args:
        paths: Files, or directories to search recursively (skipping environments, build and hidden directories).

    Returns:
        The files, sorted and without duplicates.
    """
    found: set[Path] = set()
    for p in paths:
        if p.is_dir():
            found.update(f for f in p.rglob("*.zy") if not SKIPPED_DIRS & set(f.relative_to(p).parts))
        else:
            found.add(p)
    return sorted(found)


def format_paths(paths: list[Path], check: bool = False) -> FormatResult:
    """Format files in place.

    Args:
        paths: Files or directories.
        check: Only report which files would change; write nothing.

    Returns:
        Which files changed, which did not, and which could not be formatted.
    """
    result = FormatResult()
    for path in find_sources(paths):
        try:
            source = path.read_text()
            formatted = format_source(source)
        except ZyloError as e:
            e.file = e.file or str(path)
            result.errors.append(e)
            continue
        except (OSError, UnicodeDecodeError) as e:
            result.errors.append(ZyloError(f"cannot read the file: {e}", file=str(path)))
            continue
        if formatted == source:
            result.unchanged.append(path)
            continue
        result.changed.append(path)
        if not check:
            path.write_text(formatted)
    return result
