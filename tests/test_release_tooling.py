"""Tests for the release tooling: version consistency, the version-bump check and install.sh."""
import hashlib
import http.server
import importlib.util
import os
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

import zylo

ROOT = Path(__file__).parent.parent
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is for Linux and macOS")


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- versions ----------------------------------------------------------------------------------

def test_package_version_matches_pyproject():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert zylo.__version__ == declared


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.parametrize("new,expected", [("0.2.0", 0), ("0.1.1", 0), ("1.0.0", 0), ("0.1.0", 1), ("0.0.9", 1)])
def test_version_bump_check(tmp_path, monkeypatch, new, expected):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{new}"\n')
    monkeypatch.chdir(tmp_path)
    assert load_script("check_version_bump").main("HEAD") == expected


def test_version_bump_rejects_malformed_versions():
    with pytest.raises(SystemExit):
        load_script("check_version_bump").parse("1.0")


# ---- install.sh -----------------------------------------------------------------------------------

@pytest.fixture
def release_server(tmp_path):
    """A local 'GitHub releases' directory served over HTTP: <root>/latest/download/..."""
    root = tmp_path / "releases"
    (root / "latest" / "download").mkdir(parents=True)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def publish(root: Path, body: str = '#!/bin/sh\necho "zylo 9.9.9"\n', target: str = "linux-x86_64", checksum: str | None = None) -> None:
    d = root / "latest" / "download"
    (d / f"zylo-{target}").write_text(body)
    digest = checksum or hashlib.sha256(body.encode()).hexdigest()
    (d / "SHA256SUMS").write_text(f"{digest}  zylo-{target}\n")


def install(tmp_path: Path, base: str, *args: str, target: str = "linux-x86_64") -> tuple[subprocess.CompletedProcess, Path]:
    dest = tmp_path / "bin"
    env = {**os.environ, "ZYLO_BASE_URL": base, "ZYLO_INSTALL_DIR": str(dest), "ZYLO_TARGET": target}
    r = subprocess.run(["sh", str(ROOT / "install.sh"), *args], capture_output=True, text=True, env=env)
    return r, dest


@posix_only
def test_install_downloads_verifies_and_installs(tmp_path, release_server):
    root, base = release_server
    publish(root)
    r, dest = install(tmp_path, base)
    assert r.returncode == 0, r.stderr
    assert "Installed zylo 9.9.9" in r.stdout
    assert subprocess.run([str(dest / "zylo"), "--version"], capture_output=True, text=True).stdout.strip() == "zylo 9.9.9"


@posix_only
def test_install_rejects_a_checksum_mismatch_and_installs_nothing(tmp_path, release_server):
    root, base = release_server
    publish(root, checksum="0" * 64)
    r, dest = install(tmp_path, base)
    assert r.returncode == 1 and "checksum mismatch" in r.stderr
    assert not (dest / "zylo").exists()


@posix_only
def test_install_removes_a_binary_that_does_not_run(tmp_path, release_server):
    root, base = release_server
    publish(root, body="#!/bin/sh\nexit 3\n")
    r, dest = install(tmp_path, base)
    assert r.returncode == 1 and "does not run" in r.stderr
    assert not (dest / "zylo").exists()


@posix_only
def test_install_reports_a_missing_release_and_an_unlisted_target(tmp_path, release_server):
    root, base = release_server
    r, _ = install(tmp_path, base)
    assert r.returncode == 1 and "could not download" in r.stderr
    publish(root, target="macos-arm64")
    r, _ = install(tmp_path, base, target="linux-x86_64")
    assert r.returncode == 1 and "could not download" in r.stderr


@posix_only
def test_install_rejects_unsupported_platforms_with_a_pip_hint(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text('#!/bin/sh\n[ "$1" = "-m" ] && echo mips || echo Plan9\n')
    (fake / "uname").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "ZYLO_TARGET": ""}
    r = subprocess.run(["sh", str(ROOT / "install.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "no prebuilt binary" in r.stderr and "pip install" in r.stderr


@posix_only
def test_uninstall(tmp_path, release_server):
    root, base = release_server
    publish(root)
    _, dest = install(tmp_path, base)
    assert (dest / "zylo").exists()
    r, _ = install(tmp_path, base, "--uninstall")
    assert r.returncode == 0 and not (dest / "zylo").exists()
