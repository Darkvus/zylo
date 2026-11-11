"""Rust dependencies: validation, a generated Cargo project, and building it with `cargo`."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import BackendError, PackageError

CRATE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")
ALLOWED_KEYS = {"version", "features", "default-features", "path", "git", "branch", "tag", "rev", "package"}
BUILD_DIR = ".zylo-build"


def validate_rust_dep(name: str, spec: object, allow_path: bool = True) -> object:
    """Check one `[rust-dependencies]` entry (same syntax as Cargo) and return it."""
    if not CRATE_RE.match(name):
        raise PackageError(f"invalid crate name {name!r}")
    if isinstance(spec, str):
        if re.search(r"[\r\n]", spec):
            raise PackageError(f"invalid version for crate {name!r}")
        return spec
    if not isinstance(spec, dict):
        raise PackageError(f"crate {name!r}: expected a version string or a table")
    extra = set(spec) - ALLOWED_KEYS
    if extra:
        raise PackageError(f"crate {name!r}: unsupported key(s) {', '.join(sorted(extra))} (allowed: {', '.join(sorted(ALLOWED_KEYS))})")
    if "path" in spec and not allow_path:
        raise PackageError(f"crate {name!r}: a package may not use path dependencies; only your own zylo.toml may")
    for k, v in spec.items():
        if k == "features":
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise PackageError(f"crate {name!r}: features must be a list of strings")
        elif k == "default-features":
            if not isinstance(v, bool):
                raise PackageError(f"crate {name!r}: default-features must be true or false")
        elif not isinstance(v, str) or re.search(r"[\r\n]", v):
            raise PackageError(f"crate {name!r}: {k} must be a string")
    return spec


def toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k if re.fullmatch(r'[A-Za-z0-9_-]+', k) else json.dumps(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v))


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
    r = subprocess.run(cmd, capture_output=True, text=True, env={**__import__("os").environ, "CARGO_TERM_COLOR": "never"})
    if r.returncode:
        raise BackendError("cargo could not build the program" + (" (offline)" if offline else "") + ":\n" + (r.stderr or r.stdout).rstrip())
    exe = d / "target" / "release" / ("zylo_program.exe" if sys.platform == "win32" else "zylo_program")
    shutil.copy2(exe, out)
