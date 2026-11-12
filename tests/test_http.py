"""std.http end to end: build programs with Cargo, run them as subprocesses and talk to them over a real socket."""
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from zylo.application import compile as pipeline
from zylo.application.compile import load_program
from zylo.domain.errors import BackendError, PackageError
from zylo.packaging.stdlib import stdlib_support
from zylo.semantics.checker import check

ROOT = Path(__file__).parent.parent
needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")

SERVER = """
import std.http as http

fn handle(req: http.Request) -> http.Response:
    if req.path == "/":
        return http.text(200, "hello " + req.query.get("name").unwrap_or("world"))
    if req.path == "/echo":
        h: dict[str, str] = {}
        h["x-method"] = req.method
        h["x-len"] = str(len(req.body))
        h["x-agent"] = http.header(req.headers, "User-Agent").unwrap_or("none")
        return http.Response(201, h, req.body)
    if req.path == "/boom":
        xs: list[int] = []
        return http.text(200, str(xs[1]))
    if req.path == "/old":
        return http.redirect("/")
    if req.path == "/json":
        return http.json_response(200, '{"ok":true}')
    if req.path == "/a b":
        return http.text(200, "space")
    return http.not_found()

match http.serve_with("127.0.0.1", __PORT__, handle, __N__, 100):
    ok(n):
        print("served", n)
    err(e):
        eprint(e)
        exit(1)
"""

CLIENT = """
import std.http as http

base = "http://127.0.0.1:__PORT__"
match http.get(base + "/?name=zy"):
    ok(r):
        print(r.status, r.body)
    err(e):
        print("error", e)
h: dict[str, str] = {}
h["content-type"] = "text/plain"
match http.post(base + "/echo", "payload", h):
    ok(r):
        print(r.status, r.body, r.headers.get("x-method").unwrap_or("?"))
    err(e):
        print("error", e)
match http.get(base + "/nope"):
    ok(r):
        print(r.status, r.body)
    err(e):
        print("error", e)
match http.get(base + "/old"):
    ok(r):
        print(r.status, r.headers.get("location").unwrap_or("?"))
    err(e):
        print("error", e)
match http.get("http://127.0.0.1:__DEAD__/"):
    ok(r):
        print("unexpected", r.status)
    err(e):
        print("refused")
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    """Build a program once per source; skips the module when Cargo cannot fetch the crates (offline, no cache)."""
    cache = tmp_path_factory.mktemp("cache")
    mp = pytest.MonkeyPatch()
    mp.setenv("ZYLO_CACHE_DIR", str(cache))
    out_dir = tmp_path_factory.mktemp("bin")
    built: dict[str, Path] = {}

    def do(name: str, source: str) -> Path:
        if name in built:
            return built[name]
        src = out_dir / f"{name}.zy"
        src.write_text(source)
        rs, crates, manifest = pipeline.compile_file(src)
        exe = out_dir / name
        try:
            pipeline.build_executable(rs, crates, manifest, exe, True, False)
        except BackendError as e:
            text = str(e)
            if any(w in text for w in ("download", "network", "resolve", "registry", "index", "offline", "spurious")):
                pytest.skip("cargo cannot fetch the crates here (offline without a cache)")
            raise
        built[name] = exe
        return exe

    yield do
    mp.undo()


def start(exe: Path) -> subprocess.Popen:
    return subprocess.Popen([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def fetch(url: str, data: bytes | None = None, method: str | None = None, headers: dict | None = None):
    """(status, headers, body) of a request, retrying while the server starts."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    deadline = time.time() + 15
    while True:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()
        except urllib.error.URLError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# ---- the Zylo side, without Cargo -------------------------------------------------------------------

def test_the_module_type_checks(tmp_path):
    main = tmp_path / "main.zy"
    main.write_text(SERVER.replace("__PORT__", "1").replace("__N__", "1"))
    check(load_program(main))


def test_support_and_crates_only_come_with_std_http():
    rust, crates = stdlib_support(["std.http"])
    assert "mod zylo_http" in rust and set(crates) == {"tiny_http", "ureq"}
    assert stdlib_support(["std.json", "std.math"]) == ("", {})


def test_programs_without_std_http_keep_the_rustc_path(tmp_path):
    main = tmp_path / "main.zy"
    main.write_text("import std.json as json\nprint(json.stringify(json.Json.Null))\n")
    rs, crates, _ = pipeline.compile_file(main)
    assert crates == {} and "zylo_http" not in rs


def test_importing_std_http_adds_the_crates(tmp_path):
    main = tmp_path / "main.zy"
    main.write_text(SERVER.replace("__PORT__", "1").replace("__N__", "1"))
    rs, crates, _ = pipeline.compile_file(main)
    assert set(crates) == {"tiny_http", "ureq"} and "mod zylo_http" in rs


def test_a_conflicting_crate_in_the_project_is_an_error(tmp_path):
    (tmp_path / "zylo.toml").write_text('[package]\nname = "p"\n[rust-dependencies]\nureq = "1"\n')
    main = tmp_path / "main.zy"
    main.write_text(SERVER.replace("__PORT__", "1").replace("__N__", "1"))
    with pytest.raises(PackageError, match="conflicting requirements for crate 'ureq'"):
        pipeline.compile_file(main)


# ---- running servers ----------------------------------------------------------------------------------

@needs_cargo
def test_server_answers_python_clients(build):
    port = free_port()
    n = 12
    proc = start(build("server", SERVER.replace("__PORT__", str(port)).replace("__N__", str(n))))
    base = f"http://127.0.0.1:{port}"
    try:
        s, h, b = fetch(base + "/?name=Ada%20L+x")
        assert (s, b) == (200, "hello Ada L x") and h["Content-Length"] == "13"
        assert h["content-type"].startswith("text/plain")
        s, h, b = fetch(base + "/echo", data=b"pay\xc3\xa9load", headers={"User-Agent": "tester"})
        assert (s, b) == (201, "payéload") and h["x-method"] == "POST" and h["x-len"] == "8" and h["x-agent"] == "tester"
        assert fetch(base + "/json")[2] == '{"ok":true}'
        assert fetch(base + "/missing")[0] == 404
        assert fetch(base + "/a%20b")[2] == "space"
        # a redirect is not followed by the client below, so look at it with a handler that refuses to follow
        opener = urllib.request.build_opener(NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(base + "/old", timeout=10)
        assert e.value.code == 302 and e.value.headers["location"] == "/"
        # HEAD has the headers of GET and no body; OPTIONS on an unknown path is answered by the server
        s, h, b = fetch(base + "/", method="HEAD")
        assert s == 200 and b == "" and h["Content-Length"] == "11"
        s, h, b = fetch(base + "/nowhere", method="OPTIONS")
        assert s == 204 and "GET" in h["Allow"] and b == ""
        # a request that is not HTTP is dropped (tiny_http answers 400 or closes) and does not count or kill the server
        with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
            c.sendall(b"NOT AN HTTP REQUEST\r\n\r\n")
            try:
                c.recv(100)
            except ConnectionResetError:
                pass
        # a panic in the handler is a 500, and the server keeps going
        assert fetch(base + "/boom")[0] == 500
        assert fetch(base + "/")[2] == "hello world"
        # PUT and DELETE reach the handler with their method
        assert fetch(base + "/echo", data=b"x", method="PUT")[1]["x-method"] == "PUT"
        assert fetch(base + "/echo", method="DELETE")[1]["x-method"] == "DELETE"
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0 and out.strip() == f"served {n}", err
        assert "handler panicked: index out of bounds" in err
    finally:
        proc.kill()


@needs_cargo
def test_body_limit_is_413(build):
    port = free_port()
    src = SERVER.replace("__PORT__", str(port)).replace("__N__", "2").replace(", 100)", ", 10)")
    proc = start(build("limited", src))
    try:
        s, _, _ = fetch(f"http://127.0.0.1:{port}/echo", data=b"x" * 11)
        assert s == 413
        s, _, b = fetch(f"http://127.0.0.1:{port}/echo", data=b"x" * 10)
        assert s == 201 and b == "x" * 10
        proc.communicate(timeout=20)
    finally:
        proc.kill()


@needs_cargo
def test_zylo_client_talks_to_a_zylo_server(build):
    port = free_port()
    server = start(build("server2", SERVER.replace("__PORT__", str(port)).replace("__N__", "4")))
    client = build("client", CLIENT.replace("__PORT__", str(port)).replace("__DEAD__", str(free_port())))
    try:
        time.sleep(0.3)
        r = subprocess.run([str(client)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert r.stdout.splitlines() == ["200 hello zy", "201 payload POST", "404 Not Found", "302 /", "refused"]
        server.communicate(timeout=20)
    finally:
        server.kill()


@needs_cargo
def test_port_in_use_is_an_error_not_a_crash(build):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        exe = build("server3", SERVER.replace("__PORT__", str(port)).replace("__N__", "1"))
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "cannot listen on" in r.stderr
