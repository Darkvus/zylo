"""Language server completion, builtin hover and signature help, and code actions, over JSON-RPC on a multi-file workspace."""
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from zylo.domain.builtins import FUNCTIONS, METHODS
from zylo.interface.lsp import Server

SHAPES = '''struct Point:
    "A point in the plane."
    x: float
    y: float = 0.0
    "The height."

    fn move(self, dx: float, dy: float = 0.0) -> Point:
        "Moves the point."
        return Point(x=self.x + dx, y=self.y + dy)

    fn norm(self) -> float:
        return self.x

enum Shape:
    "A shape."
    Circle(float)
    "A circle with its radius."
    Rect(float, float)
    Empty

    fn sides(self) -> int:
        return 0

fn area(s: Shape) -> float:
    "Area of a shape."
    match s:
        Shape.Circle(r):
            return r * r
        Shape.Rect(w, h):
            return w * h
        Shape.Empty:
            return 0.0

fn _hidden() -> int:
    return 1
'''

UTIL = '''pub from shapes import Point

fn double(n: int) -> int:
    "Twice n."
    return n * 2
'''

MAIN = '''import shapes
from shapes import Point, Shape

glob = 3

fn total(p: Point, n: int = 2) -> float:
    names = ["a", "b"]
    scores = {"a": 1}
    tags = {1, 2}
    box = cell(1)
    maybe = parse_int("3")
    text = "hello"
    q = p.move(1.0)
    for i in range(n):
        inner = i
    return q.x
'''


class Workspace:
    """Files on disk and a language server with them open; every request replaces one line of `main.zy`."""

    def __init__(self, root: Path, files: dict[str, str]):
        self.root, self.files = root, dict(files)
        for name, text in files.items():
            (root / name).write_text(text)

    def uri(self, name: str) -> str:
        return (self.root / name).resolve().as_uri()

    def send(self, method: str, params: dict, texts: dict[str, str] | None = None):
        texts = {**self.files, **(texts or {})}
        msgs = [{"method": "textDocument/didOpen", "params": {"textDocument": {"uri": self.uri(n), "text": t}}}
                for n, t in texts.items()]
        msgs.append({"id": 1, "method": method, "params": params})
        reader = io.BytesIO(b"".join(frame(m) for m in msgs))
        writer = io.BytesIO()
        Server(reader, writer).serve()
        reply = next(m for m in parse_frames(writer.getvalue()) if m.get("id") == 1)
        assert "error" not in reply, reply
        return reply["result"]

    def typing(self, name: str, after: str, text: str, line_text: str | None = None):
        """Insert a line `text` after the line containing `after` and return (texts, line, character) of its end."""
        lines = self.files[name].split("\n")
        at = next(i for i, ln in enumerate(lines) if after in ln) + 1
        lines.insert(at, text)
        return {name: "\n".join(lines)}, at, len(text)

    def complete(self, text: str, name: str = "main.zy", after: str = "q = p.move(1.0)") -> list[dict]:
        texts, line, ch = self.typing(name, after, text)
        return self.send("textDocument/completion", {"textDocument": {"uri": self.uri(name)},
                                                     "position": {"line": line, "character": ch}}, texts)

    def labels(self, text: str, **kw) -> dict[str, dict]:
        return {i["label"]: i for i in self.complete(text, **kw)}

    def hover(self, name: str, needle: str, nth: int = 0, texts: dict[str, str] | None = None) -> str | None:
        src = (texts or self.files)[name]
        idx = -1
        for _ in range(nth + 1):
            idx = src.index(needle, idx + 1)
        line, ch = src[:idx].count("\n"), idx - (src[:idx].rfind("\n") + 1)
        r = self.send("textDocument/hover", {"textDocument": {"uri": self.uri(name)}, "position": {"line": line, "character": ch}}, texts)
        return r["contents"]["value"] if r else None

    def signature(self, text: str, name: str = "main.zy", after: str = "q = p.move(1.0)"):
        texts, line, ch = self.typing(name, after, text)
        return self.send("textDocument/signatureHelp", {"textDocument": {"uri": self.uri(name)},
                                                        "position": {"line": line, "character": ch}}, texts)

    def actions(self, name: str, line: int, character: int, texts: dict[str, str] | None = None) -> list[dict]:
        pos = {"line": line, "character": character}
        return self.send("textDocument/codeAction", {"textDocument": {"uri": self.uri(name)}, "range": {"start": pos, "end": pos},
                                                     "context": {"diagnostics": []}}, texts)

    def apply(self, action: dict, texts: dict[str, str] | None = None) -> dict[str, str]:
        """The new text of every file an action changes, by file name."""
        out = {}
        for uri, edits in action["edit"]["changes"].items():
            fname = next(n for n in self.files if self.uri(n) == uri)
            lines = (texts or self.files)[fname].split("\n")
            for e in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True):
                s, en = e["range"]["start"], e["range"]["end"]
                text = "\n".join(lines)
                offs = lambda pos: sum(len(lines[k]) + 1 for k in range(pos["line"])) + pos["character"]  # noqa: E731
                text = text[:offs(s)] + e["newText"] + text[offs(en):]
                lines = text.split("\n")
            out[fname] = "\n".join(lines)
        return out


def frame(message: dict) -> bytes:
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def parse_frames(data: bytes) -> list[dict]:
    out, i = [], 0
    while i < len(data):
        j = data.index(b"\r\n\r\n", i)
        n = int(data[i:j].split(b":")[1])
        out.append(json.loads(data[j + 4:j + 4 + n]))
        i = j + 4 + n
    return out


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path, {"shapes.zy": SHAPES, "util.zy": UTIL, "main.zy": MAIN})


# ---- protocol ------------------------------------------------------------------------------------------------------

def test_initialize_announces_completion_and_code_actions():
    reader = io.BytesIO(frame({"id": 1, "method": "initialize", "params": {}}))
    writer = io.BytesIO()
    Server(reader, writer).serve()
    caps = parse_frames(writer.getvalue())[0]["result"]["capabilities"]
    assert caps["completionProvider"]["triggerCharacters"] == ["."]
    assert caps["codeActionProvider"]["codeActionKinds"] == ["quickfix"]


# ---- members after a dot ---------------------------------------------------------------------------------------------

def test_a_struct_value_offers_its_fields_and_methods_with_kinds_details_and_docs(ws):
    items = ws.labels("    p.")
    assert set(items) == {"x", "y", "move", "norm"}
    assert items["x"]["kind"] == 5 and items["x"]["detail"] == "float"
    assert items["y"]["detail"] == "float = 0.0" and "The height." in items["y"]["documentation"]["value"]
    assert items["move"]["kind"] == 2 and items["move"]["detail"] == "(dx: float, dy: float = 0.0) -> Point"
    assert "Moves the point." in items["move"]["documentation"]["value"]


def test_the_type_of_a_value_comes_from_inference_through_calls_and_chains(ws):
    assert set(ws.labels("    q.")) == {"x", "y", "move", "norm"}
    assert set(ws.labels("    p.move(1.0).")) == {"x", "y", "move", "norm"}
    assert set(ws.labels("    z = q.move(2.0).mo")) == {"x", "y", "move", "norm"}  # the word being typed does not matter


def test_self_has_the_fields_and_methods_of_its_struct(tmp_path):
    w = Workspace(tmp_path, {"main.zy": "struct C:\n    n: int\n\n    fn bump(self) -> int:\n        return self.n\n"})
    texts, line, ch = w.typing("main.zy", "fn bump", "        self.")
    items = w.send("textDocument/completion", {"textDocument": {"uri": w.uri("main.zy")}, "position": {"line": line, "character": ch}}, texts)
    assert {i["label"] for i in items} == {"n", "bump"}


def test_an_enum_value_offers_its_methods(ws):
    w = Workspace(ws.root, {**ws.files, "main.zy": ws.files["main.zy"].replace("q = p.move(1.0)", "q = p.move(1.0)\n    sh = Shape.Empty")})
    assert set(w.labels("    sh.", after="sh = Shape.Empty")) == {"sides"}


def test_builtin_types_offer_their_methods_with_the_element_types_filled_in(ws):
    names = ws.labels("    names.")
    assert names["append"]["detail"] == "(value: str)" and names["pop"]["detail"] == "() -> str?"
    assert names["map"]["detail"] == "(f: fn(str) -> U) -> list[U]" and names["fold"]["kind"] == 2
    assert "Adds a value at the end." in names["append"]["documentation"]["value"]
    assert ws.labels("    scores.")["get"]["detail"] == "(key: str, default: int) -> int"
    assert set(ws.labels("    scores.")) == set(METHODS["dict"])
    assert set(ws.labels("    tags.")) == set(METHODS["set"])
    assert ws.labels("    tags.")["add"]["detail"] == "(value: int)"
    assert set(ws.labels("    box.")) == set(METHODS["Cell"])
    assert set(ws.labels("    maybe.")) == {"unwrap", "unwrap_or"}
    assert ws.labels("    maybe.")["unwrap"]["detail"] == "() -> int"
    assert set(ws.labels("    text.")) == set(METHODS["str"])


def test_a_string_literal_and_a_loop_variable_have_members_too(ws):
    assert "upper" in ws.labels('    "abc".')
    assert "upper" in ws.labels('    names[0].')
    assert "to_json" not in ws.labels("    p.")


def test_a_module_offers_its_public_exports_only(ws):
    items = ws.labels("    shapes.")
    assert set(items) == {"Point", "Shape", "area"}  # `_hidden` is private
    assert items["Point"]["kind"] == 22 and items["Shape"]["kind"] == 13
    assert items["area"]["kind"] == 3 and items["area"]["detail"] == "fn area(s: Shape) -> float"
    assert "Area of a shape." in items["area"]["documentation"]["value"]


def test_a_module_offers_what_it_re_exports(tmp_path):
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "util.zy": UTIL, "main.zy": "import util\n\nfn f():\n    x = 1\n"})
    assert set(w.labels("    util.", after="fn f():")) == {"Point", "double"}


def test_an_enum_offers_its_variants_also_through_a_module(ws):
    for text in ("    Shape.", "    shapes.Shape.", "    z = shapes.area(Shape."):
        items = ws.labels(text)
        assert set(items) == {"Circle", "Rect", "Empty"}, text
    items = ws.labels("    Shape.")
    assert items["Circle"]["kind"] == 20 and items["Circle"]["detail"] == "(float)"
    assert "A circle with its radius." in items["Circle"]["documentation"]["value"]
    assert items["Empty"]["detail"] == ""


def test_members_inside_if_conditions_and_f_strings(ws):
    assert set(ws.labels("    if p.")) == {"x", "y", "move", "norm"}
    assert set(ws.labels('    print(f"{p.')) == {"x", "y", "move", "norm"}
    assert ws.complete('    print("p.') == []  # inside a string
    assert ws.complete("    x = 1  # p.") == []  # inside a comment


def test_nothing_is_offered_after_a_number_or_an_unknown_value(ws):
    assert ws.complete("    x = 1.") == []
    assert ws.complete("    x = nope.") == []


# ---- names in scope --------------------------------------------------------------------------------------------------

def test_names_in_scope_are_ranked_before_builtins_and_keywords(ws):
    items = ws.labels("    ")
    assert items["p"]["kind"] == 6 and items["p"]["detail"] == "Point"
    assert items["names"]["detail"] == "list[str]" and items["q"]["detail"] == "Point"
    assert items["n"]["detail"] == "int"
    assert items["total"]["kind"] == 3 and items["total"]["detail"].startswith("fn total(")
    assert items["Point"]["kind"] == 22 and items["shapes"]["kind"] == 9
    assert items["len"]["kind"] == 3 and items["if"]["kind"] == 14
    assert items["p"]["sortText"] < items["total"]["sortText"] < items["len"]["sortText"] < items["if"]["sortText"]
    assert "glob" not in items  # a function does not see the variables of the top level
    assert "inner" not in items and "i" not in items  # they belong to the loop's block


def test_loop_and_match_variables_are_visible_inside_their_blocks(ws):
    texts, line, ch = ws.typing("main.zy", "inner = i", "        ")
    items = {i["label"] for i in ws.send("textDocument/completion", {"textDocument": {"uri": ws.uri("main.zy")},
                                                                     "position": {"line": line, "character": ch}}, texts)}
    assert {"i", "inner", "p", "q"} <= items
    texts, line, ch = ws.typing("shapes.zy", "Shape.Rect(w, h):", "            ")
    items = {i["label"] for i in ws.send("textDocument/completion", {"textDocument": {"uri": ws.uri("shapes.zy")},
                                                                     "position": {"line": line, "character": ch}}, texts)}
    assert {"w", "h", "s"} <= items and "r" not in items


def test_top_level_variables_are_visible_at_the_top_level(ws):
    texts = {"main.zy": MAIN + "\nprint(\n"}
    items = {i["label"] for i in ws.send("textDocument/completion", {"textDocument": {"uri": ws.uri("main.zy")},
                                                                     "position": {"line": MAIN.count("\n") + 1, "character": 6}}, texts)}
    assert "glob" in items and "total" in items and "p" not in items


def test_imported_names_are_offered_with_their_description(ws):
    items = ws.labels("    ")
    assert items["Shape"]["kind"] == 13
    w = Workspace(ws.root, {**ws.files, "main.zy": "from shapes import area as ar\nfrom util import double\n\nfn f():\n    x = 1\n"})
    found = w.labels("    ", after="x = 1")
    assert found["ar"]["detail"].startswith("fn area(") and found["double"]["detail"] == "fn double(n: int) -> int"
    assert "Twice n." in found["double"]["documentation"]["value"]


def test_completion_works_while_the_file_has_a_syntax_error_elsewhere(ws):
    broken = MAIN.replace("    maybe = parse_int(\"3\")\n", "    maybe = parse_int(\n")
    w = Workspace(ws.root, {**ws.files, "main.zy": broken})
    items = w.labels("    p.", after="q = p.move(1.0)")
    assert set(items) == {"x", "y", "move", "norm"}
    assert "q" in w.labels("    ", after="q = p.move(1.0)")


def test_the_unfinished_line_does_not_need_to_parse(ws):
    for text in ("    x = [p.", "    y = p.move(1.0, q.", "    if p.norm() > 1.0 and q.", "    return q."):
        assert "move" in ws.labels(text), text


# ---- types, arguments and imports ------------------------------------------------------------------------------------

def test_types_are_offered_where_a_type_is_expected(ws):
    for text in ("    z: ", "    z: list[", "fn g(a: int, b: ", "fn g(a: int) -> "):
        items = ws.labels(text)
        assert {"int", "str", "list", "Point", "Shape"} <= set(items), text
        assert "if" not in items and "print" not in items and "p" not in items, text
    assert items["int"]["kind"] == 7
    assert set(ws.labels("    z: shapes.")) == {"Point", "Shape"}


def test_struct_constructors_offer_their_fields_as_named_arguments(ws):
    items = ws.labels("    r = Point(")
    assert items["x="]["kind"] == 5 and items["x="]["detail"] == "float" and items["y="]["detail"] == "float = 0.0"
    assert items["x="]["sortText"] < items["p"]["sortText"]
    items = ws.labels("    r = Point(x=1.0, ")
    assert "y=" in items and "x=" not in items
    assert "x=" not in ws.labels("    r = Point(1.0, ") and "y=" in ws.labels("    r = Point(1.0, ")
    assert "x=" not in ws.labels("    r = Point(x=")  # a value is being written
    assert "x=" not in ws.labels("    r = [Point(x=1.0), ")  # inside a list, not in the call


def test_functions_and_methods_offer_their_parameters_as_named_arguments(ws):
    assert ws.labels("    r = total(p, ")["n="]["detail"] == "int = 2"
    assert ws.labels("    r = p.move(1.0, ")["dy="]["detail"] == "float = 0.0"
    assert "dx=" in ws.labels("    r = p.move(")
    assert ws.labels("    r = shapes.area(")["s="]["kind"] == 6


def test_import_lines_offer_modules_and_exports(ws):
    texts = {"main.zy": "import \n"}
    pos = {"textDocument": {"uri": ws.uri("main.zy")}, "position": {"line": 0, "character": 7}}
    items = {i["label"] for i in ws.send("textDocument/completion", pos, texts)}
    assert items == {"std", "shapes", "util"}
    texts = {"main.zy": "import std.\n"}
    pos["position"]["character"] = 11
    assert {i["label"] for i in ws.send("textDocument/completion", pos, texts)} >= {"json", "math", "os", "text"}
    texts = {"main.zy": "from shapes import \n"}
    pos["position"]["character"] = 19
    assert {i["label"] for i in ws.send("textDocument/completion", pos, texts)} == {"Point", "Shape", "area"}
    texts = {"main.zy": "from shapes import Point, \n"}
    pos["position"]["character"] = 26
    assert {i["label"] for i in ws.send("textDocument/completion", pos, texts)} == {"Shape", "area"}
    texts = {"main.zy": "from std.json import \n"}
    pos["position"]["character"] = 21
    assert "parse" in {i["label"] for i in ws.send("textDocument/completion", pos, texts)}


# ---- builtin hover and signature help ----------------------------------------------------------------------------------

def test_every_builtin_function_is_documented():
    from zylo.domain.builtins import BUILTINS
    assert set(FUNCTIONS) == BUILTINS


def test_the_method_catalog_matches_the_ones_the_checker_knows(tmp_path):
    """The checker lists the methods of each built-in type in its error; the catalog must list the same ones."""
    probes = {"str": '"a"', "list": "[1]", "dict": '{"a": 1}', "set": "{1}", "Cell": "cell(1)"}
    for kind, value in probes.items():
        p = tmp_path / f"{kind}.zy"
        p.write_text(f"x = {value}\nx.nope()\n")
        out = subprocess.run([sys.executable, "-m", "zylo", "check", str(p)], capture_output=True, text=True).stderr
        have = re.search(r"available: ([^)]*)\)", out)
        assert have, out
        assert set(have.group(1).split(", ")) == set(METHODS[kind]), kind
    p = tmp_path / "opt.zy"
    p.write_text('x = parse_int("1")\nx.nope()\n')
    out = subprocess.run([sys.executable, "-m", "zylo", "check", str(p)], capture_output=True, text=True).stderr
    assert set(re.search(r"available: ([^)]*)\)", out).group(1).split(", ")) == set(METHODS["Option"])


def test_hover_describes_builtin_functions(tmp_path):
    w = Workspace(tmp_path, {"main.zy": 'print(len("a"), sum([1]), parse_int("1"))\n'})
    assert "fn len(x: str | list | dict | set) -> int" in w.hover("main.zy", "len")
    assert "The number of characters" in w.hover("main.zy", "len")
    assert "fn print(*values)" in w.hover("main.zy", "print")
    assert "fn parse_int(text: str) -> int?" in w.hover("main.zy", "parse_int")


def test_hover_describes_builtin_methods_with_the_receivers_types(ws):
    src = ws.files["main.zy"].replace("    q = p.move(1.0)\n", "    q = p.move(1.0)\n    names.append(\"c\")\n    v = scores.get(\"a\", 0)\n"
                                      "    u = text.upper()\n    m = maybe.unwrap_or(0)\n")
    texts = {"main.zy": src}
    assert "(method) list[str].append(value: str)" in ws.hover("main.zy", "append", texts=texts)
    assert "Adds a value at the end." in ws.hover("main.zy", "append", texts=texts)
    assert "(method) dict[str, int].get(key: str, default: int) -> int" in ws.hover("main.zy", "get", texts=texts)
    assert "(method) str.upper() -> str" in ws.hover("main.zy", "upper", texts=texts)
    assert "(method) int?.unwrap_or(default: int) -> int" in ws.hover("main.zy", "unwrap_or", texts=texts)


def test_hover_on_a_user_method_named_like_a_builtin_still_shows_the_user_method(tmp_path):
    src = "struct Bag:\n    n: int\n\n    fn append(self, v: int) -> int:\n        return self.n + v\n\nb = Bag(n=1)\nprint(b.append(2))\n"
    w = Workspace(tmp_path, {"main.zy": src})
    assert "fn Bag.append(self, v: int) -> int" in w.hover("main.zy", "append", 1)


def test_signature_help_for_builtin_functions_and_methods(ws):
    sig = ws.signature("    n = len(")
    assert sig["signatures"][0]["label"] == "fn len(x: str | list | dict | set) -> int"
    assert sig["activeParameter"] == 0
    sig = ws.signature("    print(1, 2, ")
    assert sig["signatures"][0]["label"] == "fn print(*values)" and sig["activeParameter"] == 0
    sig = ws.signature("    names.insert(0, ")
    assert sig["signatures"][0]["label"] == "list[str].insert(index: int, value: str)"
    assert sig["activeParameter"] == 1
    assert "Inserts a value" in sig["signatures"][0]["documentation"]["value"]
    sig = ws.signature('    x = scores.get("a", ')
    assert sig["signatures"][0]["label"] == "dict[str, int].get(key: str, default: int) -> int" and sig["activeParameter"] == 1
    sig = ws.signature("    text.replace(")
    assert sig["signatures"][0]["label"] == "str.replace(old: str, new: str) -> str"


def test_builtin_methods_cannot_be_renamed_or_jumped_to(ws):
    src = ws.files["main.zy"].replace("    q = p.move(1.0)\n", "    q = p.move(1.0)\n    names.append(\"c\")\n")
    texts = {"main.zy": src}
    line = src[:src.index(".append") + 1].count("\n")
    ch = src.index(".append") - src.rfind("\n", 0, src.index(".append"))
    pos = {"textDocument": {"uri": ws.uri("main.zy")}, "position": {"line": line, "character": ch}}
    assert ws.send("textDocument/definition", pos, texts) == []
    reader = io.BytesIO(b"".join(frame(m) for m in [
        {"method": "textDocument/didOpen", "params": {"textDocument": {"uri": ws.uri("main.zy"), "text": src}}},
        {"id": 1, "method": "textDocument/prepareRename", "params": pos}]))
    writer = io.BytesIO()
    Server(reader, writer).serve()
    reply = next(m for m in parse_frames(writer.getvalue()) if m.get("id") == 1)
    assert "built-in method" in reply["error"]["message"]


def test_hover_keeps_its_types_when_another_statement_has_a_syntax_error(tmp_path):
    src = 'xs = [1, 2]\ny = (\nz = len(xs)\nxs.append(3)\n'
    w = Workspace(tmp_path, {"main.zy": src})
    assert "(variable) xs: list[int]" in w.hover("main.zy", "xs", 2)
    assert "(method) list[int].append(value: int)" in w.hover("main.zy", "append")


# ---- code actions --------------------------------------------------------------------------------------------------------

def line_of(text: str, needle: str) -> tuple[int, int]:
    idx = text.index(needle)
    return text[:idx].count("\n"), idx - (text[:idx].rfind("\n") + 1)


def test_quick_fix_adds_a_missing_import_from_the_workspace(tmp_path):
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "util.zy": UTIL, "main.zy": "fn f(n: int) -> int:\n    return double(n)\n"})
    line, ch = line_of(w.files["main.zy"], "double")
    actions = w.actions("main.zy", line, ch)
    assert [a["title"] for a in actions] == ["Import 'double' from 'util'"]
    assert actions[0]["kind"] == "quickfix"
    assert w.apply(actions[0])["main.zy"] == "from util import double\n\nfn f(n: int) -> int:\n    return double(n)\n"


def test_the_import_goes_after_the_existing_ones_and_joins_a_from_line_of_the_module(tmp_path):
    src = "import shapes\nfrom util import double\n\nfn f(s: Shape) -> float:\n    return area(s) + double(1)\n"
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "util.zy": UTIL, "main.zy": src})
    line, ch = line_of(src, "Shape")
    (action,) = w.actions("main.zy", line, ch)
    assert action["title"] == "Import 'Shape' from 'shapes'"
    assert w.apply(action)["main.zy"].startswith("import shapes\nfrom util import double\nfrom shapes import Shape\n")
    src2 = "from shapes import Point\n\nfn f(s: Shape) -> float:\n    return 1.0\n"
    w2 = Workspace(tmp_path, {"main.zy": src2})
    line, ch = line_of(src2, "Shape")
    (action,) = w2.actions("main.zy", line, ch)
    assert w2.apply(action)["main.zy"].startswith("from shapes import Point, Shape\n")


def test_the_import_of_a_name_found_in_the_standard_library(tmp_path):
    src = "fn f(s: str):\n    print(parse(s))\n"
    w = Workspace(tmp_path, {"main.zy": src})
    line, ch = line_of(src, "parse")
    titles = [a["title"] for a in w.actions("main.zy", line, ch)]
    assert "Import 'parse' from 'std.json'" in titles
    action = next(a for a in w.actions("main.zy", line, ch) if "std.json" in a["title"])
    assert w.apply(action)["main.zy"].startswith("from std.json import parse\n")


def test_a_module_written_before_a_dot_is_imported_as_a_module(tmp_path):
    src = "fn f(n: int) -> int:\n    return util.double(n)\n"
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "util.zy": UTIL, "main.zy": src})
    line, ch = line_of(src, "util")
    (action,) = w.actions("main.zy", line, ch)
    assert action["title"] == "Import the module 'util'"
    assert w.apply(action)["main.zy"] == "import util\n" + src
    src = "fn f(s: str):\n    x = json.parse(s)\n"
    w = Workspace(tmp_path, {"main.zy": src})
    line, ch = line_of(src, "json")
    assert [a["title"] for a in w.actions("main.zy", line, ch)] == ["Import the module 'std.json'"]


def test_no_import_is_offered_for_names_that_exist_or_are_private_or_unknown(tmp_path):
    src = "from util import double\n\nfn f(n: int) -> int:\n    x = _hidden()\n    y = nowhere(n)\n    return double(n) + len([1])\n"
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "util.zy": UTIL, "main.zy": src})
    for needle in ("_hidden", "nowhere", "double(n)", "len", "int", "n)"):
        assert w.actions("main.zy", *line_of(src, needle)) == [], needle


def test_a_name_exported_by_two_modules_gets_one_action_each(tmp_path):
    other = "fn double(n: int) -> int:\n    return n + n\n"
    src = "fn f() -> int:\n    return double(1)\n"
    w = Workspace(tmp_path, {"util.zy": UTIL, "other.zy": other, "main.zy": src})
    titles = [a["title"] for a in w.actions("main.zy", *line_of(src, "double"))]
    assert titles == ["Import 'double' from 'other'", "Import 'double' from 'util'"]


NON_EXHAUSTIVE = '''from shapes import Shape

fn describe(s: Shape) -> str:
    match s:
        Shape.Empty:
            return "empty"
'''


def test_quick_fix_adds_the_missing_match_arms(tmp_path):
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": NON_EXHAUSTIVE})
    (action,) = w.actions("main.zy", *line_of(NON_EXHAUSTIVE, "match"))
    assert action["title"] == "Add the missing match arms (Circle, Rect)"
    fixed = w.apply(action)["main.zy"]
    assert fixed == NON_EXHAUSTIVE + ('        Shape.Circle(_):\n            print("TODO: handle Shape.Circle")\n'
                                      '        Shape.Rect(_, _):\n            print("TODO: handle Shape.Rect")\n')


def test_the_fixed_match_covers_the_enum(tmp_path):
    from zylo.application.compile import diagnose_source
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": NON_EXHAUSTIVE})
    assert any("does not cover" in e.msg for e in diagnose_source(NON_EXHAUSTIVE, tmp_path / "main.zy"))
    (action,) = w.actions("main.zy", *line_of(NON_EXHAUSTIVE, "match"))
    fixed = w.apply(action)["main.zy"]
    assert not any("does not cover" in e.msg for e in diagnose_source(fixed, tmp_path / "main.zy"))


def test_the_match_arms_use_the_enum_as_the_other_arms_write_it_and_work_anywhere_inside_the_match(tmp_path):
    src = "import shapes\n\nfn f(s: shapes.Shape) -> int:\n    match s:\n        shapes.Shape.Empty:\n            return 1\n        shapes.Shape.Rect(a, b):\n            return 2\n"
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": src})
    line, _ = line_of(src, "return 2")
    (action,) = w.actions("main.zy", line, 14)
    assert action["title"] == "Add the missing match arm for shapes.Shape.Circle"
    assert w.apply(action)["main.zy"].endswith("            return 2\n        shapes.Shape.Circle(_):\n            print(\"TODO: handle shapes.Shape.Circle\")\n")


def test_the_arms_are_added_to_the_end_of_a_file_without_a_final_newline_and_inside_a_nested_match(tmp_path):
    src = NON_EXHAUSTIVE.rstrip("\n")
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": src})
    (action,) = w.actions("main.zy", *line_of(src, "match"))
    assert w.apply(action)["main.zy"].startswith(src + "\n        Shape.Circle(_):")
    nested = ("from shapes import Shape\n\nfn f(s: Shape, t: Shape) -> int:\n    match s:\n        Shape.Empty:\n"
              "            match t:\n                Shape.Empty:\n                    return 1\n        Shape.Circle(_):\n            return 2\n"
              "        Shape.Rect(_, _):\n            return 3\n")
    w = Workspace(tmp_path, {"main.zy": nested})
    line, ch = line_of(nested, "match t")
    (action,) = w.actions("main.zy", line, ch)
    fixed = w.apply(action)["main.zy"]
    assert "                Shape.Circle(_):\n                    print(" in fixed and fixed.count("Shape.Circle(_)") == 2


def test_no_match_arms_are_offered_when_nothing_is_missing(tmp_path):
    full = NON_EXHAUSTIVE + "        Shape.Circle(r):\n            return \"c\"\n        Shape.Rect(a, b):\n            return \"r\"\n"
    wild = NON_EXHAUSTIVE + "        _:\n            return \"other\"\n"
    guarded = NON_EXHAUSTIVE + "        Shape.Circle(r) if r > 1.0:\n            return \"c\"\n"
    for src in (full, wild):
        w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": src})
        assert w.actions("main.zy", *line_of(src, "match")) == []
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": guarded})
    (action,) = w.actions("main.zy", *line_of(guarded, "match"))
    assert "Circle, Rect" in action["title"]  # a guarded arm does not count
    w = Workspace(tmp_path, {"main.zy": "n = 3\nmatch n:\n    1:\n        print(1)\n"})
    assert w.actions("main.zy", 1, 0) == []  # only enums


def test_code_actions_are_empty_on_ordinary_code(ws):
    assert ws.actions("main.zy", 0, 3) == []
    assert ws.actions("main.zy", 999, 0) == []
