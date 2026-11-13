"""Language server navigation: hover, go to definition, rename and signature help (single files and projects)."""
import io
import json
from pathlib import Path

import pytest

from zylo.application.editor import hover, open_project, rename, signature_help
from zylo.interface.lsp import Server, to_character, to_column
from zylo.semantics.navigation import RenameError


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


SHAPES = '''struct Point:
    "A point in the plane."
    x: float
    y: float = 0.0
    "The height."

    fn move(self, dx: float, dy: float = 0.0) -> Point:
        "Moves the point."
        return Point(x=self.x + dx, y=self.y + dy)

enum Shape:
    "A shape."
    Circle(float)
    "A circle with its radius."
    Empty

fn area(s: Shape) -> float:
    "Area of a shape."
    match s:
        Shape.Circle(r):
            return r * r
        Shape.Empty:
            return 0.0
'''

MAIN = '''import shapes
from shapes import Point, area as ar

fn total(p: Point, n: int = 2) -> float:
    q = p.move(1.0, dy=2.0)
    for i in range(n):
        q = q.move(float(i))
    return q.x + ar(shapes.Shape.Circle(2.0)) + shapes.area(shapes.Shape.Empty)

p = Point(x=1.0)
t = total(p)
print(f"{t} {p.x}")
'''


def where(text: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """0-based (line, character) of the `nth` occurrence of `needle`."""
    idx = text.rindex(needle) if nth < 0 else -1
    for _ in range(nth + 1 if nth >= 0 else 0):
        idx = text.index(needle, idx + 1)
    before = text[:idx]
    return before.count("\n"), idx - (before.rfind("\n") + 1)


class Workspace:
    """Files on disk plus an in-process language server with some of them open."""

    def __init__(self, root: Path, files: dict[str, str], opened: list[str] | None = None):
        self.root = root
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        self.texts = dict(files)
        self.opened = opened if opened is not None else list(files)
        self.next_id = 100

    def uri(self, name: str) -> str:
        return (self.root / name).resolve().as_uri()

    def request(self, method: str, name: str, needle: str, nth: int = 0, extra: dict | None = None, shift: int = 0,
                texts: dict[str, str] | None = None) -> dict:
        """Send one request at the position of `needle` in file `name`; return the whole reply message."""
        line, ch = where((texts or self.texts)[name], needle, nth)
        params = {"textDocument": {"uri": self.uri(name)}, "position": {"line": line, "character": ch + shift}, **(extra or {})}
        opens = [{"method": "textDocument/didOpen", "params": {"textDocument": {"uri": self.uri(n), "text": (texts or self.texts)[n]}}}
                 for n in dict.fromkeys([*self.opened, *(texts or {})])]
        reader = io.BytesIO(b"".join(frame(m) for m in [*opens, {"id": 1, "method": method, "params": params}]))
        writer = io.BytesIO()
        Server(reader, writer).serve()
        return next(m for m in parse_frames(writer.getvalue()) if m.get("id") == 1)

    def hover(self, name: str, needle: str, nth: int = 0, shift: int = 0) -> str | None:
        r = self.request("textDocument/hover", name, needle, nth, shift=shift)["result"]
        return r["contents"]["value"] if r else None

    def definition(self, name: str, needle: str, nth: int = 0, shift: int = 0) -> list[tuple[str, int, int]]:
        r = self.request("textDocument/definition", name, needle, nth, shift=shift)["result"]
        return [(Path(loc["uri"][7:]).name, loc["range"]["start"]["line"], loc["range"]["start"]["character"]) for loc in r]

    def rename(self, name: str, needle: str, new: str, nth: int = 0, texts: dict[str, str] | None = None) -> dict:
        return self.request("textDocument/rename", name, needle, nth, {"newName": new}, texts=texts)

    def renamed(self, name: str, needle: str, new: str, nth: int = 0) -> dict[str, str]:
        """Apply a rename and return the new text of every file it touched, by file name."""
        reply = self.rename(name, needle, new, nth)
        assert "error" not in reply, reply
        out = {}
        for uri, edits in reply["result"]["changes"].items():
            fname = next(n for n in self.texts if self.uri(n) == uri)
            lines = self.texts[fname].split("\n")
            for e in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True):
                s, en = e["range"]["start"], e["range"]["end"]
                assert s["line"] == en["line"]
                row = lines[s["line"]]
                lines[s["line"]] = row[:s["character"]] + e["newText"] + row[en["character"]:]
            out[fname] = "\n".join(lines)
        return out

    def prepare(self, name: str, needle: str, nth: int = 0) -> dict:
        return self.request("textDocument/prepareRename", name, needle, nth)

    def signature(self, name: str, needle: str, nth: int = 0, shift: int = 0, texts: dict[str, str] | None = None):
        return self.request("textDocument/signatureHelp", name, needle, nth, shift=shift, texts=texts)["result"]


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": MAIN})


@pytest.fixture
def solo(tmp_path):
    def make(text: str) -> Workspace:
        return Workspace(tmp_path, {"doc.zy": text})
    return make


# ---- protocol ---------------------------------------------------------------------------------------------

def test_initialize_announces_the_navigation_capabilities():
    reader = io.BytesIO(frame({"id": 1, "method": "initialize", "params": {}}))
    writer = io.BytesIO()
    Server(reader, writer).serve()
    caps = parse_frames(writer.getvalue())[0]["result"]["capabilities"]
    assert caps["hoverProvider"] and caps["definitionProvider"]
    assert caps["renameProvider"] == {"prepareProvider": True}
    assert caps["signatureHelpProvider"]["triggerCharacters"] == ["(", ","]


def test_utf16_columns_round_trip_around_astral_characters():
    text = 'x = "😀" + y'
    col = to_column(text, 11)  # the `y` sits after a character that takes two UTF-16 units
    assert text[col - 1] == "y"
    assert to_character(text, col) == 11
    assert to_column("abc", 99) > 3


# ---- hover ----------------------------------------------------------------------------------------------

def test_hover_shows_the_type_of_a_variable(solo):
    w = solo("n = 3\nxs = [n, 4]\nname = \"a\"\nprint(xs, name)\n")
    assert "(variable) xs: list[int]" in w.hover("doc.zy", "xs", 1)
    assert "(variable) name: str" in w.hover("doc.zy", "name", 1)
    assert "(variable) n: int" in w.hover("doc.zy", "n", 0)


def test_hover_shows_the_narrowed_type_inside_an_is_check(solo):
    w = solo("fn f(x: int?) -> int:\n    if x is some:\n        return x + 1\n    return 0\n")
    assert "(parameter) x: int?" in w.hover("doc.zy", "x", 0)
    assert "(variable) x: int" not in w.hover("doc.zy", "x", 1)
    assert w.hover("doc.zy", "x + 1") .endswith("x: int\n```")


def test_hover_on_parameters_loop_variables_and_pattern_variables(ws):
    assert "(parameter) p: Point\n" in ws.hover("main.zy", "p: Point")
    assert "(variable) i: int" in ws.hover("main.zy", "i in")
    assert "(variable) r: float" in ws.hover("shapes.zy", "r)")
    assert "(variable) r: float" in ws.hover("shapes.zy", "r * r")


def test_hover_on_a_function_shows_signature_and_description(ws):
    text = ws.hover("main.zy", "total", 1)
    assert "fn total(p: Point, n: int = 2) -> float" in text
    text = ws.hover("shapes.zy", "area")
    assert "fn area(s: Shape) -> float" in text and "Area of a shape." in text


def test_hover_on_a_struct_lists_its_fields_and_description(ws):
    text = ws.hover("shapes.zy", "Point")
    assert "struct Point" in text and "x: float" in text and "y: float = 0.0" in text and "A point in the plane." in text


def test_hover_on_an_enum_and_a_variant(ws):
    enum = ws.hover("shapes.zy", "Shape")
    assert "enum Shape" in enum and "Circle(float)" in enum and "Empty" in enum and "A shape." in enum
    variant = ws.hover("main.zy", "Circle")
    assert "Shape.Circle(float)" in variant and "A circle with its radius." in variant


def test_hover_on_a_field_and_a_method(ws):
    field = ws.hover("main.zy", "x", 2, shift=0)  # `q.x`
    assert "(field) Point.x: float" in field
    default = ws.hover("shapes.zy", "y: float")
    assert "(field) Point.y: float = 0.0" in default and "The height." in default
    method = ws.hover("main.zy", "move")
    assert "fn Point.move(self, dx: float, dy: float = 0.0) -> Point" in method and "Moves the point." in method


def test_hover_follows_imports_and_aliases(ws):
    assert "fn area(s: Shape) -> float" in ws.hover("main.zy", "ar", 1)
    assert "struct Point" in ws.hover("main.zy", "Point", 0)
    assert "module shapes" in ws.hover("main.zy", "shapes", 1)


def test_hover_on_builtins_and_on_nothing(solo):
    w = solo("print(len([1]))\n")
    assert "(built-in) len" in w.hover("doc.zy", "len")
    assert w.hover("doc.zy", "(") is None
    assert w.hover("doc.zy", "1") is None
    assert w.hover("doc.zy", "print", shift=5) is None  # the mouse is on the parenthesis, not on `print`


def test_hover_still_describes_definitions_in_a_program_with_errors(solo):
    w = solo('fn f(a: int) -> int:\n    "Doubles."\n    return a * 2\n\nprint(f(zzz))\n')
    text = w.hover("doc.zy", "f(zzz")
    assert "fn f(a: int) -> int" in text and "Doubles." in text


def test_hover_and_navigation_survive_a_syntax_error_elsewhere(solo):
    w = solo("fn f(a: int) -> int:\n    return a\n\nx = (\nprint(f(1))\n")
    assert "fn f(a: int) -> int" in w.hover("doc.zy", "f", 1)


def test_hover_reads_the_unsaved_buffer_not_the_disk(ws):
    text = ws.hover("main.zy", "total", 1)
    assert "n: int = 2" in text
    edited = MAIN.replace("n: int = 2", "n: int = 7")
    ws.texts["main.zy"] = edited
    assert "n: int = 7" in ws.hover("main.zy", "total", 1)


# ---- go to definition -------------------------------------------------------------------------------------------

def test_definition_of_locals_and_parameters(ws):
    assert ws.definition("main.zy", "q", 3) == [("main.zy", 4, 4)]  # `q.x` -> `q = ...`
    assert ws.definition("main.zy", "n)") == [("main.zy", 3, 19)]  # `range(n)` -> the parameter
    assert ws.definition("main.zy", "i)") == [("main.zy", 5, 8)]  # the loop variable
    assert ws.definition("shapes.zy", "r * r") == [("shapes.zy", 19, 21)]  # the pattern variable


def test_definition_of_a_variable_reassigned_goes_to_the_first_assignment(solo):
    w = solo("a = 1\na = 2\nprint(a)\n")
    assert w.definition("doc.zy", "a)") == [("doc.zy", 0, 0)]


def test_definition_across_modules(ws):
    assert ws.definition("main.zy", "Point", 1) == [("shapes.zy", 0, 7)]
    assert ws.definition("main.zy", "total(p)") == [("main.zy", 3, 3)]
    assert ws.definition("main.zy", "area(shapes.Shape.Empty)") == [("shapes.zy", 16, 3)]  # `shapes.area(...)`
    assert ws.definition("main.zy", "ar(shapes") == [("shapes.zy", 16, 3)]  # through the alias
    assert ws.definition("main.zy", "Circle") == [("shapes.zy", 12, 4)]
    assert ws.definition("main.zy", "Shape", 0) == [("shapes.zy", 10, 5)]


def test_definition_of_an_import_goes_to_the_module_file(ws):
    assert ws.definition("main.zy", "shapes", 0) == [("shapes.zy", 0, 0)]
    assert ws.definition("main.zy", "shapes", 3) == [("shapes.zy", 0, 0)]  # a use of the module name


def test_definition_of_fields_and_methods(ws):
    assert ws.definition("main.zy", "x", 2) == [("shapes.zy", 2, 4)]  # `q.x`
    assert ws.definition("main.zy", "move") == [("shapes.zy", 6, 7)]
    assert ws.definition("shapes.zy", "x + dx") == [("shapes.zy", 2, 4)]  # `self.x`
    assert ws.definition("main.zy", "x}") == [("shapes.zy", 2, 4)]  # `p.x` in the f-string


def test_definition_of_self_goes_to_its_type(ws):
    assert ws.definition("shapes.zy", "self.x") == [("shapes.zy", 0, 7)]


def test_definition_of_named_arguments(ws):
    assert ws.definition("main.zy", "dy=") == [("shapes.zy", 6, 29)]  # the method's parameter
    assert ws.definition("main.zy", "x=1.0") == [("shapes.zy", 2, 4)]  # a constructor's field


def test_definition_of_builtins_and_unknown_names_is_empty(solo):
    w = solo("print(zzz)\n")
    assert w.definition("doc.zy", "print") == []
    assert w.definition("doc.zy", "zzz") == []


def test_definition_follows_a_chain_of_reexports(tmp_path):
    w = Workspace(tmp_path, {
        "core.zy": "fn helper() -> int:\n    return 1\n",
        "mid.zy": "pub from core import helper\n",
        "top.zy": "pub from mid import helper\n",
        "main.zy": "from top import helper\nprint(helper())\n",
    }, opened=["main.zy"])
    assert w.definition("main.zy", "helper", 0) == [("core.zy", 0, 3)]
    assert w.definition("main.zy", "helper()") == [("core.zy", 0, 3)]


def test_definition_through_a_renamed_reexport(tmp_path):
    w = Workspace(tmp_path, {
        "core.zy": "fn helper() -> int:\n    return 1\n",
        "mid.zy": "pub from core import helper as aid\n",
        "main.zy": "from mid import aid\nprint(aid())\n",
    }, opened=["main.zy"])
    assert w.definition("main.zy", "aid()") == [("core.zy", 0, 3)]


# ---- rename: locals ---------------------------------------------------------------------------------------

def test_rename_a_local_only_touches_its_own_function(solo):
    w = solo("fn a() -> int:\n    n = 1\n    return n\n\nfn b() -> int:\n    n = 2\n    return n + 1\n")
    out = w.renamed("doc.zy", "n = 1", "count")
    assert out["doc.zy"] == ("fn a() -> int:\n    count = 1\n    return count\n\nfn b() -> int:\n    n = 2\n    return n + 1\n")


def test_rename_a_variable_renames_every_assignment_and_use_in_its_scope(solo):
    w = solo("total = 0\nfor i in range(3):\n    total = total + i\nprint(total)\n")
    out = w.renamed("doc.zy", "total", "acc", 2)
    assert out["doc.zy"] == "acc = 0\nfor i in range(3):\n    acc = acc + i\nprint(acc)\n"


def test_rename_distinguishes_same_names_in_sibling_scopes(solo):
    w = solo("c = true\nif c:\n    t = 1\n    print(t)\nelse:\n    t = 2\n    print(t)\n")
    out = w.renamed("doc.zy", "t = 1", "u")
    assert out["doc.zy"] == "c = true\nif c:\n    u = 1\n    print(u)\nelse:\n    t = 2\n    print(t)\n"


def test_rename_a_parameter_updates_named_arguments_in_other_files(ws):
    out = ws.renamed("shapes.zy", "dy: float", "delta")
    assert "dy" not in out["shapes.zy"].split("fn move")[1].split("return")[0].replace("delta", "")
    assert "fn move(self, dx: float, delta: float = 0.0)" in out["shapes.zy"]
    assert "return Point(x=self.x + dx, y=self.y + delta)" in out["shapes.zy"]
    assert "p.move(1.0, delta=2.0)" in out["main.zy"]


def test_rename_a_closure_parameter_and_a_captured_variable(solo):
    w = solo("k = 10\nf = fn(x: int) => x + k\nprint(f(1))\n")
    assert w.renamed("doc.zy", "x", "v")["doc.zy"] == "k = 10\nf = fn(v: int) => v + k\nprint(f(1))\n"
    assert w.renamed("doc.zy", "k", "bonus")["doc.zy"] == "bonus = 10\nf = fn(x: int) => x + bonus\nprint(f(1))\n"


def test_rename_loop_and_pattern_variables(ws):
    out = ws.renamed("shapes.zy", "r)", "radius")
    assert "Shape.Circle(radius):\n            return radius * radius" in out["shapes.zy"]
    out = ws.renamed("main.zy", "i in", "k")
    assert "for k in range(n):" in out["main.zy"] and "q.move(float(k))" in out["main.zy"]


def test_rename_a_local_function(solo):
    w = solo("fn outer() -> int:\n    fn inner(a: int) -> int:\n        return a + 1\n    return inner(1)\n")
    out = w.renamed("doc.zy", "inner", "step")
    assert "fn step(a: int)" in out["doc.zy"] and "return step(1)" in out["doc.zy"]


def test_rename_a_variable_to_a_name_in_scope_is_rejected(solo):
    w = solo("a = 1\nb = 2\nprint(a + b)\n")
    err = w.rename("doc.zy", "a", "b")["error"]
    assert "already" in err["message"] and err["code"] == -32803


def test_rename_a_variable_to_a_function_it_calls_is_rejected(solo):
    w = solo("fn go() -> int:\n    return 1\n\nfn f() -> int:\n    x = 2\n    return x + go()\n")
    err = w.rename("doc.zy", "x", "go")["error"]
    assert "clash" in err["message"]


def test_rename_a_parameter_to_another_parameter_is_rejected(solo):
    w = solo("fn f(a: int, b: int) -> int:\n    return a + b\n")
    assert "already" in w.rename("doc.zy", "a: int", "b")["error"]["message"]


# ---- rename: top-level items across files -------------------------------------------------------------------------

def test_rename_a_function_in_every_file_that_uses_it(ws):
    out = ws.renamed("shapes.zy", "area", "surface")
    assert "fn surface(s: Shape)" in out["shapes.zy"]
    assert "from shapes import Point, surface as ar" in out["main.zy"]
    assert "shapes.surface(shapes.Shape.Empty)" in out["main.zy"]
    assert "ar(shapes.Shape.Circle(2.0))" in out["main.zy"]  # the local alias keeps its name


def test_rename_an_alias_only_changes_the_importing_file(ws):
    out = ws.renamed("main.zy", "ar", "surface", 1)
    assert set(out) == {"main.zy"}
    assert "area as surface" in out["main.zy"] and "surface(shapes.Shape.Circle(2.0))" in out["main.zy"]


def test_rename_a_function_from_a_use_site(ws):
    out = ws.renamed("main.zy", "total", "sum_up", 1)
    assert "fn sum_up(p: Point" in out["main.zy"] and "t = sum_up(p)" in out["main.zy"]


def test_rename_a_struct_everywhere(ws):
    out = ws.renamed("shapes.zy", "Point", "Vec2")
    assert out["shapes.zy"].startswith("struct Vec2:") and "-> Vec2:" in out["shapes.zy"] and "return Vec2(x=" in out["shapes.zy"]
    assert "from shapes import Vec2, area as ar" in out["main.zy"]
    assert "fn total(p: Vec2" in out["main.zy"] and "p = Vec2(x=1.0)" in out["main.zy"]


def test_rename_an_enum_and_its_variants(ws):
    out = ws.renamed("shapes.zy", "Shape", "Figure")
    assert "enum Figure:" in out["shapes.zy"] and "fn area(s: Figure)" in out["shapes.zy"]
    assert "Figure.Circle(r):" in out["shapes.zy"]
    assert "shapes.Figure.Circle(2.0)" in out["main.zy"]
    out = ws.renamed("main.zy", "Circle", "Disc")
    assert "Shape.Disc(float)" not in out["shapes.zy"] and "    Disc(float)" in out["shapes.zy"]
    assert "Shape.Disc(r):" in out["shapes.zy"] and "shapes.Shape.Disc(2.0)" in out["main.zy"]


def test_rename_through_a_reexport_chain(tmp_path):
    files = {
        "core.zy": "fn helper() -> int:\n    return 1\n",
        "mid.zy": "pub from core import helper\n",
        "main.zy": "from mid import helper\nprint(helper())\n",
    }
    w = Workspace(tmp_path, files, opened=["main.zy"])
    out = w.renamed("main.zy", "helper()", "assist")
    assert out["core.zy"].startswith("fn assist()")
    assert out["mid.zy"] == "pub from core import assist\n"
    assert out["main.zy"] == "from mid import assist\nprint(assist())\n"


def test_rename_in_a_nested_package_directory(tmp_path):
    files = {
        "lib/util.zy": "fn twice(n: int) -> int:\n    return n * 2\n",
        "main.zy": "import lib.util\nprint(util.twice(2))\n",
    }
    w = Workspace(tmp_path, files, opened=["main.zy"])
    out = w.renamed("main.zy", "twice", "double")
    assert out["lib/util.zy"].startswith("fn double(") and "util.double(2)" in out["main.zy"]


def test_rename_a_name_that_collides_with_another_definition_is_rejected(ws):
    assert "already defined" in ws.rename("shapes.zy", "area", "Point")["error"]["message"]
    assert "already defined" in ws.rename("main.zy", "total", "ar", 1)["error"]["message"]


def test_rename_to_a_name_a_user_has_as_a_variable_is_rejected(tmp_path):
    w = Workspace(tmp_path, {"main.zy": "fn go() -> int:\n    return 1\n\nfn f() -> int:\n    n = go()\n    return n\n"})
    assert "already a variable" in w.rename("main.zy", "go", "n")["error"]["message"]


def test_a_rename_is_rejected_when_a_use_would_become_private(ws):
    assert "private" in ws.rename("shapes.zy", "area", "_area")["error"]["message"]


# ---- rename: members ------------------------------------------------------------------------------------------------

def test_rename_a_field_in_accesses_constructors_and_named_arguments(ws):
    out = ws.renamed("shapes.zy", "x: float", "px")
    assert "    px: float" in out["shapes.zy"]
    assert "Point(px=self.px + dx, y=self.y + dy)" in out["shapes.zy"]
    assert "p = Point(px=1.0)" in out["main.zy"] and "return q.px +" in out["main.zy"]
    assert "{t} {p.px}" in out["main.zy"]


def test_rename_a_field_from_a_use_site_in_another_file(ws):
    out = ws.renamed("main.zy", "x", "col", 2)
    assert "    col: float" in out["shapes.zy"] and "q.col" in out["main.zy"]


def test_rename_a_method(ws):
    out = ws.renamed("main.zy", "move", "shift")
    assert "fn shift(self, dx: float" in out["shapes.zy"]
    assert "p.shift(1.0, dy=2.0)" in out["main.zy"] and "q.shift(float(i))" in out["main.zy"]


def test_rename_a_member_to_an_existing_member_is_rejected(ws):
    assert "already has a member" in ws.rename("shapes.zy", "x: float", "y")["error"]["message"]
    assert "already has a member" in ws.rename("shapes.zy", "move", "x")["error"]["message"]


def test_renaming_a_field_leaves_other_structs_with_the_same_field_alone(solo):
    w = solo("struct A:\n    n: int\n\nstruct B:\n    n: int\n\na = A(n=1)\nb = B(n=2)\nprint(a.n + b.n)\n")
    out = w.renamed("doc.zy", "n: int", "count")["doc.zy"]
    assert "struct A:\n    count: int" in out and "struct B:\n    n: int" in out
    assert "A(count=1)" in out and "B(n=2)" in out and "a.count + b.n" in out


def test_renaming_a_field_is_refused_when_a_receiver_type_is_unknown(solo):
    w = solo("struct A:\n    n: int\n\nfn f(a: A) -> int:\n    return a.n + zzz.n\n")
    err = w.rename("doc.zy", "n: int", "count")["error"]
    assert "type of a value" in err["message"]


# ---- rename: validation -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["if", "struct", "none", "1abc", "a b", "", "a-b", "self", "_"])
def test_rename_rejects_keywords_and_invalid_identifiers(solo, bad):
    w = solo("a = 1\nprint(a)\n")
    assert w.rename("doc.zy", "a", bad)["error"]["code"] == -32803


@pytest.mark.parametrize("bad", ["print", "int", "Result", "len"])
def test_rename_rejects_built_in_names(solo, bad):
    w = solo("fn f() -> int:\n    return 1\n\nprint(f())\n")
    assert "built-in" in w.rename("doc.zy", "f()", bad)["error"]["message"]


def test_rename_to_the_same_name_changes_nothing(solo):
    w = solo("a = 1\nprint(a)\n")
    assert w.rename("doc.zy", "a", "a")["result"] == {"changes": {}}


def test_rename_of_builtins_modules_and_self_is_rejected(ws):
    assert "built-in" in ws.rename("main.zy", "range", "span")["error"]["message"]
    assert "module" in ws.rename("main.zy", "shapes", "figures")["error"]["message"]
    assert "self" in ws.rename("shapes.zy", "self.x", "me")["error"]["message"]


def test_rename_of_a_standard_library_function_is_rejected(tmp_path):
    w = Workspace(tmp_path, {"main.zy": "from std.math import gcd\nprint(gcd(4, 6))\n"})
    err = w.rename("main.zy", "gcd(4", "divisor")["error"]
    assert "outside this workspace" in err["message"]


def test_rename_where_there_is_no_name_fails_cleanly(solo):
    w = solo("a = 1\n")
    assert w.rename("doc.zy", "= 1", "b")["error"]["code"] == -32803


# ---- prepareRename -------------------------------------------------------------------------------------------------------

def test_prepare_rename_gives_the_range_and_placeholder(ws):
    r = ws.prepare("main.zy", "total", 1)["result"]
    line, ch = where(MAIN, "total", 1)
    assert r == {"range": {"start": {"line": line, "character": ch}, "end": {"line": line, "character": ch + 5}},
                 "placeholder": "total"}


def test_prepare_rename_refuses_what_cannot_be_renamed_and_ignores_the_rest(ws):
    assert "built-in" in ws.prepare("main.zy", "range")["error"]["message"]
    assert ws.prepare("main.zy", "= Point")["result"] is None
    assert ws.prepare("main.zy", "from")["result"] is None  # a keyword


def test_rename_with_unsaved_changes_in_another_open_file(tmp_path):
    w = Workspace(tmp_path, {"shapes.zy": SHAPES, "main.zy": MAIN})
    texts = dict(w.texts)
    texts["main.zy"] = MAIN + "extra = ar(shapes.Shape.Empty)\nmore = shapes.area(shapes.Shape.Empty)\n"
    reply = w.rename("shapes.zy", "area", "surface", texts=texts)
    edits = reply["result"]["changes"][w.uri("main.zy")]
    assert {e["range"]["start"]["line"] for e in edits} >= {MAIN.count("\n") + 1}  # the unsaved line is edited too
    assert len(edits) == 3


# ---- signature help -------------------------------------------------------------------------------------------------------

SIG = '''fn add(a: int, b: int = 2, c: str = "x") -> int:
    "Adds."
    return a + b

struct P:
    x: int
    y: int = 0

    fn scale(self, k: int, by: int = 1) -> int:
        return self.x * k

enum E:
    V(int, str)

'''


def sig_active(w, text, at, shift=0):
    texts = {"doc.zy": SIG + text}
    return w.signature("doc.zy", at, -1, shift=shift, texts=texts)


def test_signature_help_shows_the_signature_and_the_active_parameter(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    for i, (code, at, shift) in enumerate([
        ("add()", "add(", 4),
        ("add(1, 2)", "add(1, 2)", 6),
        ("add(1, 2, \"q\")", "add(1, 2, ", 10),
    ]):
        texts = {"doc.zy": SIG + code + "\n"}
        r = w.signature("doc.zy", at, -1, shift=shift, texts=texts)
        assert r["signatures"][0]["label"] == 'fn add(a: int, b: int = 2, c: str = "x") -> int'
        assert r["activeParameter"] == i
        assert r["signatures"][0]["documentation"]["value"] == "Adds."
    label = r["signatures"][0]["label"]
    a, b = r["signatures"][0]["parameters"][1]["label"]
    assert label[a:b] == "b: int = 2"


def test_signature_help_while_the_call_is_unfinished(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    for code, shift, active in [("x = add(", 8, 0), ("x = add(1, ", 11, 1), ("print(add(1, 2, ", 16, 2)]:
        texts = {"doc.zy": SIG + code + "\n"}
        r = w.signature("doc.zy", code, -1, shift=shift, texts=texts)
        assert r["signatures"][0]["label"].startswith("fn add(") and r["activeParameter"] == active, code


def test_signature_help_after_a_dangling_operator_or_equals(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    for code, shift in [("x = add(1 + ", 12), ("x = add(1, b=", 13)]:
        texts = {"doc.zy": SIG + code + "\n"}
        r = w.signature("doc.zy", code, -1, shift=shift, texts=texts)
        assert r is not None and r["signatures"][0]["label"].startswith("fn add("), code


def test_signature_help_with_named_arguments(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    texts = {"doc.zy": SIG + "add(1, c=\"z\", b=3)\n"}
    assert w.signature("doc.zy", "c=", -1, shift=2, texts=texts)["activeParameter"] == 2
    assert w.signature("doc.zy", "b=3", -1, shift=3, texts=texts)["activeParameter"] == 1
    texts = {"doc.zy": SIG + "add(1, zz=3)\n"}
    r = w.signature("doc.zy", "zz=", -1, shift=3, texts=texts)
    assert r["activeParameter"] == 3  # not a parameter: nothing is highlighted


def test_signature_help_in_the_inner_call_of_a_nested_call(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    texts = {"doc.zy": SIG + "add(add(1, ), [1, 2])\n"}
    r = w.signature("doc.zy", "add(1, ", -1, shift=7, texts=texts)
    assert r["activeParameter"] == 1
    r = w.signature("doc.zy", "[1, 2]", -1, shift=3, texts=texts)  # inside the outer call, second argument
    assert r["activeParameter"] == 1 and r["signatures"][0]["label"].startswith("fn add(")
    assert w.signature("doc.zy", "add(add", -1, shift=4, texts=texts)["activeParameter"] == 0


def test_signature_help_for_constructors_methods_and_variants(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    texts = {"doc.zy": SIG + "q = P(1, \n"}
    r = w.signature("doc.zy", "P(1, ", -1, shift=5, texts=texts)
    assert r["signatures"][0]["label"] == "P(x: int, y: int = 0)" and r["activeParameter"] == 1
    texts = {"doc.zy": SIG + "p = P(1)\np.scale(2, \n"}
    r = w.signature("doc.zy", "p.scale(2, ", -1, shift=11, texts=texts)
    assert r["signatures"][0]["label"] == "fn P.scale(k: int, by: int = 1) -> int" and r["activeParameter"] == 1
    texts = {"doc.zy": SIG + "v = E.V(1, \n"}
    r = w.signature("doc.zy", "E.V(1, ", -1, shift=7, texts=texts)
    assert r["signatures"][0]["label"] == "E.V(int, str)" and r["activeParameter"] == 1


def test_signature_help_across_modules(ws):
    texts = {"shapes.zy": SHAPES, "main.zy": MAIN + "z = ar(\nw = shapes.area(\nv = Point(1.0, \n"}
    r = ws.signature("main.zy", "ar(\n", -1, shift=3, texts=texts)
    assert r["signatures"][0]["label"] == "fn area(s: Shape) -> float" and r["signatures"][0]["documentation"]["value"] == "Area of a shape."
    r = ws.signature("main.zy", "shapes.area(\n", -1, shift=13, texts=texts)
    assert r["signatures"][0]["label"] == "fn area(s: Shape) -> float"
    r = ws.signature("main.zy", "Point(1.0, ", -1, shift=11, texts=texts)
    assert r["signatures"][0]["label"] == "Point(x: float, y: float = 0.0)" and r["activeParameter"] == 1


def test_signature_help_is_empty_outside_a_call_and_for_unknown_callees(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    texts = {"doc.zy": SIG + "x = (1 + 2)\ny = nope(1, \nprint(1, \nif x:\n    pass\n"}
    assert w.signature("doc.zy", "(1 + 2", -1, shift=3, texts=texts) is None
    assert w.signature("doc.zy", "nope(", -1, shift=5, texts=texts) is None
    assert w.signature("doc.zy", "print(", -1, shift=6, texts=texts) is None  # built-ins have no declared signature
    assert w.signature("doc.zy", "x = (1", -1, shift=0, texts=texts) is None


def test_signature_help_ignores_a_function_definition_header(tmp_path):
    w = Workspace(tmp_path, {"doc.zy": SIG}, opened=[])
    texts = {"doc.zy": SIG + "fn other(a: int, \n"}
    assert w.signature("doc.zy", "other(a", -1, shift=9, texts=texts) is None


# ---- use cases without the protocol ------------------------------------------------------------------------------------------

def test_the_use_cases_work_on_a_project_directly(tmp_path):
    (tmp_path / "doc.zy").write_text("fn f(a: int) -> int:\n    return a\n\nprint(f(1))\n")
    project = open_project(tmp_path / "doc.zy")
    h = hover(project, tmp_path / "doc.zy", 4, 7)
    assert h is not None and "fn f(a: int) -> int" in h.markdown
    edits = rename(project, tmp_path / "doc.zy", 4, 7, "g")
    assert sorted((e.place.line, e.place.col) for e in edits) == [(1, 4), (4, 7)]
    with pytest.raises(RenameError):
        rename(open_project(tmp_path / "doc.zy"), tmp_path / "doc.zy", 4, 7, "while")
    sig = signature_help(tmp_path / "doc.zy", "fn f(a: int) -> int:\n    return a\n\nprint(f(", 4, 9, project)
    assert sig is not None and sig.label == "fn f(a: int) -> int" and sig.active == 0


def test_hidden_and_build_directories_are_not_searched(tmp_path):
    w = Workspace(tmp_path, {
        "main.zy": "fn f() -> int:\n    return 1\n\nprint(f())\n",
        ".hidden/other.zy": "x = f()\n",
        "target/gen.zy": "y = f()\n",
    }, opened=["main.zy"])
    out = w.renamed("main.zy", "f()", "g")
    assert set(out) == {"main.zy"}


# ---- more cases ---------------------------------------------------------------------------------------------------------------------

def test_rename_is_refused_when_a_file_that_may_use_the_name_has_a_syntax_error(tmp_path):
    w = Workspace(tmp_path, {
        "main.zy": "fn f() -> int:\n    return 1\n\nprint(f())\n",
        "broken.zy": "from main import f\nx = (f(\n",
    }, opened=["main.zy"])
    err = w.rename("main.zy", "f()", "g")["error"]
    assert "broken.zy" in err["message"] and "syntax error" in err["message"]


def test_rename_a_renamed_reexport_changes_every_file_that_binds_it(tmp_path):
    w = Workspace(tmp_path, {
        "core.zy": "fn helper() -> int:\n    return 1\n",
        "mid.zy": "pub from core import helper as aid\n",
        "main.zy": "from mid import aid\nprint(aid())\n",
    }, opened=["main.zy"])
    out = w.renamed("main.zy", "aid()", "assist")
    assert out["mid.zy"] == "pub from core import helper as assist\n"
    assert out["main.zy"] == "from mid import assist\nprint(assist())\n"
    assert "core.zy" not in out


def test_variants_in_patterns_with_a_module_qualified_enum(tmp_path):
    w = Workspace(tmp_path, {
        "shapes.zy": SHAPES,
        "main.zy": "import shapes\n\nfn r(s: shapes.Shape) -> float:\n    match s:\n        shapes.Shape.Circle(k):\n"
                   "            return k\n        shapes.Shape.Empty:\n            return 0.0\n",
    }, opened=["main.zy"])
    assert w.definition("main.zy", "Circle") == [("shapes.zy", 12, 4)]
    assert w.definition("main.zy", "Shape", 0) == [("shapes.zy", 10, 5)]
    out = w.renamed("main.zy", "Circle", "Disc")
    assert "shapes.Shape.Disc(k):" in out["main.zy"] and "    Disc(float)" in out["shapes.zy"]
    assert "Shape.Disc(r):" in out["shapes.zy"]


def test_rename_a_global_variable_to_a_function_name_is_rejected(ws):
    assert "already used" in ws.rename("main.zy", "t = total", "total")["error"]["message"]


def test_rename_a_function_used_inside_an_f_string(solo):
    w = solo('fn name() -> str:\n    return "z"\n\nprint(f"hi {name()} and {name()}")\n')
    out = w.renamed("doc.zy", "name()", "label")["doc.zy"]
    assert out.count("label") == 3 and "name" not in out


def test_hover_and_definition_for_self_and_enum_methods(solo):
    w = solo("enum Dir:\n    Up\n    Down\n\n    fn flip(self) -> Dir:\n        match self:\n"
             "            Dir.Up:\n                return Dir.Down\n            Dir.Down:\n                return Dir.Up\n"
             "\nprint(Dir.Up.flip())\n")
    assert "(self) self: Dir" in w.hover("doc.zy", "self:")
    assert w.definition("doc.zy", "flip()") == [("doc.zy", 4, 7)]
    out = w.renamed("doc.zy", "flip(self", "toggle")["doc.zy"]
    assert "fn toggle(self)" in out and "Dir.Up.toggle()" in out


def test_unpacked_variables_have_types(solo):
    w = solo('pair = (1, "a")\nn, s = pair\nprint(n, s)\n')
    assert "(variable) n: int" in w.hover("doc.zy", "n, s = pair")
    assert "(variable) s: str" in w.hover("doc.zy", "s)")
    assert w.renamed("doc.zy", "n, s", "num")["doc.zy"] == 'pair = (1, "a")\nnum, s = pair\nprint(num, s)\n'


def test_navigation_works_on_a_file_the_editor_has_not_opened(tmp_path):
    w = Workspace(tmp_path, {"lib.zy": "fn f() -> int:\n    return 1\n", "main.zy": "from lib import f\nprint(f())\n"}, opened=[])
    assert w.definition("main.zy", "f()") == [("lib.zy", 0, 3)]
    assert "fn f() -> int" in w.hover("main.zy", "f()")


def test_a_default_value_can_use_enum_variants_and_is_resolved(solo):
    w = solo("enum C:\n    Red\n    Blue\n\nfn paint(c: C = C.Red) -> int:\n    return 1\n")
    assert w.definition("doc.zy", "Red)") == [("doc.zy", 1, 4)]
    assert w.definition("doc.zy", "C = ") == [("doc.zy", 0, 5)]
