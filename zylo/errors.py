class ZyloError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        self.msg, self.line, self.col = msg, line, col
        super().__init__(f"{line}:{col}: {msg}" if line else msg)


class BackendError(ZyloError):
    """rustc rejected the generated Rust, or the toolchain is missing: not an error in the user's program."""
