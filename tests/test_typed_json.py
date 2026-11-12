"""Typed JSON: `to_json`, `from_json`, `json_schema` and `json_schema_defs`.

`tests/programs/typed_json_cli.zy` is compiled once. It reads documents from standard input (a type name, a line break
and a JSON text, separated by a line holding `@@@@`), reads each one back into that type, and prints either
`OK <the value written again>` or `ERR <message>`; with `schema NAME` / `defs NAME` it prints schemas. Python (its `json`
module and, when installed, `jsonschema`) is the independent reference.
"""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application.compile import load_program, transpile
from zylo.domain.errors import ZyloError
from zylo.infrastructure.rustc import compile_rust
from zylo.semantics.checker import check, check_all
from zylo.syntax.formatter import format_source
from zylo.syntax.parser import parse

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

CLI = Path(__file__).parent / "programs" / "typed_json_cli.zy"
SEP = "\n@@@@\n"

USER_DEFAULTS = {
    "email": None, "role": "Guest", "tags": [], "address": None, "scores": {}, "ids": {}, "pair": [0, "none"],
    "shapes": [], "seen": [], "extra": None,
}


def normalised(doc: dict) -> dict:
    """What reading a `User` document and writing it again gives: defaults filled in, `none` fields as null."""
    out = {**USER_DEFAULTS, **{k: v for k, v in doc.items() if k != "unknown"}}  # unknown fields are ignored
    if out["address"] is not None:
        out["address"] = {"zip": None, **out["address"]}
    out["seen"] = sorted(set(out["seen"]))
    return out


@pytest.fixture(scope="module")
def exe(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("typed_json") / "cli"
    compile_rust(transpile(CLI), out)
    return out


def run(exe: Path, *docs: tuple[str, object]) -> list[str]:
    text = SEP.join(f"{kind}\n{doc if isinstance(doc, str) else json.dumps(doc)}" for kind, doc in docs)
    r = subprocess.run([str(exe)], input=text, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def one(exe: Path, kind: str, doc) -> str:
    return run(exe, (kind, doc))[0]


def schema_of(exe: Path, kind: str, command: str = "schema") -> dict:
    r = subprocess.run([str(exe), command, kind], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- reading and writing ----------------------------------------------------------------------------------

VALID_USERS = [
    {"name": "Ana", "age": 31},
    {"name": "Bo", "age": 3.0, "email": "b@x.org", "role": "Admin", "tags": ["a", "b"], "unknown": [1, 2]},
    {"name": "Cy", "age": 40, "address": {"street": "Main", "zip": 28001}, "scores": {"m": 1.5, "n": 2}},
    {"name": "Di", "age": 41, "address": {"street": "Side"}, "ids": {"1": True, "-7": False}, "pair": [5, "x"]},
    {"name": "Ed", "age": 1, "email": None, "address": None, "seen": [3, 1, 3, 2], "extra": {"k": [1, "two", None]}},
    {"name": "Flo", "age": 2, "shapes": [{"type": "Circle", "value": 1.5}, {"type": "Rect", "values": [1, 2.5]},
                                         {"type": "Label", "value": None}, {"type": "Label", "value": "L"},
                                         {"type": "Empty"}]},
]


@needs_rustc
@pytest.mark.parametrize("doc", VALID_USERS)
def test_valid_users_read_back_as_the_normalised_document(exe, doc):
    out = one(exe, "User", doc)
    assert out.startswith("OK ")
    assert json.loads(out[3:]) == normalised(doc)


@needs_rustc
def test_what_is_written_reads_back_the_same(exe):
    """`from_json(T, to_json(x)) == x`: writing the normalised document again is a fixed point."""
    for doc in VALID_USERS:
        first = one(exe, "User", doc)
        second = one(exe, "User", first[3:])
        assert second == first


@needs_rustc
@pytest.mark.parametrize("doc,message", [
    ({"name": "Bo", "age": "x"}, "$.age: expected int, got string"),
    ({"name": "Bo"}, "$.age: missing required field"),
    ({"age": 1}, "$.name: missing required field"),
    ({"name": 1, "age": 1}, "$.name: expected str, got number"),
    ({"name": "a", "age": 3.5}, "$.age: expected int, got 3.5 (not a whole number)"),
    ({"name": "a", "age": 1e30}, "$.age: expected int, got 1000000000000000000000000000000 (out of range)"),
    ({"name": "a", "age": None}, "$.age: expected int, got null"),
    ({"name": "a", "age": 1, "email": 5}, "$.email: expected str, got number"),
    ({"name": "a", "age": 1, "role": "Root"}, '$.role: unknown variant "Root", expected one of "Admin", "Guest"'),
    ({"name": "a", "age": 1, "role": 1}, "$.role: expected str, got number"),
    ({"name": "a", "age": 1, "tags": ["x", 2]}, "$.tags[1]: expected str, got number"),
    ({"name": "a", "age": 1, "tags": {}}, "$.tags: expected array, got object"),
    ({"name": "a", "age": 1, "address": {"zip": 1}}, "$.address.street: missing required field"),
    ({"name": "a", "age": 1, "address": []}, "$.address: expected object, got array"),
    ({"name": "a", "age": 1, "scores": {"x": "y"}}, '$.scores["x"]: expected float, got string'),
    ({"name": "a", "age": 1, "ids": {"one": True}}, '$.ids["one"]: expected an integer key'),
    ({"name": "a", "age": 1, "ids": {"1": 1}}, '$.ids["1"]: expected bool, got number'),
    ({"name": "a", "age": 1, "pair": [1]}, "$.pair: expected an array of 2 items, got 1"),
    ({"name": "a", "age": 1, "pair": [1, 2]}, "$.pair[1]: expected str, got number"),
    ({"name": "a", "age": 1, "seen": [1, "x"]}, "$.seen[1]: expected int, got string"),
    ({"name": "a", "age": 1, "shapes": [{"type": "Circle"}]}, "$.shapes[0].value: missing required field"),
    ({"name": "a", "age": 1, "shapes": [{"type": "Rect", "values": [1]}]},
     "$.shapes[0].values: expected an array of 2 items, got 1"),
    ({"name": "a", "age": 1, "shapes": [{"type": "Hex"}]},
     '$.shapes[0].type: unknown variant "Hex", expected one of "Circle", "Rect", "Label", "Empty"'),
    ({"name": "a", "age": 1, "shapes": [{"value": 1}]}, "$.shapes[0].type: missing required field"),
    ({"name": "a", "age": 1, "shapes": ["Circle"]}, "$.shapes[0]: expected object, got string"),
    ("[1]", "$: expected object, got array"),
    ("7", "$: expected object, got number"),
    ("null", "$: expected object, got null"),
])
def test_errors_name_the_path(exe, doc, message):
    assert one(exe, "User", doc) == "ERR " + message


@needs_rustc
def test_the_errors_of_other_types(exe):
    docs = [
        ("list[int]", "[1, 2, 3]"), ("list[int]", "[1, 2.5]"), ("list[int]", '{"a": 1}'), ("list[int]", "[[1]]"),
        ("list[int]", "[9223372036854775807]"), ("list[int]", "[-9223372036854775808, 3.0, -0.0]"),
        ("dict[str, list[float]]", '{"a": [1, 2.5], "b": []}'), ("dict[str, list[float]]", '{"a": [1, "x"]}'),
        ("tuple[int, bool?]", "[1, null]"), ("tuple[int, bool?]", "[1, true]"), ("tuple[int, bool?]", "[1]"),
        ("tuple[int, bool?]", '[1, "no"]'),
        ("Tree", '{"value": 1, "children": [{"value": 2}, {"value": 3, "children": [{"value": "x"}]}]}'),
        ("Tree", '{"value": 1, "children": [{"value": 2}, {"value": 3, "children": [{"value": 4}]}]}'),
        ("Shape", '{"type": "Circle", "value": 2}'), ("Shape", '{"type": "Label"}'), ("Shape", "1"),
        ("Shape", '{"type": 1}'),
    ]
    assert run(exe, *docs) == [
        "OK [1,2,3]",
        "ERR $[1]: expected int, got 2.5 (not a whole number)",
        "ERR $: expected array, got object",
        "ERR $[0]: expected int, got array",
        # 2^63 - 1 is not exactly representable as a float: the value read is 2^63 (written 9223372036854776000, the shortest text that reads back), out of range
        "ERR $[0]: expected int, got 9223372036854776000 (out of range)",
        "OK [-9223372036854776000,3,0]",
        'OK {"a":[1,2.5],"b":[]}',
        'ERR $["a"][1]: expected float, got string',
        "OK [1,null]", "OK [1,true]",
        "ERR $: expected an array of 2 items, got 1",
        "ERR $[1]: expected bool, got string",
        'ERR $.children[1].children[0].value: expected int, got string',
        'OK {"children":[{"children":[],"value":2},{"children":[{"children":[],"value":4}],"value":3}],"value":1}',
        'OK {"type":"Circle","value":2}',
        "ERR $.value: missing required field",
        "ERR $: expected object, got number",
        "ERR $.type: expected str, got number",
    ]


@needs_rustc
def test_random_users_round_trip(exe):
    rng = random.Random(7)

    def user() -> dict:
        doc: dict = {"name": rng.choice(["a", "b\"q", "é", "😀", ""]), "age": rng.randint(-10**9, 10**9)}
        if rng.random() < 0.5:
            doc["email"] = rng.choice([None, "x@y"])
        if rng.random() < 0.5:
            doc["tags"] = [rng.choice(["t", "u", "v w"]) for _ in range(rng.randint(0, 4))]
        if rng.random() < 0.5:
            doc["scores"] = {rng.choice("abc"): rng.choice([0.5, -2.25, 3]) for _ in range(rng.randint(0, 3))}
        if rng.random() < 0.5:
            doc["role"] = rng.choice(["Admin", "Guest"])
        if rng.random() < 0.5:
            doc["address"] = {"street": "s", **({"zip": rng.randint(0, 99999)} if rng.random() < 0.5 else {})}
        return doc

    docs = [user() for _ in range(60)]
    out = run(exe, *(("User", d) for d in docs))
    for doc, line in zip(docs, out, strict=True):
        assert json.loads(line[3:]) == normalised(doc)


# ---- schemas ------------------------------------------------------------------------------------------------

try:
    import jsonschema
except ImportError:  # the schema tests below need it; without it they are skipped
    jsonschema = None

needs_jsonschema = pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")


def document(exe: Path, kind: str) -> dict:
    """The schema of a type with the components its `$ref`s point to, as one document."""
    return {**schema_of(exe, kind), "components": {"schemas": schema_of(exe, kind, "defs")}}


@needs_jsonschema
@needs_rustc
@pytest.mark.parametrize("kind", ["User", "Tree", "Shape", "list[int]", "dict[str, list[float]]", "tuple[int, bool?]"])
def test_schemas_are_valid_json_schema(exe, kind):
    jsonschema.Draft202012Validator.check_schema(document(exe, kind))


@needs_jsonschema
@needs_rustc
@pytest.mark.parametrize("doc", VALID_USERS)
def test_a_document_that_reads_back_satisfies_the_schema(exe, doc):
    validator = jsonschema.Draft202012Validator(document(exe, "User"))
    validator.validate({**doc, "seen": sorted(set(doc["seen"]))} if "seen" in doc else doc)  # a set has no repeats
    validator.validate(json.loads(one(exe, "User", doc)[3:]))  # what to_json writes satisfies it too


@needs_jsonschema
@needs_rustc
def test_what_the_schema_rejects_is_an_error_too(exe):
    validator = jsonschema.Draft202012Validator(document(exe, "User"))
    rejected = 0
    for doc in [{"name": "Bo", "age": "x"}, {"name": "Bo"}, {"age": 1}, {"name": 1, "age": 1}, {"name": "a", "age": 3.5},
                {"name": "a", "age": None}, {"name": "a", "age": 1, "role": "Root"}, {"name": "a", "age": 1, "tags": [1]},
                {"name": "a", "age": 1, "address": {"zip": 1}}, {"name": "a", "age": 1, "pair": [1]},
                {"name": "a", "age": 1, "scores": {"x": "y"}}, {"name": "a", "age": 1, "ids": {"one": True}},
                {"name": "a", "age": 1, "shapes": [{"type": "Hex"}]}, {"name": "a", "age": 1, "shapes": [{"type": "Circle"}]},
                {"name": "a", "age": 1, "shapes": [{"type": "Rect", "values": [1]}]}, [1], "x", None]:
        assert not validator.is_valid(doc), doc
        assert one(exe, "User", doc).startswith("ERR "), doc
        rejected += 1
    assert rejected == 18


@needs_jsonschema
@needs_rustc
def test_shape_schema_accepts_exactly_the_encoded_values(exe):
    validator = jsonschema.Draft202012Validator(document(exe, "Shape"))
    good = [{"type": "Circle", "value": 1}, {"type": "Rect", "values": [1, 2]}, {"type": "Label", "value": None},
            {"type": "Label", "value": "x"}, {"type": "Empty"}]
    bad = [{"type": "Circle"}, {"type": "Circle", "value": "x"}, {"type": "Rect", "values": [1]}, {"type": "Other"}, "Empty"]
    assert all(validator.is_valid(d) and one(exe, "Shape", d).startswith("OK ") for d in good)
    assert not any(validator.is_valid(d) or one(exe, "Shape", d).startswith("OK ") for d in bad)


@needs_rustc
def test_schema_contents(exe):
    user = schema_of(exe, "User")
    assert user["type"] == "object" and user["description"] == "A registered user."
    assert user["required"] == ["name", "age"]  # no default and not an Option
    props = user["properties"]
    assert props["name"] == {"type": "string", "description": "Display name."}
    assert props["email"] == {"type": ["string", "null"]}
    assert props["address"] == {"anyOf": [{"$ref": "#/components/schemas/Address"}, {"type": "null"}], "default": None}
    assert props["role"] == {"$ref": "#/components/schemas/Role", "default": "Guest"}
    assert props["scores"] == {"type": "object", "additionalProperties": {"type": "number"}, "default": {}}
    assert props["ids"]["propertyNames"] == {"pattern": "^-?[0-9]+$"}
    assert props["seen"] == {"type": "array", "items": {"type": "integer"}, "uniqueItems": True, "default": []}
    assert props["pair"]["prefixItems"] == [{"type": "integer"}, {"type": "string"}]
    assert props["pair"]["default"] == [0, "none"]
    assert props["extra"] == {"default": None}  # any JSON value
    assert schema_of(exe, "list[int]") == {"type": "array", "items": {"type": "integer"}}
    assert schema_of(exe, "tuple[int, bool?]")["prefixItems"][1] == {"type": ["boolean", "null"]}


@needs_rustc
def test_definitions_collect_every_named_type_once(exe):
    defs = schema_of(exe, "User", "defs")
    assert sorted(defs) == ["Address", "Role", "Shape", "User"]
    assert defs["Role"] == {"type": "string", "enum": ["Admin", "Guest"], "description": "What a user may do."}
    assert defs["User"] == schema_of(exe, "User")
    assert sorted(schema_of(exe, "Tree", "defs")) == ["Tree"]
    assert schema_of(exe, "Tree", "defs")["Tree"]["properties"]["children"]["items"] == {"$ref": "#/components/schemas/Tree"}
    assert sorted(schema_of(exe, "Shape", "defs")) == ["Shape"]


# ---- compile errors -------------------------------------------------------------------------------------------

def project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path / "main.zy"


def fails(tmp_path: Path, src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        check(load_program(project(tmp_path, {"main.zy": src})))
    if line is not None:
        assert e.value.line == line
    return e.value


def test_functions_are_not_data(tmp_path):
    src = "struct Handler:\n    name: str\n    cb: fn(int) -> int\n\nh = Handler(\"a\", fn(x: int) => x)\nj = to_json(h)\n"
    fails(tmp_path, src, r"to_json cannot handle Handler: Handler\.cb has the function type fn\(int\) -> int", 6)


def test_a_result_is_not_data(tmp_path):
    fails(tmp_path, "r: Result[int, str] = ok(1)\nj = to_json(r)\n", r"to_json cannot handle Result\[int, str\]: .* is a Result", 2)
    fails(tmp_path, "struct A:\n    r: Result[int, str]\nj = json_schema(A)\n", r"json_schema cannot handle A: A\.r is a Result", 3)
    fails(tmp_path, "j = json_schema(list[fn(int) -> int])\n", "functions are not data", 1)


def test_dict_keys_must_be_str_or_int(tmp_path):
    fails(tmp_path, "d: dict[bool, int] = {}\nj = to_json(d)\n", "JSON object keys must be str or int", 2)


def test_argument_errors(tmp_path):
    fails(tmp_path, "j = to_json()\n", r"to_json takes 1 argument", 1)
    fails(tmp_path, "j = to_json(1, 2)\n", r"to_json takes 1 argument", 1)
    fails(tmp_path, "r = from_json(int)\n", r"from_json takes 2 argument", 1)
    fails(tmp_path, "r = from_json(int, 1)\n", r"from_json expects a Json value, not int", 1)
    fails(tmp_path, "r = from_json(Nope, 1)\n", r"unknown type 'Nope'", 1)
    fails(tmp_path, "x = json_schema(int, 1)\n", r"json_schema takes 1 argument", 1)


def test_the_result_types(tmp_path):
    src = ("import std.json as json\nstruct P:\n    x: int\n"
           "j: json.Json = to_json(P(1))\nr: Result[P, str] = from_json(P, j)\nr2: Result[list[int]?, str] = from_json(list[int]?, j)\n"
           "s: json.Json = json_schema(P)\nd: json.Json = json_schema_defs(P)\n")
    assert check_all(load_program(project(tmp_path, {"main.zy": src}))) == []


def test_the_names_are_reserved(tmp_path):
    fails(tmp_path, "fn to_json(x: int) -> int:\n    return x\n", "built-in function", 1)


def test_a_string_cannot_be_compiled_without_std_json():
    with pytest.raises(ZyloError, match="needs the module std.json"):
        check(parse("x = to_json(1)\n"))


def test_errors_are_reported_once_and_the_checker_is_repeatable(tmp_path):
    p = project(tmp_path, {"main.zy": "struct P:\n    x: int\n    cb: fn() -> int\nj = to_json(P(1, fn() => 1))\nk = to_json(2)\n"})
    errors = check_all(load_program(p))
    assert [e.line for e in errors] == [4]


# ---- modules ---------------------------------------------------------------------------------------------------------

SHAPES = '''\
enum Kind:
    "Where it goes."
    Round
    Square

struct Point:
    x: float
    y: float = 0.0
    kind: Kind = Kind.Round

struct Path:
    "A line through points."
    points: list[Point]
    name: str?
'''

OTHER = '''\
struct Point:
    id: int
'''

MAIN = '''\
import std.json as json
import shapes
import other
from shapes import Path, Point

p = Path([Point(1.0, 2.0, shapes.Kind.Square)], none)
j = to_json(p)
print(json.stringify(j))
back = from_json(shapes.Path, j)
print(back == ok(p))
print(from_json(Path, json.parse('{"points": [{"x": "a"}], "name": null}').unwrap()))
print(from_json(shapes.Kind, json.parse('"Hex"').unwrap()))
print(json.stringify(to_json(other.Point(3))), json.stringify(to_json(Point(0.5))))
print(json.stringify(json_schema_defs(Path)))
print(json.stringify(json_schema_defs(list[other.Point])))
print(json.stringify(json_schema(shapes.Kind)))
'''


@needs_rustc
def test_types_from_other_modules(tmp_path):
    main = project(tmp_path, {"main.zy": MAIN, "shapes.zy": SHAPES, "other.zy": OTHER})
    exe = tmp_path / "prog"
    compile_rust(transpile(main), exe)
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.splitlines()
    assert out[0] == '{"name":null,"points":[{"kind":"Square","x":1,"y":2}]}'
    assert out[1] == "true"
    assert out[2] == 'Err("$.points[0].x: expected float, got string")'
    assert out[3] == 'Err("$: unknown variant \\"Hex\\", expected one of \\"Round\\", \\"Square\\"")'
    assert out[4] == '{"id":3} {"kind":"Round","x":0.5,"y":0}'
    defs = json.loads(out[5])
    assert sorted(defs) == ["Kind", "Path", "shapes.Point"]  # two modules define a `Point`: the module is kept
    assert defs["Path"]["properties"]["points"]["items"] == {"$ref": "#/components/schemas/shapes.Point"}
    assert defs["Path"]["description"] == "A line through points."
    assert sorted(json.loads(out[6])) == ["other.Point"]
    assert json.loads(out[7]) == {"type": "string", "enum": ["Round", "Square"], "description": "Where it goes."}


# ---- doc strings ----------------------------------------------------------------------------------------------------------

DOCS = '''\
struct A:
    "Describes A."
    x: int
    "The x."
    y: int

    fn f(self) -> int:
        return self.x

enum E:
    "Describes E."
    P
    "The P."
    Q(int)
'''


def test_doc_strings_are_parsed_and_formatted():
    prog = parse(DOCS)
    a, e = prog.body
    assert (a.doc, a.fields[0].doc, a.fields[1].doc) == ("Describes A.", "The x.", None)
    assert (e.doc, e.variants[0].doc, e.variants[1].doc) == ("Describes E.", "The P.", None)
    assert format_source(DOCS) == DOCS
    assert format_source(format_source(DOCS)) == DOCS
    check(prog)  # a description changes nothing else


def test_comments_around_doc_strings_are_kept():
    src = "struct A:\n    # about\n    \"Doc.\"  # trailing\n    x: int  # the x\n    \"Its doc.\"\n"
    assert format_source(src) == src


@pytest.mark.parametrize("src", [
    "struct A:\n    x: int\n    fn f(self):\n        return\n    \"late\"\n",
    "struct A:\n    \"one\"\n    \"two\"\n    x: int\n",
    "enum E:\n    A\n    \"one\"\n    \"two\"\n",
    "struct A:\n    x: int\n    \"one\"\n    \"two\"\n",
])
def test_misplaced_doc_strings_are_rejected(src):
    with pytest.raises(ZyloError, match="a description goes first"):
        parse(src)


def test_a_formatted_type_argument_is_kept():
    src = "r = from_json(list[int], j)\ns = json_schema(dict[str, tuple[int, str?]]?)\n"
    assert format_source(src) == src
