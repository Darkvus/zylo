"""Generate the VS Code (TextMate) grammar from the language's own keyword and built-in lists.

Usage: python scripts/generate_vscode_grammar.py

The grammar is generated so that it cannot drift from the lexer and the built-in catalog; a test checks that the
committed file equals what this script produces.
"""

import json
from pathlib import Path

from zylo.domain.builtins import BUILTINS
from zylo.syntax.lexer import KEYWORDS

OUTPUT = Path(__file__).parent.parent / "editors" / "vscode" / "syntaxes" / "zylo.tmLanguage.json"

CONTROL = {"if", "elif", "else", "while", "for", "in", "return", "break", "continue", "pass", "import", "from", "as", "match"}
WORD_OPERATORS = {"and", "or", "not", "is"}
STORAGE = {"fn", "struct", "enum", "extern", "pub"}
CONSTANTS = {"true", "false", "none"}
TYPES = ["int", "float", "bool", "str", "list", "dict", "set", "tuple", "Result", "Cell"]
ESCAPE = r"\\[nrt0\\\"']"


def words(names) -> str:
    """A regular expression matching any of the names as whole words."""
    return r"\b(" + "|".join(sorted(names)) + r")\b"


def string_rule(quote: str, scope: str) -> dict:
    """A plain string delimited by `quote`."""
    return {"name": scope, "begin": quote, "end": quote,
            "beginCaptures": {"0": {"name": "punctuation.definition.string.begin.zylo"}},
            "endCaptures": {"0": {"name": "punctuation.definition.string.end.zylo"}},
            "patterns": [{"include": "#escape"}]}


def fstring_rule(quote: str) -> dict:
    """An f-string delimited by `quote`, with `{expression:spec}` fields."""
    return {"name": "string.quoted.fstring.zylo", "begin": r"\b(f)(" + quote + ")", "end": quote,
            "beginCaptures": {"1": {"name": "storage.type.string.zylo"},
                              "2": {"name": "punctuation.definition.string.begin.zylo"}},
            "endCaptures": {"0": {"name": "punctuation.definition.string.end.zylo"}},
            "patterns": [{"include": "#escape"},
                         {"match": r"\{\{|\}\}", "name": "constant.character.escape.zylo"},
                         {"name": "meta.interpolation.zylo", "begin": r"\{", "end": r"\}",
                          "beginCaptures": {"0": {"name": "punctuation.section.interpolation.begin.zylo"}},
                          "endCaptures": {"0": {"name": "punctuation.section.interpolation.end.zylo"}},
                          "patterns": [{"match": r":[^}]*", "name": "constant.other.format-spec.zylo"},
                                       {"include": "$self"}]}]}


def grammar() -> dict:
    """Build the whole grammar."""
    assert KEYWORDS == CONTROL | WORD_OPERATORS | STORAGE | CONSTANTS, "a keyword has no highlighting category"
    return {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json",
        "name": "Zylo",
        "scopeName": "source.zylo",
        "fileTypes": ["zy"],
        "patterns": [{"include": f"#{n}"} for n in (
            "comments", "fstrings", "strings", "definitions", "keywords", "constants", "numbers", "types", "builtins",
            "self", "struct-names", "calls", "operators")],
        "repository": {
            "comments": {"name": "comment.line.number-sign.zylo", "match": "#.*$"},
            "escape": {"name": "constant.character.escape.zylo", "match": ESCAPE},
            "fstrings": {"patterns": [fstring_rule('"'), fstring_rule("'")]},
            "strings": {"patterns": [string_rule('"', "string.quoted.double.zylo"),
                                     string_rule("'", "string.quoted.single.zylo")]},
            "definitions": {"patterns": [
                {"match": r"\b(extern)\s+(fn)\s+([A-Za-z_]\w*)",
                 "captures": {"1": {"name": "storage.modifier.zylo"}, "2": {"name": "storage.type.function.zylo"},
                              "3": {"name": "entity.name.function.zylo"}}},
                {"match": r"\b(fn)\s+([A-Za-z_]\w*)",
                 "captures": {"1": {"name": "storage.type.function.zylo"}, "2": {"name": "entity.name.function.zylo"}}},
                {"match": r"\b(struct)\s+([A-Za-z_]\w*)",
                 "captures": {"1": {"name": "storage.type.struct.zylo"}, "2": {"name": "entity.name.type.struct.zylo"}}}]},
            "keywords": {"patterns": [
                {"name": "keyword.control.zylo", "match": words(CONTROL)},
                {"name": "keyword.operator.word.zylo", "match": words(WORD_OPERATORS)},
                {"name": "storage.type.zylo", "match": words(STORAGE)}]},
            "constants": {"name": "constant.language.zylo", "match": words(CONSTANTS)},
            "numbers": {"patterns": [{"name": "constant.numeric.float.zylo", "match": r"\b\d+\.\d+\b"},
                                     {"name": "constant.numeric.integer.zylo", "match": r"\b\d+\b"}]},
            "types": {"name": "support.type.zylo", "match": words(TYPES)},
            "builtins": {"name": "support.function.builtin.zylo", "match": words(BUILTINS) + r"(?=\s*\()"},
            "self": {"name": "variable.language.self.zylo", "match": r"\bself\b"},
            "struct-names": {"name": "entity.name.type.zylo", "match": r"\b[A-Z][A-Za-z0-9_]*\b"},
            "calls": {"name": "entity.name.function.call.zylo", "match": r"\b[A-Za-z_]\w*(?=\s*\()"},
            "operators": {"name": "keyword.operator.zylo", "match": r"->|==|!=|<=|>=|\+=|-=|\*=|/=|%=|[+\-*/%<>=?]"},
        },
    }


def render() -> str:
    """The grammar as JSON text."""
    return json.dumps(grammar(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    """Write the grammar file."""
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
