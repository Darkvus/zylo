"""Conformance tests for `std.json`: RFC 8259 valid and invalid documents, round trips and the accessors.

One program (`tests/programs/json_cli.zy`) is compiled once; it parses every document it reads (separated by a line
holding `@@@@`) and prints `OK <compact>` or `ERR <message>`. Python's `json` module is the independent reference.
"""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from zylo.application.compile import transpile
from zylo.infrastructure.rustc import compile_rust

pytestmark = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

CLI = Path(__file__).parent / "programs" / "json_cli.zy"
SEP = "\n@@@@\n"

VALID = [
    "null", "true", "false", "0", "-0", "1", "-1", "123456789", "0.5", "-0.5", "1.5e3", "1E3", "1e+3", "1e-3", "1.25E-2",
    "0e0", "0.0", "-0.0e-0", "1e308", "5e-324", "1.7976931348623157e308",
    '""', '"abc"', '"a b"', r'"\""', r'"\\"', r'"\/"', r'"\b\f\n\r\t"', r'"A"', r'"é"', r'"é"',
    r'"€"', r'"😀"', r'"😀"', r'"\u0000"', r'"\u001f"', r'"\u007f"', '"é"', '"€"', '"😀"',
    '" "', '" "', '"﻿"', r'"a\u0000b"',
    "[]", "[ ]", "[1]", "[1,2,3]", "[ 1 , 2 ]", "[[]]", "[[],[]]", "[[[[[[1]]]]]]", '[null,true,false,0,"x",[],{}]',
    "{}", "{ }", '{"a":1}', '{"a":1,"b":2}', '{"a":{"b":{"c":[1,{"d":null}]}}}', '{"":1}', '{"a":1,"a":2}',
    '{"é":"ü","😀":1}', r'{"a":1}', ' \t\r\n null \t\r\n', "\n\n[\n1\n,\n2\n]\n",
    '{"a":[1,2,{"b":[]}],"c":"d"}', "[" * 100 + "]" * 100, "[" * 512 + "]" * 512,
    "123456789012345678901234567890", "0.1234567890123456789", "-1.5e-10",
]

INVALID = [
    "", " ", "\n", "nul", "nulL", "Null", "NULL", "tru", "True", "false1", "truefalse", "nulll",
    "NaN", "Infinity", "-Infinity", "inf", "+1", "-", "--1", "01", "-01", "00", "1.", ".5", "-.5", "1.e3", "1e", "1e+",
    "1E-", "0x10", "1_000", "1,2", "1 2", "- 1", "1e999", "-1e999", "٣", "１",
    '"', '"abc', '"abc\n"', '"a\tb"', "\"\x01\"", "\"\x1f\"", r'"\x"', r'"\a"', r'"\u"', r'"\u12"', r'"\u12G4"',
    r'"\uZZZZ"', r'"\ud83d"', r'"\ud83dx"', r'"\ud83dA"', r'"\ude00"', r'"\ud83d\ud83d"', '"\\', "'a'", '"a" "b"',
    "[", "]", "[1", "[1,", "[1,]", "[,1]", "[1 2]", "[1;2]", "[1}", "[}", "[[]", "[]]", "[null,]", "[,]", "[1,,2]",
    "{", "}", '{"a"', '{"a":', '{"a":1', '{"a":1,', '{"a":1,}', '{,}', '{"a" 1}', '{"a"::1}', '{a:1}', "{'a':1}",
    "{1:2}", '{"a":1 "b":2}', '{"a":1}}', '{"a":}', '{null:1}', '{"a":1;}', "[]x", "{}x", "null null", "[] []",
    "/* c */ null", "null // c", "[1,2,3] ,", "﻿null", "\x00", "null\x00", "\x0b", "\x0c1",
    "[" * 513 + "]" * 513, "[" * 5000, '{"a":' * 600 + "1" + "}" * 600,
]


def run_documents(exe: Path, docs: list[str]) -> list[str]:
    out = subprocess.run([str(exe)], input=SEP.join(docs), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split("\n@@@@\n")
    return [x.removesuffix("\n") for x in lines]


@pytest.fixture(scope="module")
def exe(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("json") / "json_cli"
    compile_rust(transpile(CLI), path)
    return path


def reference(text: str):
    """Python's reading of `text`, with every integer read as a float (Zylo numbers are floats)."""
    def refuse(name):
        raise ValueError(name)
    return json.loads(text, parse_int=float, parse_constant=refuse)


def test_every_valid_document_parses_to_the_same_value(exe):
    for doc, line in zip(VALID, run_documents(exe, VALID)):
        assert line.startswith("OK "), (doc, line)
        assert reference(line[3:]) == reference(doc), doc


def test_every_invalid_document_is_rejected_with_a_position(exe):
    for doc, line in zip(INVALID, run_documents(exe, INVALID)):
        assert line.startswith("ERR "), (doc, line)
        assert " at line " in line and ", column " in line, line


def test_the_references_agree_on_the_lists():
    """The lists themselves are right: Python accepts every valid one and rejects the invalid ones (bar its leniency)."""
    for doc in VALID:
        reference(doc)
    lenient = {r'"\ud83d"', r'"\ud83dx"', r'"\ud83dA"', r'"\ude00"', r'"\ud83d\ud83d"', "\x0c1", "\x0b", "﻿null"}
    for doc in INVALID:
        if doc in lenient or doc.startswith(("1e999", "-1e999")) or doc in ("[" * 513 + "]" * 513,
                                                                            '{"a":' * 600 + "1" + "}" * 600):
            continue
        with pytest.raises((ValueError, RecursionError)):
            reference(doc)


@pytest.mark.parametrize("doc,message", [
    ("[1,]", "expected a JSON value but found ']' at line 1, column 4"),
    ('{"a" 1}', "expected ':' but found '1' at line 1, column 6"),
    ('{"a":1', "expected ',' or '}' but found end of input at line 1, column 7"),
    ("[1,\n 2,\n x]", "expected a JSON value but found 'x' at line 3, column 2"),
    ("nul", "invalid literal, expected 'null' at line 1, column 4"),
    ("1 2", "unexpected '2' after the JSON value at line 1, column 3"),
    ("01", "unexpected '1' after the JSON value at line 1, column 2"),
    ("1.", "expected a digit after '.' but found end of input at line 1, column 3"),
    ('"é\n"', "unescaped control character U+000A in string at line 1, column 3"),
    (r'"\ud800"', "unpaired surrogate in \\u escape at line 1, column 8"),
    ("[" * 513, "nesting is deeper than 512 levels at line 1, column 513"),
])
def test_error_messages(exe, doc, message):
    assert run_documents(exe, [doc]) == ["ERR " + message]


def random_value(rng: random.Random, depth: int = 0):
    kind = rng.random()
    if depth > 4 or kind < 0.45:
        return rng.choice([
            None, True, False, rng.randint(-10**9, 10**9), rng.random() * 10 ** rng.randint(-20, 20),
            -rng.random(), "".join(rng.choice("aé€😀\"\\\n\t\x00\x1f /") for _ in range(rng.randint(0, 8)))])
    if kind < 0.75:
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    return {"".join(rng.choice("kéy\"\\1") for _ in range(rng.randint(0, 4))): random_value(rng, depth + 1)
            for _ in range(rng.randint(0, 5))}


def test_round_trip_of_random_documents(exe):
    rng = random.Random(7)
    values = [random_value(rng) for _ in range(400)]
    docs = [json.dumps(v, ensure_ascii=rng.random() < 0.5, indent=rng.choice([None, 2])) for v in values]
    for value, doc, line in zip(values, docs, run_documents(exe, docs)):
        assert line.startswith("OK "), (doc, line)
        assert reference(line[3:]) == reference(doc)
        assert reference(line[3:]) == reference(json.dumps(value))


def test_stringify_is_compact_and_pretty_is_indented(exe):
    doc = '{"b":[1,2,{"c":null}],"a":"x\\ny\\u0001\\"é","e":[],"f":{},"g":[[]],"h":-5,"i":true}'
    value = json.loads(doc)
    compact, pretty = run_documents(exe, ["@compact " + doc, "@pretty " + doc])
    assert compact == json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    assert pretty == json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_accessors(exe):
    [out] = run_documents(exe, ["@accessors"])
    assert out.splitlines() == [
        'get: some "Ada"', "get missing: none", "get on array: none", "index: some 20", "index -1: some 30",
        "index 3: none", "index -4: none", "index on object: none", "as_str: some Ada", "as_str of num: none",
        "as_int: some 42", "as_int 4.5: none", "as_int of str: none", "as_float: some 4.5", "as_bool: some true",
        "as_bool of null: none", "is_null: true false", "as_list: 3 true", "as_dict: 6 true",
    ]
