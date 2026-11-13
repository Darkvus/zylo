"""Use case: format Zylo source files."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from zylo.domain.errors import ZyloError
from zylo.packaging.manifest import find_manifest
from zylo.syntax.formatter import DEFAULT_LINE_LENGTH, format_source

MIN_LINE_LENGTH = 10
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


def configured_line_length(start: Path) -> int:
    """The line length set by `[format] line-length` in the nearest `zylo.toml`, or the default (88).

    Args:
        start: A file or directory; the search for `zylo.toml` starts there and goes up.

    Returns:
        The line length.

    Raises:
        ZyloError: If the setting is not a whole number of at least 10.
    """
    manifest = find_manifest(start if start.is_dir() else start.parent)
    if manifest is None:
        return DEFAULT_LINE_LENGTH
    try:
        value = (tomllib.loads(manifest.read_text()).get("format") or {}).get("line-length", DEFAULT_LINE_LENGTH)
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_LINE_LENGTH  # a broken manifest is reported by the commands that need it
    return check_line_length(value, f"{manifest}: [format] line-length")


def check_line_length(value: object, what: str) -> int:
    """Validate a line length.

    Raises:
        ZyloError: If it is not an integer of at least 10 (shorter lines cannot hold even a small call).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < MIN_LINE_LENGTH:
        raise ZyloError(f"{what} must be a whole number of at least {MIN_LINE_LENGTH}, not {value!r}")
    return value


def format_text(source: str, line_length: int = DEFAULT_LINE_LENGTH) -> str:
    """Format a source text.

    Args:
        source: The program's text.
        line_length: Lines longer than this are wrapped.

    Returns:
        The formatted text.

    Raises:
        ZyloError: If the program has a syntax error.
    """
    return format_source(source, line_length)


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


def format_paths(paths: list[Path], check: bool = False, line_length: int | None = None) -> FormatResult:
    """Format files in place.

    Args:
        paths: Files or directories.
        check: Only report which files would change; write nothing.
        line_length: The line length for every file; by default each file uses the `[format] line-length` of its
            nearest `zylo.toml`, or 88.

    Returns:
        Which files changed, which did not, and which could not be formatted.
    """
    result = FormatResult()
    for path in find_sources(paths):
        try:
            source = path.read_text()
            formatted = format_source(source, line_length or configured_line_length(path))
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
