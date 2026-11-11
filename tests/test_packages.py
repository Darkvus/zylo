import os
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.domain.errors import PackageError
from zylo.interface.cli import main
from zylo.packaging import env as envmod
from zylo.packaging.lock import load_lock
from zylo.packaging.manifest import (
    Dependency,
    Manifest,
    dump_manifest,
    load_manifest,
    normalize_url,
    package_layout,
    split_ref,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

STRUTIL = {
    "zylo.toml": '[package]\nname = "strutil"\nversion = "1.2.0"\n',
    "src/lib.zy": "fn shout(s: str) -> str:\n    return s + \"!\"\n\nfn _hidden() -> int:\n    return 1\n",
}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def make_repo(root: Path, name: str, files: dict[str, str], tag: str | None = None) -> str:
    repo = root / "repos" / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    commit_files(repo, files, "initial", tag)
    return repo.as_uri()


def commit_files(repo: Path, files: dict[str, str], msg: str, tag: str | None = None) -> str:
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    if tag:
        git(repo, "tag", tag)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """A project directory with a manifest and an environment, and the CWD set to it."""
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.chdir(d)
    monkeypatch.delenv("ZYLO_ENV", raising=False)
    assert main(["init", "app"]) == 0
    assert main(["venv"]) == 0
    return d


# ---- manifest ------------------------------------------------------------------------------------

def test_normalize_url_and_split_ref():
    assert normalize_url("github.com/ana/strutil") == "https://github.com/ana/strutil.git"
    assert normalize_url("github.com/ana/strutil.git") == "https://github.com/ana/strutil.git"
    assert normalize_url("https://x.org/a/b.git") == "https://x.org/a/b.git"
    assert normalize_url("git@github.com:ana/lib.git") == "git@github.com:ana/lib.git"
    assert normalize_url("file:///tmp/r") == "file:///tmp/r"
    assert split_ref("github.com/a/b@v1.2") == ("github.com/a/b", "v1.2")
    assert split_ref("github.com/a/b") == ("github.com/a/b", None)
    assert split_ref("git@github.com:a/b.git") == ("git@github.com:a/b.git", None)
    assert split_ref("git@github.com:a/b.git@main") == ("git@github.com:a/b.git", "main")


@pytest.mark.parametrize("bad", ["-oProxyCommand=x", "foo", "a/b", "has space/x/y", ""])
def test_normalize_url_rejects_bad_sources(bad):
    with pytest.raises(PackageError):
        normalize_url(bad)


def test_split_ref_rejects_option_like_refs():
    with pytest.raises(PackageError, match="invalid ref"):
        split_ref("github.com/a/b@--upload-pack=x")


def test_manifest_roundtrip(tmp_path):
    m = Manifest("app", "0.3.0", "src/lib.zy", {
        "strutil": Dependency("strutil", git="https://github.com/ana/strutil.git", ref="v1.2.0"),
        "local": Dependency("local", path="../local"),
        "latest": Dependency("latest", git="https://x.org/a/b.git"),
    })
    p = tmp_path / "zylo.toml"
    p.write_text(dump_manifest(m))
    back = load_manifest(p)
    assert back.name == "app" and back.version == "0.3.0" and back.entry == "src/lib.zy"
    assert back.dependencies["strutil"].ref == "v1.2.0"
    assert back.dependencies["local"].path == "../local"
    assert back.dependencies["latest"].ref is None


@pytest.mark.parametrize("name", ["../evil", "a/b", "1abc", "has-dash", "", "a b"])
def test_invalid_package_names_are_rejected(tmp_path, name):
    p = tmp_path / "zylo.toml"
    p.write_text(f'[package]\nname = "{name}"\n')
    with pytest.raises(PackageError, match="invalid package name"):
        load_manifest(p)
    p.write_text(f'[package]\nname = "ok"\n[dependencies]\n"{name}" = "github.com/a/b"\n')
    with pytest.raises(PackageError, match="invalid dependency name"):
        load_manifest(p)


def test_manifest_errors_are_package_errors(tmp_path):
    with pytest.raises(PackageError, match="cannot read"):
        load_manifest(tmp_path / "nope.toml")
    p = tmp_path / "zylo.toml"
    p.write_text("not [valid")
    with pytest.raises(PackageError, match="invalid TOML"):
        load_manifest(p)
    p.write_text("[dependencies]\n")
    with pytest.raises(PackageError, match="missing \\[package\\] name"):
        load_manifest(p)


def test_package_layout_defaults_and_declared_entry(tmp_path):
    pkg = tmp_path / "p"
    (pkg / "src").mkdir(parents=True)
    (pkg / "src" / "lib.zy").write_text("")
    assert package_layout(pkg)[0] == (pkg / "src" / "lib.zy").resolve()
    (pkg / "zylo.toml").write_text('[package]\nname = "p"\nentry = "src/main_lib.zy"\n')
    (pkg / "src" / "main_lib.zy").write_text("")
    assert package_layout(pkg)[0].name == "main_lib.zy"
    (pkg / "zylo.toml").write_text('[package]\nname = "p"\nentry = "../outside.zy"\n')  # must stay inside
    assert package_layout(pkg)[0].name == "lib.zy"


# ---- environments ------------------------------------------------------------------------------------

def test_env_create_force_and_safety(tmp_path):
    root = tmp_path / "e"
    env = envmod.Env.create(root)
    assert env.exists() and env.packages_dir.is_dir()
    with pytest.raises(PackageError, match="already exists"):
        envmod.Env.create(root)
    (env.packages_dir / "x").mkdir()
    envmod.Env.create(root, force=True)
    assert env.installed() == []
    other = tmp_path / "precious"
    other.mkdir()
    (other / "file.txt").write_text("keep me")
    with pytest.raises(PackageError, match="does not look like a Zylo environment"):
        envmod.Env.create(other, force=True)
    assert (other / "file.txt").exists()


def test_env_discovery(tmp_path):
    envmod.Env.create(tmp_path / ".zylo-env")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert envmod.discover(deep, environ={}).root.resolve() == (tmp_path / ".zylo-env").resolve()
    other = tmp_path / "elsewhere"
    envmod.Env.create(other)
    assert envmod.discover(deep, environ={"ZYLO_ENV": str(other)}).root == other
    assert envmod.discover(deep, explicit=str(other), environ={}).root == other
    with pytest.raises(PackageError, match="not a Zylo environment"):
        envmod.discover(deep, explicit=str(tmp_path / "nope"), environ={})


def test_init_creates_files_and_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "demo"]) == 0
    assert (tmp_path / "src" / "main.zy").is_file()
    assert load_manifest(tmp_path / "zylo.toml").name == "demo"
    assert ".zylo-env/" in (tmp_path / ".gitignore").read_text()
    assert main(["init"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_init_rejects_invalid_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "../x"]) == 1
    assert "invalid package name" in capsys.readouterr().err


def test_commands_without_manifest_or_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZYLO_ENV", raising=False)
    assert main(["install"]) == 1
    assert "no zylo.toml found" in capsys.readouterr().err
    (tmp_path / "zylo.toml").write_text('[package]\nname = "p"\n')
    assert main(["install"]) == 1
    assert "no virtual environment found" in capsys.readouterr().err


# ---- add / install / remove / list ---------------------------------------------------------------------------

@needs_git
def test_add_installs_updates_manifest_and_lock(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1.2.0")
    assert main(["add", f"{url}@v1.2.0"]) == 0
    out = capsys.readouterr().out
    assert "installed  strutil 1.2.0" in out
    assert (proj / ".zylo-env" / "packages" / "strutil" / "src" / "lib.zy").is_file()
    assert not (proj / ".zylo-env" / "packages" / "strutil" / ".git").exists()
    m = load_manifest(proj / "zylo.toml")
    assert m.dependencies["strutil"].ref == "v1.2.0" and m.dependencies["strutil"].git == url
    lock = load_lock(proj / "zylo.lock")
    e = lock.entries["strutil"]
    assert e.version == "1.2.0" and len(e.commit) == 40 and e.checksum.startswith("sha256:")
    assert main(["list"]) == 0
    assert "strutil 1.2.0" in capsys.readouterr().out


@needs_git
@needs_rustc
def test_program_uses_an_installed_package(proj, tmp_path, capfd):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1.2.0")
    assert main(["add", f"{url}@v1.2.0"]) == 0
    capfd.readouterr()
    (proj / "src" / "main.zy").write_text("import strutil\nfrom strutil import shout as s\nprint(strutil.shout(\"hi\"))\nprint(s(\"yo\"))\n")
    assert main(["run", str(proj / "src" / "main.zy")]) == 0
    assert capfd.readouterr().out == "hi!\nyo!\n"


@needs_git
def test_package_private_names_and_errors_point_at_the_package(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL | {"src/lib.zy": "fn bad() -> int:\n    return \"x\"\n"}, tag="v1")
    assert main(["add", f"{url}@v1"]) == 0
    capsys.readouterr()
    (proj / "src" / "main.zy").write_text("import strutil\nprint(strutil.bad())\n")
    assert main(["check", str(proj / "src" / "main.zy")]) == 1
    err = capsys.readouterr().err
    assert "packages/strutil/src/lib.zy:2" in err and 'return "x"' in err


@needs_git
def test_import_without_an_environment_explains_why(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZYLO_ENV", raising=False)
    (tmp_path / "main.zy").write_text("import strutil\n")
    assert main(["check", str(tmp_path / "main.zy")]) == 1
    assert "no environment is active" in capsys.readouterr().err


@needs_git
def test_install_is_reproducible_and_uses_the_locked_commit(proj, tmp_path, capsys):
    repo = tmp_path / "repos" / "strutil"
    url = make_repo(tmp_path, "strutil", STRUTIL)
    assert main(["add", f"{url}@main"]) == 0
    locked = load_lock(proj / "zylo.lock").entries["strutil"]
    new = commit_files(repo, {"src/lib.zy": "fn shout(s: str) -> str:\n    return s + \"!!!\"\n"}, "change")
    assert new != locked.commit
    # recreate the environment from scratch: the LOCKED commit is installed, not the moved branch
    assert main(["venv", "--force"]) == 0
    assert main(["install"]) == 0
    again = load_lock(proj / "zylo.lock").entries["strutil"]
    assert (again.commit, again.checksum) == (locked.commit, locked.checksum)
    assert '"!"' in (proj / ".zylo-env" / "packages" / "strutil" / "src" / "lib.zy").read_text()
    # `update` moves the branch forward
    capsys.readouterr()
    assert main(["update"]) == 0
    assert "updated    strutil" in capsys.readouterr().out
    assert load_lock(proj / "zylo.lock").entries["strutil"].commit == new


@needs_git
def test_install_when_up_to_date_does_nothing(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1")
    assert main(["add", f"{url}@v1"]) == 0
    capsys.readouterr()
    shutil.rmtree(tmp_path / "repos")  # no network/repo needed when the install matches the lock
    assert main(["install"]) == 0
    assert "up to date" in capsys.readouterr().out


@needs_git
def test_tampered_install_is_repaired_from_the_locked_commit(proj, tmp_path):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1")
    assert main(["add", f"{url}@v1"]) == 0
    lib = proj / ".zylo-env" / "packages" / "strutil" / "src" / "lib.zy"
    good = lib.read_text()
    lib.write_text("fn shout(s: str) -> str:\n    return \"pwned\"\n")
    assert main(["install"]) == 0
    assert lib.read_text() == good


@needs_git
def test_checksum_mismatch_with_the_lock_is_an_error(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1")
    assert main(["add", f"{url}@v1"]) == 0
    lock = proj / "zylo.lock"
    lock.write_text(lock.read_text().replace("sha256:", "sha256:0000"))
    shutil.rmtree(proj / ".zylo-env" / "packages" / "strutil")
    capsys.readouterr()
    assert main(["install"]) == 1
    assert "checksum mismatch" in capsys.readouterr().err


@needs_git
def test_remove_prunes_the_environment(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1")
    assert main(["add", f"{url}@v1"]) == 0
    capsys.readouterr()
    assert main(["remove", "strutil"]) == 0
    assert "removed    strutil" in capsys.readouterr().out
    assert not (proj / ".zylo-env" / "packages" / "strutil").exists()
    assert "strutil" not in load_manifest(proj / "zylo.toml").dependencies
    assert main(["remove", "strutil"]) == 1


@needs_git
def test_transitive_dependencies_are_installed(proj, tmp_path):
    base = make_repo(tmp_path, "basepkg", {"zylo.toml": '[package]\nname = "basepkg"\nversion = "0.1.0"\n',
                                           "src/lib.zy": "fn one() -> int:\n    return 1\n"}, tag="v1")
    top = make_repo(tmp_path, "toppkg", {
        "zylo.toml": f'[package]\nname = "toppkg"\nversion = "0.2.0"\n[dependencies]\nbasepkg = "{base}@v1"\n',
        "src/lib.zy": "import basepkg\nfn two() -> int:\n    return basepkg.one() + 1\n"}, tag="v1")
    assert main(["add", f"{top}@v1"]) == 0
    assert envmod.discover(proj).installed() == ["basepkg", "toppkg"]
    assert set(load_lock(proj / "zylo.lock").entries) == {"basepkg", "toppkg"}


@needs_git
@needs_rustc
def test_transitive_package_import_runs(proj, tmp_path, capfd):
    base = make_repo(tmp_path, "basepkg", {"src/lib.zy": "fn one() -> int:\n    return 1\n"}, tag="v1")
    top = make_repo(tmp_path, "toppkg", {
        "zylo.toml": f'[package]\nname = "toppkg"\n[dependencies]\nbasepkg = "{base}@v1"\n',
        "src/lib.zy": "import basepkg\nfn two() -> int:\n    return basepkg.one() + 1\n"}, tag="v1")
    # basepkg has no manifest, so its dependency name comes from the manifest key
    assert main(["add", f"{top}@v1"]) == 0
    capfd.readouterr()
    (proj / "src" / "main.zy").write_text("import toppkg\nprint(toppkg.two())\n")
    assert main(["run", str(proj / "src" / "main.zy")]) == 0
    assert capfd.readouterr().out == "2\n"


@needs_git
def test_conflicting_requirements_are_reported(proj, tmp_path, capsys):
    c1 = make_repo(tmp_path, "cpkg", {"src/lib.zy": "fn f() -> int:\n    return 1\n"}, tag="v1")
    repo = tmp_path / "repos" / "cpkg"
    commit_files(repo, {"src/lib.zy": "fn f() -> int:\n    return 2\n"}, "v2", tag="v2")
    a = make_repo(tmp_path, "apkg", {"zylo.toml": f'[package]\nname = "apkg"\n[dependencies]\ncpkg = "{c1}@v1"\n', "src/lib.zy": "fn a() -> int:\n    return 1\n"}, tag="v1")
    b = make_repo(tmp_path, "bpkg", {"zylo.toml": f'[package]\nname = "bpkg"\n[dependencies]\ncpkg = "{c1}@v2"\n', "src/lib.zy": "fn b() -> int:\n    return 1\n"}, tag="v1")
    assert main(["add", f"{a}@v1"]) == 0
    capsys.readouterr()
    assert main(["add", f"{b}@v1"]) == 1
    assert "conflicting requirements for 'cpkg'" in capsys.readouterr().err


@needs_git
def test_package_name_must_match_its_manifest(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1")
    assert main(["add", f"{url}@v1", "--name", "other"]) == 1
    assert "does not match" in capsys.readouterr().err
    m = load_manifest(proj / "zylo.toml")
    m.dependencies["wrong"] = Dependency("wrong", git=url, ref="v1")
    (proj / "zylo.toml").write_text(dump_manifest(m))
    assert main(["install"]) == 1
    assert "points to a package named 'strutil'" in capsys.readouterr().err


@needs_git
def test_add_with_name_for_a_package_without_a_manifest(proj, tmp_path):
    url = make_repo(tmp_path, "plain", {"src/lib.zy": "fn f() -> int:\n    return 1\n"}, tag="v1")
    assert main(["add", f"{url}@v1", "--name", "plainlib"]) == 0
    assert envmod.discover(proj).installed() == ["plainlib"]


@needs_git
def test_bad_ref_and_bad_url_give_friendly_errors(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "strutil", STRUTIL, tag="v1")
    assert main(["add", f"{url}@v9"]) == 1
    assert "cannot download" in capsys.readouterr().err
    assert main(["add", "--", "-oProxyCommand=evil"]) == 1
    assert "invalid git source" in capsys.readouterr().err
    assert main(["add", "notasource"]) == 1


@needs_git
def test_packages_with_symlinks_are_rejected(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "evil", {"src/lib.zy": "fn f() -> int:\n    return 1\n"}, tag="v1")
    repo = tmp_path / "repos" / "evil"
    os.symlink("/etc/passwd", repo / "src" / "leak.zy")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    git(repo, "tag", "v2")
    assert main(["add", f"{url}@v2", "--name", "evil"]) == 1
    assert "symbolic link" in capsys.readouterr().err
    assert envmod.discover(proj).installed() == []


@needs_git
def test_path_dependencies(proj, tmp_path, capsys):
    lib = tmp_path / "locallib"
    (lib / "src").mkdir(parents=True)
    (lib / "zylo.toml").write_text('[package]\nname = "locallib"\nversion = "0.0.1"\n')
    (lib / "src" / "lib.zy").write_text("fn f() -> int:\n    return 7\n")
    assert main(["add", "--path", str(lib)]) == 0
    assert (proj / ".zylo-env" / "packages" / "locallib" / "src" / "lib.zy").is_file()
    lock = load_lock(proj / "zylo.lock").entries["locallib"]
    assert lock.commit is None and lock.source.startswith("path:")
    (lib / "src" / "lib.zy").write_text("fn f() -> int:\n    return 8\n")  # edits are picked up by install
    capsys.readouterr()
    assert main(["install"]) == 0
    assert "return 8" in (proj / ".zylo-env" / "packages" / "locallib" / "src" / "lib.zy").read_text()


@needs_git
def test_path_dependency_in_a_package_is_rejected(proj, tmp_path, capsys):
    url = make_repo(tmp_path, "sneaky", {"zylo.toml": '[package]\nname = "sneaky"\n[dependencies]\nx = { path = "/etc" }\n',
                                         "src/lib.zy": "fn f() -> int:\n    return 1\n"}, tag="v1")
    assert main(["add", f"{url}@v1"]) == 1
    assert "path dependency" in capsys.readouterr().err


@needs_git
@needs_rustc
def test_local_modules_win_over_packages_and_submodules_work(proj, tmp_path, capfd):
    url = make_repo(tmp_path, "toolkit", {
        "zylo.toml": '[package]\nname = "toolkit"\nentry = "src/toolkit.zy"\n',
        "src/toolkit.zy": "fn top() -> int:\n    return 1\n",
        "src/extra.zy": "fn more() -> int:\n    return 10\n"}, tag="v1")
    assert main(["add", f"{url}@v1"]) == 0
    capfd.readouterr()
    src = proj / "src"
    (src / "main.zy").write_text("import toolkit\nimport toolkit.extra\nprint(toolkit.top() + extra.more())\n")
    assert main(["run", str(src / "main.zy")]) == 0
    assert capfd.readouterr().out == "11\n"
    (src / "toolkit.zy").write_text("fn top() -> int:\n    return 100\n")  # a local file now shadows the package
    (src / "main.zy").write_text("import toolkit\nprint(toolkit.top())\n")
    assert main(["run", str(src / "main.zy")]) == 0
    assert capfd.readouterr().out == "100\n"
