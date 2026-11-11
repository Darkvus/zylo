"""The layering rules of the package, enforced.

Dependencies point inwards: the domain depends on nothing, and an outer layer may use only the layers listed for it
below. See docs/architecture.md.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent / "zylo"

ALLOWED = {
    "domain": set(),
    "syntax": {"domain"},
    "packaging": {"domain"},
    "semantics": {"domain", "syntax", "packaging"},
    "generation": {"domain"},
    "infrastructure": {"domain", "packaging"},
    "application": {"domain", "syntax", "semantics", "generation", "packaging", "infrastructure"},
    "interface": {"domain", "application"},
}
FILES = sorted(p for layer in ALLOWED for p in (ROOT / layer).glob("*.py"))


def zylo_layers_imported(path: Path) -> set[str]:
    """Layers of the package that a file imports (`zylo.<layer>...`)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            parts = node.module.split(".")
            if parts[0] == "zylo" and len(parts) > 1:
                found.add(parts[1])
            elif node.module == "zylo":
                found.update(a.name for a in node.names if a.name in ALLOWED)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "zylo" and len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            pytest.fail(f"{path.relative_to(ROOT)}: use absolute imports (`from zylo.<layer> import ...`)")
    return found


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_layer_only_imports_the_layers_it_may(path: Path):
    layer = path.parent.name
    used = zylo_layers_imported(path) - {layer}
    forbidden = used - ALLOWED[layer]
    assert not forbidden, (
        f"{layer}/{path.name} imports from {sorted(forbidden)}, but the {layer} layer may only use {sorted(ALLOWED[layer])}")


def test_every_module_belongs_to_a_layer():
    top_level = {p.name for p in ROOT.glob("*.py")}
    assert top_level == {"__init__.py", "__main__.py"}
    packages = {p.name for p in ROOT.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert packages == set(ALLOWED)


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_every_layer_documents_its_role(layer: str):
    doc = ast.get_docstring(ast.parse((ROOT / layer / "__init__.py").read_text()))
    assert doc and len(doc.splitlines()) >= 3, f"zylo/{layer}/__init__.py must describe the layer"


def test_the_domain_is_pure():
    """No input or output, and no process or network access, in the innermost layer."""
    banned = {"os", "subprocess", "shutil", "socket", "pathlib", "tempfile", "urllib", "http"}
    for path in (ROOT / "domain").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module else []
            assert not (set(names) & banned), f"domain/{path.name} imports {set(names) & banned}"


def test_the_installer_receives_its_downloader():
    """The downloader is a constructor argument, so packaging does not depend on the git adapter."""
    tree = ast.parse((ROOT / "packaging" / "installer.py").read_text())
    installer = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Installer")
    init = next(n for n in installer.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    assert "fetcher" in [a.arg for a in init.args.args]
