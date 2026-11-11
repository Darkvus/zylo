"""Install packages into an environment according to a manifest and a lock file."""
import hashlib
import os
import shutil
from pathlib import Path

from . import gitfetch
from .env import Env
from .errors import PackageError
from .lock import Lock, LockEntry
from .manifest import MANIFEST, Dependency, Manifest, load_manifest


def walk_files(root: Path):
    """(relative path, absolute path) of every file under `root`, rejecting symbolic links."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise PackageError(f"package contains a symbolic link ({p.relative_to(root)}); links are not allowed")
        for name in filenames:
            p = Path(dirpath) / name
            yield p.relative_to(root).as_posix(), p


def checksum_tree(root: Path) -> str:
    h = hashlib.sha256()
    for rel, p in sorted(walk_files(root)):
        h.update(rel.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


def requirement(dep: Dependency) -> tuple:
    return (dep.git, dep.ref, dep.path)


def describe(dep: Dependency) -> str:
    return f"path {dep.path}" if dep.is_path else f"{dep.git}" + (f"@{dep.ref}" if dep.ref else "")


class Installer:
    def __init__(self, env: Env, manifest: Manifest, lock: Lock, update: bool = False):
        self.env, self.manifest, self.lock, self.update = env, manifest, lock, update
        self.prefetched: dict[tuple[str, str | None], tuple[Path, str]] = {}

    # ---- fetching ---------------------------------------------------------------------------------

    def fetch(self, url: str, ref: str | None) -> tuple[Path, str]:
        hit = self.prefetched.pop((url, ref), None)
        return hit if hit else gitfetch.fetch(url, ref, self.env.tmp_dir)

    def prefetch(self, url: str, ref: str | None) -> tuple[Path, str]:
        got = gitfetch.fetch(url, ref, self.env.tmp_dir)
        self.prefetched[(url, ref)] = got
        return got

    # ---- one package ----------------------------------------------------------------------------------

    def install_one(self, dep: Dependency, base: Path) -> LockEntry:
        target = self.env.packages_dir / dep.name  # the name is a validated identifier
        locked = None if self.update else self.lock.entries.get(dep.name)
        commit: str | None = None
        if dep.is_path:
            locked = None  # a local directory may change freely; it is simply reinstalled
            src = (base / dep.path).resolve()
            if not src.is_dir():
                raise PackageError(f"dependency {dep.name!r}: path {dep.path} is not a directory")
            staged = Path(self.env.tmp_dir / f"path-{dep.name}")
            shutil.rmtree(staged, ignore_errors=True)
            shutil.copytree(src, staged, symlinks=True, ignore=shutil.ignore_patterns(".git", ".zylo-env"))
            source, ref = f"path:{dep.path}", None
        else:
            source, ref = dep.git, dep.ref
            if locked and locked.source == source and locked.ref == ref and locked.commit:
                if target.is_dir() and checksum_tree(target) == locked.checksum:
                    return locked  # already installed exactly as locked: no network needed
                staged, commit = self.fetch(source, locked.commit)  # reproduce the locked commit
            else:
                locked = None
                staged, commit = self.fetch(source, ref)
        checksum = checksum_tree(staged)
        if locked and checksum != locked.checksum:
            raise PackageError(
                f"checksum mismatch for {dep.name!r} at commit {locked.commit[:8]}: the repository content changed "
                f"since it was locked (run `zylo update` if you trust the new content)"
            )
        version = ""
        if (staged / MANIFEST).is_file():
            pkg = load_manifest(staged / MANIFEST)
            if pkg.name != dep.name:
                raise PackageError(f"dependency {dep.name!r} points to a package named {pkg.name!r}")
            version = pkg.version
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        return LockEntry(dep.name, source, ref, commit, checksum, version)

    # ---- the whole environment -----------------------------------------------------------------------------

    def sync(self) -> Lock:
        """Install every dependency (and theirs), remove what is no longer needed, return the new lock."""
        wanted: dict[str, LockEntry] = {}
        required: dict[str, Dependency] = {}
        queue = [(d, self.manifest.root) for d in self.manifest.dependencies.values()]
        try:
            while queue:
                dep, base = queue.pop(0)
                seen = required.get(dep.name)
                if seen is not None:
                    if requirement(seen) != requirement(dep):
                        raise PackageError(
                            f"conflicting requirements for {dep.name!r}: {describe(seen)} and {describe(dep)}"
                        )
                    continue
                required[dep.name] = dep
                wanted[dep.name] = self.install_one(dep, base)
                pkg_manifest = self.env.packages_dir / dep.name / MANIFEST
                if pkg_manifest.is_file():
                    for sub in load_manifest(pkg_manifest).dependencies.values():
                        if sub.is_path:
                            raise PackageError(f"package {dep.name!r} has a path dependency {sub.name!r}; only the project may")
                        queue.append((sub, pkg_manifest.parent))
            for name in self.env.installed():
                if name not in wanted:
                    shutil.rmtree(self.env.packages_dir / name)
        finally:
            for staged, _ in self.prefetched.values():
                shutil.rmtree(staged.parent, ignore_errors=True)
            shutil.rmtree(self.env.tmp_dir, ignore_errors=True)
        return Lock(wanted)
