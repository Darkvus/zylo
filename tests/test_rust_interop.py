import shutil
from pathlib import Path

import pytest

from zylo import ast, cli
from zylo.cargo import cargo_toml, validate_rust_dep
from zylo.checker import check
from zylo.cli import main
from zylo.env import Env
from zylo.errors import PackageError, ZyloError
from zylo.manifest import Manifest, dump_manifest, load_manifest
from zylo.parser import parse
from zylo.types import FLOAT, INT, STR, ResultT

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")
needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")


def checked(src: str):
    return check(parse(src))


def fails(src: str, match: str) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    return e.value


# ---- syntax and types -------------------------------------------------------------------------------

def test_parse_extern_fn():
    s = parse('extern fn hypot(a: float, b: float) -> float = "f64::hypot"\n').body[0]
    assert isinstance(s, ast.ExternFn) and isinstance(s, ast.FnDef)
    assert s.name == "hypot" and [p.name for p in s.params] == ["a", "b"] and s.template == "f64::hypot"
    assert parse('extern fn tick() = "x()"\n').body[0].ret is None


@pytest.mark.parametrize("src,msg", [
    ("extern x\n", "expected 'fn' after 'extern'"),
    ("extern fn f(a: int) -> int\n", "expected '='"),
    ("extern fn f(a: int) -> int = 5\n", "expected a string"),
    ('extern fn f(a) -> int = "x"\n', "needs a type"),
])
def test_parse_extern_errors(src, msg):
    with pytest.raises(ZyloError, match=msg):
        parse(src)


def test_extern_fn_types_calls_and_errors():
    p = checked('extern fn sq(x: float) -> float = "{0} * {0}"\nextern fn parse(s: str) -> Result[int, str] = "x"\nx = sq(2.0)\ny = parse("1")\n')
    assert p.body[2].value.ty == FLOAT and p.body[3].value.ty == ResultT(INT, STR)
    fails('extern fn sq(x: float) -> float = "x"\ny = sq(2)\n', "argument 1 of sq: expected float, got int")
    fails('extern fn sq(x: float) -> float = "x"\ny = sq()\n', "sq takes 1 argument")
    fails('extern fn f(x: int) -> int = "{1}"\n', "uses \\{1\\} but the function has 1 parameter")
    fails('extern fn f(x: int) -> int = "  "\n', "needs a Rust path or template")
    fails('extern fn f(x: int) -> int = "x"\nfn f(y: int) -> int:\n    return y\n', "already defined")
    fails('extern fn print(x: int) = "x"\n', "built-in function")
    fails('fn g():\n    extern fn f(x: int) = "x"\n', "expected an indented block|top level")


def test_extern_fn_may_shadow_a_shadowable_builtin():
    checked('extern fn sqrt(x: int) -> int = "{0}"\ny = sqrt(4)\n')


# ---- generated Rust ------------------------------------------------------------------------------------

def test_path_form_and_template_form_expand_by_value():
    rs = cli.transpile_source('extern fn hypot(a: float, b: float) -> float = "f64::hypot"\nextern fn shout(s: str) -> str = "{0}.to_uppercase()"\n'
                              'name = "ana"\nprint(hypot(3.0, 4.0))\nprint(shout(name), shout(name))\n')
    assert "(f64::hypot(3.0f64, 4.0f64))" in rs
    assert rs.count("((name.clone()).to_uppercase())") == 2
    assert "fn hypot" not in rs and "fn shout" not in rs


def test_placeholders_can_repeat_and_braces_survive():
    rs = cli.transpile_source('extern fn show(n: int) -> str = "format!(\\"{:x}-{0}\\", {0})"\nprint(show(255))\n')
    assert 'format!("{:x}-(255i64)", (255i64))' in rs  # every {0}, even inside the Rust string, is replaced


@needs_rustc
def test_extern_std_functions_run(tmp_path, capfd):
    prog = tmp_path / "p.zy"
    prog.write_text('extern fn hypot(a: float, b: float) -> float = "f64::hypot"\n'
                    'extern fn hex(n: int) -> str = "format!(\\"{:x}\\", {0})"\n'
                    'extern fn parse_hex(s: str) -> Result[int, str] = "i64::from_str_radix(&{0}, 16).map_err(|e| e.to_string())"\n'
                    'extern fn count(xs: list[str]) -> int = "{0}.len() as i64"\n'
                    'print(hypot(3.0, 4.0), hex(255), parse_hex("ff"), count(["a", "b"]))\n'
                    'print(parse_hex("zz") is err)\n')
    assert main(["run", str(prog)]) == 0
    assert capfd.readouterr().out == "5.0 ff Ok(255) 2\ntrue\n"


# ---- manifest and Cargo ----------------------------------------------------------------------------------

def test_rust_dependencies_and_trust_round_trip(tmp_path):
    m = Manifest("app", rust_dependencies={"rand": "0.8", "mylib": {"path": "../mylib"},
                                           "serde": {"version": "1", "features": ["derive"], "default-features": False}},
                 trusted_packages=["fastjson", "alpha"])
    p = tmp_path / "zylo.toml"
    p.write_text(dump_manifest(m))
    back = load_manifest(p)
    assert back.rust_dependencies == m.rust_dependencies and back.trusted_packages == ["alpha", "fastjson"]


@pytest.mark.parametrize("name,spec,match", [
    ("bad name", "1", "invalid crate name"),
    ("a", {"registry": "x"}, "unsupported key"),
    ("a", {"features": "x"}, "features must be a list"),
    ("a", {"default-features": "no"}, "default-features must be true or false"),
    ("a", {"version": 1}, "version must be a string"),
    ("a", 5, "expected a version string or a table"),
])
def test_validate_rust_dep_rejects(name, spec, match):
    with pytest.raises(PackageError, match=match):
        validate_rust_dep(name, spec)


def test_path_dependencies_are_not_allowed_in_packages():
    validate_rust_dep("a", {"path": "../a"})
    with pytest.raises(PackageError, match="may not use path dependencies"):
        validate_rust_dep("a", {"path": "../a"}, allow_path=False)


def test_cargo_toml_generation(tmp_path):
    t = cargo_toml({"rand": "0.8", "mylib": {"path": "mylib"}}, overflow_checks=False, base=tmp_path)
    assert 'rand = "0.8"' in t and f'path = "{(tmp_path / "mylib").resolve()}"' in t
    assert "overflow-checks = false" in t and t.rstrip().endswith("[workspace]")
    assert "overflow-checks = true" in cargo_toml({}, True, tmp_path)


def test_init_ignores_the_build_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "demo"]) == 0
    ignored = (tmp_path / ".gitignore").read_text().split()
    assert ".zylo-env/" in ignored and ".zylo-build/" in ignored


# ---- building with a crate --------------------------------------------------------------------------------

def make_crate(root: Path) -> None:
    (root / "mylib" / "src").mkdir(parents=True)
    (root / "mylib" / "Cargo.toml").write_text('[package]\nname = "mylib"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "mylib" / "src" / "lib.rs").write_text('pub fn double(x: i64) -> i64 { x * 2 }\npub fn greet(n: &str) -> String { format!("hello {}", n) }\n')


@needs_cargo
def test_program_using_a_local_crate_builds_and_runs(tmp_path, monkeypatch, capfd):
    make_crate(tmp_path)
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    (app / "zylo.toml").write_text('[package]\nname = "app"\n[rust-dependencies]\nmylib = { path = "../mylib" }\n')
    (app / "src" / "main.zy").write_text('extern fn double(x: int) -> int = "mylib::double"\nextern fn greet(n: str) -> str = "mylib::greet(&{0})"\n'
                                         'print(double(21), greet("zylo"))\n')
    monkeypatch.chdir(app)
    assert main(["run", "--offline", "src/main.zy"]) == 0
    assert capfd.readouterr().out == "42 hello zylo\n"
    assert (app / ".zylo-build" / "Cargo.toml").is_file()
    out = app / "prog"  # `build` also works and the binary is standalone
    assert main(["build", "--offline", "src/main.zy", "-o", str(out)]) == 0
    assert out.is_file()


@needs_cargo
def test_cargo_errors_are_reported_as_backend_errors(tmp_path, monkeypatch, capsys):
    app = tmp_path / "app"
    app.mkdir()
    (app / "zylo.toml").write_text('[package]\nname = "app"\n[rust-dependencies]\nnonexistent_crate_xyz = { path = "../nope" }\n')
    (app / "main.zy").write_text("print(1)\n")
    monkeypatch.chdir(app)
    assert main(["run", "--offline", "main.zy"]) == 1
    assert "cargo could not build the program" in capsys.readouterr().err


def test_without_rust_dependencies_cargo_is_not_needed(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "zylo.toml").write_text('[package]\nname = "app"\n')
    (app / "main.zy").write_text("print(1)\n")
    rs, crates, manifest = cli.compile_file(app / "main.zy")
    assert crates == {} and manifest.name == "app"


# ---- trust ------------------------------------------------------------------------------------------------------

def project_with_package(tmp_path: Path, pkg_files: dict[str, str], manifest_extra: str = "") -> Path:
    """A project whose environment already contains a package named `rustpkg`."""
    env = Env.create(tmp_path / ".zylo-env", force=True)
    for rel, text in pkg_files.items():
        f = env.packages_dir / "rustpkg" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    (tmp_path / "zylo.toml").write_text(f'[package]\nname = "app"\n{manifest_extra}')
    main_zy = tmp_path / "main.zy"
    main_zy.write_text("import rustpkg\nprint(rustpkg.shout(\"hi\"))\n")
    return main_zy


PKG = {"zylo.toml": '[package]\nname = "rustpkg"\n', "src/lib.zy": 'extern fn shout(s: str) -> str = "{0}.to_uppercase()"\n'}


def test_a_package_with_extern_fn_is_rejected_unless_trusted(tmp_path):
    main_zy = project_with_package(tmp_path, PKG)
    with pytest.raises(ZyloError) as e:
        cli.compile_file(main_zy)
    assert "package 'rustpkg' declares `extern fn`" in e.value.msg and "trusted-packages" in e.value.msg
    assert e.value.file.endswith("lib.zy") and e.value.line == 1


@needs_rustc
def test_a_trusted_package_may_use_extern_fn(tmp_path, monkeypatch, capfd):
    main_zy = project_with_package(tmp_path, PKG, '[rust]\ntrusted-packages = ["rustpkg"]\n')
    monkeypatch.chdir(tmp_path)
    assert main(["run", str(main_zy)]) == 0
    assert capfd.readouterr().out == "HI\n"


def test_crates_declared_by_packages_are_ignored_unless_trusted(tmp_path):
    files = {**PKG, "zylo.toml": '[package]\nname = "rustpkg"\n[rust-dependencies]\nrand = "0.8"\n'}
    main_zy = project_with_package(tmp_path, files)
    manifest, env = cli.project_context(main_zy)
    assert cli.rust_crates(manifest, env) == {}
    main_zy = project_with_package(tmp_path, files, '[rust]\ntrusted-packages = ["rustpkg"]\n')
    manifest, env = cli.project_context(main_zy)
    assert cli.rust_crates(manifest, env) == {"rand": "0.8"}


def test_trusted_package_crates_cannot_use_paths_or_conflict(tmp_path):
    files = {**PKG, "zylo.toml": '[package]\nname = "rustpkg"\n[rust-dependencies]\nevil = { path = "/etc" }\n'}
    manifest, env = cli.project_context(project_with_package(tmp_path, files, '[rust]\ntrusted-packages = ["rustpkg"]\n'))
    with pytest.raises(PackageError):
        cli.rust_crates(manifest, env)
    files = {**PKG, "zylo.toml": '[package]\nname = "rustpkg"\n[rust-dependencies]\nrand = "0.9"\n'}
    manifest, env = cli.project_context(project_with_package(tmp_path, files, '[rust]\ntrusted-packages = ["rustpkg"]\n[rust-dependencies]\nrand = "0.8"\n'))
    with pytest.raises(PackageError, match="conflicting requirements for crate 'rand'"):
        cli.rust_crates(manifest, env)


def test_local_modules_may_use_extern_fn_without_trust(tmp_path):
    (tmp_path / "ffi.zy").write_text('extern fn twice(n: int) -> int = "{0} * 2"\n')
    (tmp_path / "main.zy").write_text("from ffi import twice\nprint(twice(4))\n")
    rs, _, _ = cli.compile_file(tmp_path / "main.zy")
    assert "((4i64) * 2)" in rs
