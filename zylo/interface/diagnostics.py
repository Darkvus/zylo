"""Terminal error format: `file:line:col: error: message` + a code snippet."""
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
