"""Rust dependency specs."""

import json
import re

from zylo.domain.errors import PackageError

CRATE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


ALLOWED_KEYS = {"version", "features", "default-features", "path", "git", "branch", "tag", "rev", "package"}


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
    """Render a Python value as TOML.

    Args:
        v: A string, bool, list or dict.

    Returns:
        TOML text for it (dicts become inline tables).
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k if re.fullmatch(r'[A-Za-z0-9_-]+', k) else json.dumps(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v))

