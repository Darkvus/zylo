class ZyloError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0, file: str | None = None):
        msg = msg.replace("\u00b7", ".")  # linker-mangled names (`mod·name`) are shown as `mod.name`
        self.msg, self.line, self.col, self.file = msg, line, col, file
        super().__init__(f"{line}:{col}: {msg}" if line else msg)


class BackendError(ZyloError):
    """rustc rejected the generated Rust, or the toolchain is missing: not an error in the user's program."""


class PackageError(BackendError):
    """A problem with a manifest, an environment or a package (not with a Zylo program)."""
