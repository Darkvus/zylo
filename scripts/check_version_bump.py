"""Fail unless this branch's version is greater than the base branch's (one pull request = one version)."""
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def parse(v: str) -> tuple[int, ...]:
    """Parse a `MAJOR.MINOR.PATCH` version.

    Args:
        v: The version text.

    Returns:
        The three numbers.

    Raises:
        SystemExit: If the text is not a valid version.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        raise SystemExit(f"version {v!r} is not MAJOR.MINOR.PATCH")
    return tuple(int(x) for x in v.split("."))


def main(base: str) -> int:
    """Compare this branch's version with the base branch's.

    Args:
        base: A git ref of the base branch, such as `origin/main`.

    Returns:
        0 if the version was bumped, 1 otherwise.
    """
    new = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    old_text = subprocess.run(["git", "show", f"{base}:pyproject.toml"], capture_output=True, text=True, check=True).stdout
    old = tomllib.loads(old_text)["project"]["version"]
    print(f"base {old} -> this branch {new}")
    if parse(new) <= parse(old):
        print(f"error: bump the version above {old} (pyproject.toml and zylo/__init__.py)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "origin/main"))
