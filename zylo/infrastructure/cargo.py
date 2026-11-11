"""Cargo adapter: builds a program that uses Rust crates through a generated Cargo project."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from zylo.domain.errors import BackendError
from zylo.packaging.rust_deps import toml_value

BUILD_DIR = ".zylo-build"


def cargo_toml(crates: dict[str, object], overflow_checks: bool, base: Path) -> str:
    """Cargo.toml of the generated project; relative `path` dependencies are resolved against `base`."""
    lines = ['[package]', 'name = "zylo_program"', 'version = "0.0.0"', 'edition = "2021"', '', '[dependencies]']
    for name in sorted(crates):
        spec = crates[name]
        if isinstance(spec, dict) and "path" in spec:
            spec = {**spec, "path": str((base / spec["path"]).resolve())}
        lines.append(f"{name} = {toml_value(spec)}")
    lines += ['', '[profile.release]', 'opt-level = 3', f'overflow-checks = {"true" if overflow_checks else "false"}',
              '', '[workspace]']  # an empty workspace keeps Cargo from adopting a parent project
    return "\n".join(lines) + "\n"


def build(rs: str, crates: dict[str, object], out: Path, project: Path, overflow_checks: bool = True,
          offline: bool = False) -> None:
    """Build the program with Cargo in `<project>/.zylo-build` (kept between builds so crates stay cached)."""
    cargo = shutil.which("cargo")
    if not cargo:
        raise BackendError("cargo not found. Programs that use Rust crates need Cargo; install Rust: https://rustup.rs")
    d = project / BUILD_DIR
    (d / "src").mkdir(parents=True, exist_ok=True)
    toml = cargo_toml(crates, overflow_checks, project)
    if not (d / "Cargo.toml").exists() or (d / "Cargo.toml").read_text() != toml:
        (d / "Cargo.toml").write_text(toml)
    (d / "src" / "main.rs").write_text(rs)
    cmd = [cargo, "build", "--release", "--quiet", "--manifest-path", str(d / "Cargo.toml")] + (["--offline"] if offline else [])
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "CARGO_TERM_COLOR": "never"})
    if r.returncode:
        raise BackendError("cargo could not build the program" + (" (offline)" if offline else "") + ":\n" + (r.stderr or r.stdout).rstrip())
    exe = d / "target" / "release" / ("zylo_program.exe" if sys.platform == "win32" else "zylo_program")
    shutil.copy2(exe, out)
