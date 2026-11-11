"""rustc adapter."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from zylo.domain.errors import BackendError


def compile_rust(rs: str, out: Path, overflow_checks: bool = True) -> None:
    """Compile Rust source into an executable with `rustc`.

    Args:
        rs: The Rust source.
        out: Where to write the executable.
        overflow_checks: Whether integer overflow panics.

    Raises:
        BackendError: If `rustc` is missing, or rejects the code (which is a Zylo bug).
    """
    rustc = shutil.which("rustc")
    if not rustc:
        raise BackendError("rustc not found. Install Rust: https://rustup.rs")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.rs"
        src.write_text(rs)
        cmd = [rustc, "-O", "-o", str(out), str(src)]
        if overflow_checks:
            cmd[2:2] = ["-C", "overflow-checks=yes"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BackendError(
                "rustc rejected the generated code. This is a Zylo bug; "
                "use `zylo emit` to see the Rust and report it.\n" + r.stderr.rstrip()
            )

