"""The VS Code extension: its grammar is generated from the language and must cover all of it."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

import zylo
from zylo.domain.builtins import BUILTINS
from zylo.interface.cli import build_parser
from zylo.syntax.lexer import KEYWORDS

ROOT = Path(__file__).parent.parent
EXT = ROOT / "editors" / "vscode"
GRAMMAR_FILE = EXT / "syntaxes" / "zylo.tmLanguage.json"


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_vscode_grammar", ROOT / "scripts" / "generate_vscode_grammar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patterns_in(node):
    """Every regular expression string in a grammar, with the JSON key it came from."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("match", "begin", "end") and isinstance(value, str):
                found.append((key, value))
            else:
                found += patterns_in(value)
    elif isinstance(node, list):
        for item in node:
            found += patterns_in(item)
    return found


@pytest.fixture(scope="module")
def grammar():
    return json.loads(GRAMMAR_FILE.read_text())


def test_the_committed_grammar_is_the_generated_one():
    assert GRAMMAR_FILE.read_text() == load_generator().render(), "run `python scripts/generate_vscode_grammar.py`"


def test_every_pattern_is_a_valid_regular_expression(grammar):
    patterns = patterns_in(grammar)
    assert len(patterns) > 20
    for _, pattern in patterns:
        re.compile(pattern)


def test_the_grammar_is_for_zylo_files(grammar):
    assert grammar["scopeName"] == "source.zylo" and grammar["fileTypes"] == ["zy"]
    for name in ("comments", "strings", "fstrings", "definitions", "keywords", "constants", "numbers", "builtins"):
        assert name in grammar["repository"]
    for include in grammar["patterns"]:  # every top-level include points at something that exists
        assert include["include"].lstrip("#") in grammar["repository"]


def test_every_keyword_is_highlighted(grammar):
    repo = grammar["repository"]
    keyword_patterns = [p["match"] for p in repo["keywords"]["patterns"]] + [repo["constants"]["match"]]
    for kw in KEYWORDS:
        assert any(re.search(p, kw) for p in keyword_patterns), f"keyword {kw!r} has no highlighting"


def test_every_builtin_is_highlighted_only_when_called(grammar):
    pattern = grammar["repository"]["builtins"]["match"]
    for name in BUILTINS:
        assert re.search(pattern, f"{name}(1)"), name
        assert re.search(pattern, f"{name} = 1") is None  # a variable with that name is not a call
    assert re.search(pattern, "my_print(1)") is None  # whole words only


def test_definitions_comments_and_numbers(grammar):
    repo = grammar["repository"]
    fn, struct = repo["definitions"]["patterns"][1]["match"], repo["definitions"]["patterns"][2]["match"]
    assert re.search(fn, "fn add(a: int)").group(2) == "add"
    assert re.search(struct, "struct Point:").group(2) == "Point"
    extern = repo["definitions"]["patterns"][0]["match"]
    assert re.search(extern, 'extern fn hypot(a: float) = "x"').group(3) == "hypot"
    assert re.search(repo["comments"]["match"], "x = 1  # note").group(0) == "# note"
    assert re.search(repo["numbers"]["patterns"][0]["match"], "x = 2.50").group(0) == "2.50"


def test_fstrings_start_only_at_an_f_prefix(grammar):
    begin = grammar["repository"]["fstrings"]["patterns"][0]["begin"]
    # (a `"f"` string is not an f-string either, but TextMate settles that by position: the plain string starts first)
    assert re.search(begin, 'x = f"hi {a}"') and re.search(begin, "x = f'hi'") is None and re.search(begin, 'buf"') is None


# ---- the extension manifest ------------------------------------------------------------------------

def test_package_json_registers_the_language_and_the_grammar():
    pkg = json.loads((EXT / "package.json").read_text())
    lang = pkg["contributes"]["languages"][0]
    assert lang["id"] == "zylo" and ".zy" in lang["extensions"]
    assert (EXT / lang["configuration"]).is_file()
    grammar_entry = pkg["contributes"]["grammars"][0]
    assert grammar_entry["scopeName"] == "source.zylo" and (EXT / grammar_entry["path"]).is_file()
    assert (EXT / pkg["main"]).is_file() and "onLanguage:zylo" in pkg["activationEvents"]
    assert "vscode-languageclient" in pkg["dependencies"]
    assert pkg["contributes"]["configuration"]["properties"]["zylo.path"]["default"] == "zylo"


def test_the_extension_version_follows_the_project():
    assert json.loads((EXT / "package.json").read_text())["version"] == zylo.__version__


def test_language_configuration_is_valid_and_uses_hash_comments():
    cfg = json.loads((EXT / "language-configuration.json").read_text())
    assert cfg["comments"]["lineComment"] == "#" and cfg["folding"]["offSide"] is True
    for rule in (cfg["indentationRules"]["increaseIndentPattern"], cfg["onEnterRules"][0]["beforeText"]):
        pattern = re.compile(rule)
        assert pattern.match("fn f(x: int) -> int:") and pattern.match("else:  # c") and not pattern.match("x = 1")


def test_the_client_starts_the_language_server_command():
    js = (EXT / "extension.js").read_text()
    assert '"lsp"' in js and "LanguageClient" in js
    commands = build_parser()._subparsers._group_actions[0].choices
    assert "lsp" in commands and "fmt" in commands  # what the extension and the docs promise exists


def test_the_package_ships_its_runtime_dependencies():
    """`vscode-languageclient` is needed at run time, so `.vscodeignore` must not exclude `node_modules`."""
    patterns = [ln.strip() for ln in (EXT / ".vscodeignore").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert not any("node_modules" in p for p in patterns)
    assert "vscode-languageclient" in json.loads((EXT / "package.json").read_text())["dependencies"]
