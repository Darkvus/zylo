"""`zylo.toml`: project/package manifest, plus parsing of dependency sources."""
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from zylo.domain.errors import PackageError
from zylo.packaging.rust_deps import toml_value, validate_rust_dep

MANIFEST = "zylo.toml"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")
SCHEMES = ("https://", "http://", "ssh://", "git://", "file://")


def check_name(name: str, what: str = "package name") -> str:
    """Names become directory names inside the environment, so they must be plain identifiers."""
    if not NAME_RE.match(name):
        raise PackageError(f"invalid {what} {name!r}: use letters, digits and underscores, not starting with a digit")
    return name


def normalize_url(source: str) -> str:
    """`host/user/repo` -> `https://host/user/repo.git`; full URLs are kept."""
    if not source or source.startswith("-") or re.search(r"\s", source):
        raise PackageError(f"invalid git source {source!r}")
    if source.startswith(SCHEMES) or re.match(r"^[\w.\-]+@[\w.\-]+:[\w./\-]+$", source):
        return source
    parts = source.split("/")
    if len(parts) >= 3 and "." in parts[0] and all(parts):
        return "https://" + (source if source.endswith(".git") else source + ".git")
    raise PackageError(
        f"cannot understand source {source!r}: use host/user/repo (e.g. github.com/ana/strutil) or a full git URL"
    )


def split_ref(spec: str) -> tuple[str, str | None]:
    """`github.com/u/r@v1.2` -> (`github.com/u/r`, `v1.2`). An `@` before the last `/` or `:` is not a ref."""
    head, sep, tail = spec.rpartition("@")
    if sep and head and "/" not in tail and ":" not in tail:
        if not REF_RE.match(tail):
            raise PackageError(f"invalid ref {tail!r} in {spec!r}")
        return head, tail
    return spec, None


@dataclass
class Dependency:
    """A package a project depends on.

    Attributes:
        name: The name it is imported by.
        git: Normalized git URL, for git dependencies.
        ref: Tag, branch or commit.
        path: Local directory, for path dependencies.
    """
    name: str
    git: str | None = None   # normalized URL
    ref: str | None = None   # tag, branch or commit
    path: str | None = None  # local directory, relative to the manifest

    @property
    def is_path(self) -> bool:
        """Whether this is a local-directory dependency."""
        return self.path is not None

    def to_toml_value(self) -> str:
        """Render the dependency as the value in `[dependencies]`."""
        if self.path is not None:
            return "{ path = " + json.dumps(self.path) + " }"
        return json.dumps(self.git + (f"@{self.ref}" if self.ref else ""))


def parse_dependency(name: str, value: object) -> Dependency:
    """Read one entry of `[dependencies]`.

    Args:
        name: The dependency's name.
        value: A `"host/user/repo@ref"` string or a table with `git`/`ref` or `path`.

    Returns:
        The dependency.

    Raises:
        PackageError: If the name or the value is invalid.
    """
    check_name(name, "dependency name")
    if isinstance(value, str):
        src, ref = split_ref(value)
        return Dependency(name, git=normalize_url(src), ref=ref)
    if isinstance(value, dict):
        if "path" in value:
            return Dependency(name, path=str(value["path"]))
        if "git" in value:
            ref = value.get("ref")
            if ref is not None and not REF_RE.match(str(ref)):
                raise PackageError(f"invalid ref {ref!r} for dependency {name!r}")
            return Dependency(name, git=normalize_url(str(value["git"])), ref=ref)
    raise PackageError(f"dependency {name!r}: expected \"host/user/repo@ref\", {{ git = ... }} or {{ path = ... }}")


@dataclass
class Manifest:
    """A `zylo.toml`.

    Attributes:
        name: Package name.
        version: Package version.
        entry: Entry file for libraries, relative to the manifest.
        dependencies: Zylo dependencies by name.
        path: Where it was loaded from.
        rust_dependencies: Cargo-style crate requirements.
        trusted_packages: Packages allowed to call Rust.
        line_length: `[format] line-length` for `zylo fmt`, or None for the default (kept when the file is rewritten).
    """
    name: str
    version: str = "0.1.0"
    entry: str | None = None
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    path: Path | None = None  # where it was loaded from
    rust_dependencies: dict[str, object] = field(default_factory=dict)  # Cargo-style, see docs/rust-interop.md
    trusted_packages: list[str] = field(default_factory=list)  # packages allowed to call Rust
    line_length: int | None = None  # `[format] line-length`; validated by `zylo fmt`

    @property
    def root(self) -> Path:
        """The directory containing the manifest."""
        return self.path.parent if self.path else Path.cwd()


def load_manifest(path: Path) -> Manifest:
    """Read and validate a manifest.

    Args:
        path: The `zylo.toml` file.

    Returns:
        The manifest.

    Raises:
        PackageError: If the file is missing, is not valid TOML, or holds an invalid name or dependency.
    """
    try:
        data = tomllib.loads(path.read_text())
    except OSError as e:
        raise PackageError(f"cannot read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise PackageError(f"{path}: invalid TOML: {e}") from None
    pkg = data.get("package")
    if not isinstance(pkg, dict) or "name" not in pkg:
        raise PackageError(f"{path}: missing [package] name")
    deps = {n: parse_dependency(n, v) for n, v in (data.get("dependencies") or {}).items()}
    rust = {n: validate_rust_dep(n, v) for n, v in (data.get("rust-dependencies") or {}).items()}
    trusted = [check_name(str(t), "trusted package name") for t in (data.get("rust") or {}).get("trusted-packages", [])]
    width = (data.get("format") or {}).get("line-length")
    return Manifest(check_name(str(pkg["name"])), str(pkg.get("version", "0.1.0")), pkg.get("entry"), deps, path, rust, trusted,
                    width if isinstance(width, int) and not isinstance(width, bool) else None)


def dump_manifest(m: Manifest) -> str:
    """Render a manifest as TOML.

    Args:
        m: The manifest.

    Returns:
        TOML text (comments in a previous file are not preserved).
    """
    lines = ["[package]", f"name = {json.dumps(m.name)}", f"version = {json.dumps(m.version)}"]
    if m.entry:
        lines.append(f"entry = {json.dumps(m.entry)}")
    lines += ["", "[dependencies]"]
    for name in sorted(m.dependencies):
        lines.append(f"{name} = {m.dependencies[name].to_toml_value()}")
    if m.rust_dependencies:
        lines += ["", "[rust-dependencies]"] + [f"{n} = {toml_value(v)}" for n, v in sorted(m.rust_dependencies.items())]
    if m.trusted_packages:
        lines += ["", "[rust]", f"trusted-packages = {toml_value(sorted(m.trusted_packages))}"]
    if m.line_length is not None:
        lines += ["", "[format]", f"line-length = {m.line_length}"]
    return "\n".join(lines) + "\n"


def find_manifest(start: Path) -> Path | None:
    """Nearest `zylo.toml` at or above `start`."""
    cur = start.resolve()
    for d in [cur, *cur.parents]:
        if (d / MANIFEST).is_file():
            return d / MANIFEST
    return None


def package_layout(pkg_dir: Path) -> tuple[Path, Path]:
    """(entry file, source directory) of a package directory."""
    entry = None
    mf = pkg_dir / MANIFEST
    if mf.is_file():
        try:
            declared = (tomllib.loads(mf.read_text()).get("package") or {}).get("entry")
        except (OSError, tomllib.TOMLDecodeError):
            declared = None
        if declared:
            cand = (pkg_dir / declared).resolve()
            if pkg_dir.resolve() in cand.parents:  # the entry must stay inside the package
                entry = cand
    if entry is None:
        for rel in ("src/lib.zy", "lib.zy", f"{pkg_dir.name}.zy"):
            if (pkg_dir / rel).is_file():
                entry = (pkg_dir / rel).resolve()
                break
    if entry is None:
        entry = (pkg_dir / "src" / "lib.zy").resolve()
    return entry, entry.parent
