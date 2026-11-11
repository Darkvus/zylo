"""Terminal error format: `file:line:col: error: message` + a code snippet."""
from pathlib import Path

from zylo.domain.errors import BackendError, ZyloError

RED, BOLD, RESET = "\033[1;31m", "\033[1m", "\033[0m"


def format_error(err: ZyloError, source: str | None, filename: str, color: bool = False) -> str:
    """Format an error for a terminal.

    Args:
        err: The error.
        source: Text of the file the error is in, for the code snippet, or None.
        filename: The file's name for the heading.
        color: Whether to use ANSI colours.

    Returns:
        `file:line:col: error: message` followed by the source line and a caret.
    """
    label = f"{RED}error{RESET}{BOLD}" if color else "error"
    end = RESET if color else ""

    if isinstance(err, BackendError):
        return f"{filename}: {label}: {err.msg}{end}"

    where = f"{filename}:{err.line}:{err.col}" if err.line else filename
    out = [f"{where}: {label}: {err.msg}{end}"]

    lines = source.splitlines() if source else []
    if err.line and 1 <= err.line <= len(lines):
        text = lines[err.line - 1]
        gutter = str(err.line)
        pad = " " * len(gutter)
        caret = " " * max(err.col - 1, 0) + "^"
        if color:
            caret = f"{RED}{caret}{RESET}"
        out.append(f"{pad} |")
        out.append(f"{gutter} | {text}")
        out.append(f"{pad} | {caret}")
    return "\n".join(out)


def read_text_or_none(path: Path) -> str | None:
    """Read a text file, returning None when it cannot be read; used to show code snippets in errors."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def format_errors(errors: list[ZyloError], filename: str, source: str | None, color: bool = False, limit: int = 20) -> str:
    """Format several errors, each with its own file and snippet, and a closing count.

    Args:
        errors: The errors, in the order to show them.
        filename: The file being compiled; errors without a file of their own belong to it.
        source: That file's text.
        color: Whether to use ANSI colours.
        limit: How many errors to show before summarising the rest.

    Returns:
        The text to print: one block per error separated by blank lines, and, when there are several, a final line
        `error: aborting due to N previous errors`.
    """
    blocks = []
    for e in errors[:limit]:
        shown = e.file or filename
        text = source if shown == filename else read_text_or_none(Path(shown))
        blocks.append(format_error(e, text, shown, color))
    if len(errors) > limit:
        blocks.append(f"... and {len(errors) - limit} more")
    if len(errors) > 1:
        label = f"{RED}error{RESET}{BOLD}" if color else "error"
        blocks.append(f"{label}: aborting due to {len(errors)} previous errors{RESET if color else ''}")
    return "\n\n".join(blocks)
