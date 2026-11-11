"""Download a package's sources from git. Never executes anything from the repository."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from zylo.domain.errors import PackageError
from zylo.packaging.manifest import REF_RE

TIMEOUT = 180
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def git(*args: str, cwd: Path | None = None) -> str:
    """Run git non-interactively; user input can only appear after `--`, never as an option."""
    if shutil.which("git") is None:
        raise PackageError("git is not installed; it is required to download packages")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GIT_CONFIG_NOSYSTEM": "1"}
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PackageError(f"git {args[0]} timed out after {TIMEOUT}s") from None
    if r.returncode:
        raise PackageError(f"git {args[0]} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def fetch(url: str, ref: str | None, workdir: Path) -> tuple[Path, str]:
    """Clone `url` at `ref` (tag, branch or commit; default branch if None) under `workdir`.

    Returns (directory without .git, exact commit hash).
    """
    if url.startswith("-"):
        raise PackageError(f"invalid git URL {url!r}")
    if ref is not None and not (REF_RE.match(ref) or COMMIT_RE.match(ref)):
        raise PackageError(f"invalid git ref {ref!r}")
    workdir.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix="pkg-", dir=workdir)) / "src"
    try:
        shallow = ["clone", "--quiet", "--depth", "1"] + (["--branch", ref] if ref else []) + ["--", url, str(dest)]
        try:
            git(*shallow)
        except PackageError:
            shutil.rmtree(dest, ignore_errors=True)
            git("clone", "--quiet", "--", url, str(dest))  # full clone, so a commit hash can be checked out
            if ref:
                git("checkout", "--quiet", "--detach", ref, "--", cwd=dest)
        commit = git("rev-parse", "HEAD", cwd=dest)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return dest, commit
    except PackageError as e:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise PackageError(f"cannot download {url}" + (f"@{ref}" if ref else "") + f": {e.msg}") from None
