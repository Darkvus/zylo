"""`zylo.toml`: project/package manifest, plus parsing of dependency sources."""
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PackageError

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
    name: str
    git: str | None = None   # normalized URL
    ref: str | None = None   # tag, branch or commit
    path: str | None = None  # local directory, relative to the manifest

    @property
    def is_path(self) -> bool:
        return self.path is not None

    def to_toml_value(self) -> str:
        if self.path is not None:
            return "{ path = " + json.dumps(self.path) + " }"
        return json.dumps(self.git + (f"@{self.ref}" if self.ref else ""))


def parse_dependency(name: str, value: object) -> Dependency:
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
    name: str
    version: str = "0.1.0"
    entry: str | None = None
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    path: Path | None = None  # where it was loaded from

    @property
    def root(self) -> Path:
        return self.path.parent if self.path else Path.cwd()


def load_manifest(path: Path) -> Manifest:
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
    return Manifest(check_name(str(pkg["name"])), str(pkg.get("version", "0.1.0")), pkg.get("entry"), deps, path)


def dump_manifest(m: Manifest) -> str:
    lines = ["[package]", f"name = {json.dumps(m.name)}", f"version = {json.dumps(m.version)}"]
    if m.entry:
        lines.append(f"entry = {json.dumps(m.entry)}")
    lines += ["", "[dependencies]"]
    for name in sorted(m.dependencies):
        lines.append(f"{name} = {m.dependencies[name].to_toml_value()}")
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
