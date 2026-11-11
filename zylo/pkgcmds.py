"""The package-management commands: init, venv, add, remove, install, update, list."""
import re
from pathlib import Path

from . import env as envmod
from .env import ENV_DIR, Env
from .errors import PackageError
from .lock import LOCK, Lock, dump_lock, load_lock
from .manifest import (MANIFEST, Dependency, Manifest, check_name, dump_manifest, find_manifest, load_manifest,
                       normalize_url, split_ref)
from .packages import Installer

HELLO = 'print("hello from zylo")\n'


def need_manifest(cwd: Path) -> Manifest:
    path = find_manifest(cwd)
    if path is None:
        raise PackageError(f"no {MANIFEST} found here or in a parent directory; create one with `zylo init`")
    return load_manifest(path)


def need_env(cwd: Path, explicit: str | None = None) -> Env:
    env = envmod.discover(cwd, explicit)
    if env is None:
        raise PackageError("no virtual environment found; create one with `zylo venv`")
    return env


def save(manifest: Manifest, lock: Lock) -> None:
    manifest.path.write_text(dump_manifest(manifest))
    (manifest.root / LOCK).write_text(dump_lock(lock))


def summarize(before: Lock, after: Lock) -> list[str]:
    lines = []
    for name in sorted(after.entries):
        e, old = after.entries[name], before.entries.get(name)
        where = (f"{e.version or '?'}" + (f" ({e.commit[:8]})" if e.commit else " (local path)"))
        if old is None:
            lines.append(f"  installed  {name} {where}")
        elif old.checksum != e.checksum:
            lines.append(f"  updated    {name} {where}")
    for name in sorted(set(before.entries) - set(after.entries)):
        lines.append(f"  removed    {name}")
    return lines


def cmd_init(cwd: Path, name: str | None) -> int:
    if (cwd / MANIFEST).exists():
        raise PackageError(f"{MANIFEST} already exists here")
    name = check_name(name or re.sub(r"\W", "_", cwd.resolve().name))
    manifest = Manifest(name, path=cwd / MANIFEST)
    manifest.path.write_text(dump_manifest(manifest))
    src = cwd / "src"
    src.mkdir(exist_ok=True)
    if not (src / "main.zy").exists():
        (src / "main.zy").write_text(HELLO)
    ignore = cwd / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    missing = [d for d in (ENV_DIR, ".zylo-build") if d not in text.split() and f"{d}/" not in text.split()]
    if missing:
        ignore.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "".join(f"{d}/\n" for d in missing))
    print(f"created {MANIFEST} and src/main.zy for package {name!r}")
    return 0


def cmd_venv(cwd: Path, path: str | None, force: bool) -> int:
    root = Path(path) if path else cwd / ENV_DIR
    Env.create(root, force)
    print(f"created {root}\nnext: `zylo add <source>` to install a package, or `zylo install` to install from {MANIFEST}")
    return 0


def cmd_install(cwd: Path, env_dir: str | None = None, update: bool = False) -> int:
    manifest, env = need_manifest(cwd), need_env(cwd, env_dir)
    before = load_lock(manifest.root / LOCK)
    after = Installer(env, manifest, before, update).sync()
    save(manifest, after)
    lines = summarize(before, after)
    print("\n".join(lines) if lines else "environment is up to date")
    return 0


def cmd_add(cwd: Path, source: str, name: str | None, as_path: bool, env_dir: str | None = None) -> int:
    manifest, env = need_manifest(cwd), need_env(cwd, env_dir)
    before = load_lock(manifest.root / LOCK)
    inst = Installer(env, manifest, before)
    if as_path:
        pkg_manifest = Path(source) / MANIFEST
        found = load_manifest(pkg_manifest).name if pkg_manifest.is_file() else name or re.sub(r"\W", "_", Path(source).resolve().name)
        dep = Dependency(check_name(found), path=source)
    else:
        src, ref = split_ref(source)
        url = normalize_url(src)
        staged, _ = inst.prefetch(url, ref)
        declared = None
        if (staged / MANIFEST).is_file():
            declared = load_manifest(staged / MANIFEST).name
        if declared and name and name != declared:
            raise PackageError(f"the package declares the name {declared!r}; --name {name!r} does not match")
        found = declared or name or re.sub(r"\W", "_", url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1].rsplit(":", 1)[-1])
        dep = Dependency(check_name(found), git=url, ref=ref)
    manifest.dependencies[dep.name] = dep
    after = inst.sync()
    save(manifest, after)
    print("\n".join(summarize(before, after)) or f"  {dep.name} is already installed")
    print(f"updated {MANIFEST} and {LOCK}")
    return 0


def cmd_remove(cwd: Path, name: str, env_dir: str | None = None) -> int:
    manifest, env = need_manifest(cwd), need_env(cwd, env_dir)
    if name not in manifest.dependencies:
        raise PackageError(f"{name!r} is not a dependency of this project")
    del manifest.dependencies[name]
    before = load_lock(manifest.root / LOCK)
    after = Installer(env, manifest, before).sync()
    save(manifest, after)
    print("\n".join(summarize(before, after)))
    return 0


def cmd_list(cwd: Path, env_dir: str | None = None) -> int:
    env = need_env(cwd, env_dir)
    path = find_manifest(cwd)
    lock = load_lock(path.parent / LOCK) if path else Lock()
    names = env.installed()
    if not names:
        print("no packages installed")
    for name in names:
        e = lock.entries.get(name)
        if e is None:
            print(f"{name}  (not in {LOCK})")
        else:
            src = e.source + (f"@{e.ref}" if e.ref else "")
            print(f"{name} {e.version or '?'}  {src}" + (f"  {e.commit[:8]}" if e.commit else ""))
    return 0
