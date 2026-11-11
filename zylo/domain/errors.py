"""Errors Zylo can report.

`ZyloError` is a mistake in the user's program and carries its position. `BackendError` and its subclass
`PackageError` are problems outside the program (a missing toolchain, a bad manifest) and carry no position.
"""

class ZyloError(Exception):
    """A mistake in the user's program (syntax, types, imports...).

    Attributes:
        msg: The message, without the position.
        line: 1-based line, or 0 when unknown.
        col: 1-based column.
        file: The source file, when different from the one being compiled.
    """
    def __init__(self, msg: str, line: int = 0, col: int = 0, file: str | None = None):
        """Create an error.

        Args:
            msg: The message. Linker-mangled names (`module·name`) are shown as `module.name`.
            line: 1-based line of the problem, or 0 when it has no position.
            col: 1-based column.
            file: The source file, when it is not the one being compiled.
        """
        msg = msg.replace("\u00b7", ".")  # linker-mangled names (`mod·name`) are shown as `mod.name`
        self.msg, self.line, self.col, self.file = msg, line, col, file
        super().__init__(f"{line}:{col}: {msg}" if line else msg)


class BackendError(ZyloError):
    """rustc rejected the generated Rust, or the toolchain is missing: not an error in the user's program."""


class PackageError(BackendError):
    """A problem with a manifest, an environment or a package (not with a Zylo program)."""
