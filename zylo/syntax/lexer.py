"""Lexer: source text to tokens.

Handles significant indentation (INDENT/DEDENT tokens), comments, numbers, identifiers and keywords, strings with
escapes, and f-strings (kept raw for the parser to split).
"""

from dataclasses import dataclass

from zylo.domain.errors import ZyloError

KEYWORDS = {
    "fn", "if", "elif", "else", "while", "for", "in", "return", "break", "continue",
    "and", "or", "not", "true", "false", "struct", "enum", "match", "is", "none", "import", "from", "as", "extern", "pub",
}
TWO_CHAR = {"==", "!=", "<=", ">=", "->", "=>", "+=", "-=", "*=", "/=", "%="}
ONE_CHAR = set("+-*/%<>=(),:[].?{}")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}


@dataclass(frozen=True)
class Token:
    """A lexical token.

    Attributes:
        kind: INT, FLOAT, STR, FSTR, IDENT, KW, OP, NEWLINE, INDENT, DEDENT or EOF.
        value: The token's text (empty for layout tokens).
        line: 1-based line.
        col: 1-based column.
    """
    kind: str  # INT FLOAT STR IDENT KW OP NEWLINE INDENT DEDENT EOF
    value: str
    line: int
    col: int


def tokenize(src: str) -> list[Token]:
    """Split a Zylo source text into tokens.

    Args:
        src: The whole source text.

    Returns:
        The tokens, ending with an EOF token. Logical lines end with NEWLINE; changes of indentation produce INDENT and
        DEDENT.

    Raises:
        ZyloError: On a tab, inconsistent indentation, an unterminated string, an unknown escape or an unexpected
            character.
    """
    tokens: list[Token] = []
    indents = [0]
    end = (1, 1)  # position after the last line with code
    depth = 0  # open `(`, `[`, `{`: lines inside them continue the logical line
    opener = 0  # indentation of the line that opened them
    for lineno, raw in enumerate(src.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        width = len(line) - len(line.lstrip(" "))
        if depth > 0 and width <= opener and line[width] not in ")]}":
            # a line that is not indented past the opener and does not close it: the bracket was never closed, so end
            # that line here and let the parser report what it expected
            tokens.append(Token("NEWLINE", "", *end))
            depth = 0
        if depth > 0:  # continuation line: no indentation or NEWLINE tokens
            before = len(tokens)
            _lex_line(line, width, lineno, tokens)
            depth += _bracket_delta(tokens[before:])
            if depth <= 0:
                depth = 0
                tokens.append(Token("NEWLINE", "", lineno, len(line) + 1))
            end = (lineno, len(line) + 1)
            continue
        if line[width:width + 1] == "\t":
            raise ZyloError("use spaces, not tabs", lineno, width + 1)
        if width > indents[-1]:
            indents.append(width)
            tokens.append(Token("INDENT", "", lineno, 1))
        while width < indents[-1]:
            indents.pop()
            tokens.append(Token("DEDENT", "", lineno, 1))
        if width != indents[-1]:
            raise ZyloError("inconsistent indentation", lineno, width + 1)
        before = len(tokens)
        _lex_line(line, width, lineno, tokens)
        depth = max(0, _bracket_delta(tokens[before:]))
        opener = width
        if depth == 0:
            tokens.append(Token("NEWLINE", "", lineno, len(line) + 1))
        end = (lineno, len(line) + 1)
    if depth > 0:  # an unclosed bracket: end the line so the parser reports what it expected
        tokens.append(Token("NEWLINE", "", *end))
    while len(indents) > 1:
        indents.pop()
        tokens.append(Token("DEDENT", "", *end))
    tokens.append(Token("EOF", "", *end))
    return tokens


def _bracket_delta(toks: list[Token]) -> int:
    """Net number of brackets opened by these tokens."""
    return sum(1 if t.value in "([{" else -1 if t.value in ")]}" else 0 for t in toks if t.kind == "OP")


def unescape(raw: str, lineno: int = 0, col: int = 0) -> str:
    """Process the backslash escapes of a string literal."""
    if "\\" not in raw:
        return raw
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\":
            nxt = raw[i + 1:i + 2]
            if nxt not in ESCAPES:
                raise ZyloError(f"unknown escape sequence \\{nxt}", lineno, col + i)
            out.append(ESCAPES[nxt])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _scan_string(line: str, i: int, lineno: int, col: int) -> int:
    """`line[i]` is a quote; return the index of the matching closing quote (backslashes escape)."""
    quote, j = line[i], i + 1
    while j < len(line):
        if line[j] == "\\":
            j += 2
        elif line[j] == quote:
            return j
        else:
            j += 1
    raise ZyloError("unterminated string", lineno, col)


def _lex_line(line: str, i: int, lineno: int, out: list[Token]) -> None:
    n = len(line)
    while i < n:
        c = line[i]
        col = i + 1
        if c == " ":
            i += 1
        elif c == "#":
            break
        elif c.isdigit():
            j = i
            while j < n and line[j].isdigit():
                j += 1
            kind = "INT"
            if j < n and line[j] == "." and j + 1 < n and line[j + 1].isdigit():
                kind = "FLOAT"
                j += 1
                while j < n and line[j].isdigit():
                    j += 1
            out.append(Token(kind, line[i:j], lineno, col))
            i = j
        elif c == "f" and line[i + 1:i + 2] in ('"', "'"):  # f-string: kept raw, parsed by the parser
            j = _scan_string(line, i + 1, lineno, col + 1)
            out.append(Token("FSTR", line[i + 2:j], lineno, col))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] in "_\u00b7"):  # `·` joins the linker's `module·name`
                j += 1
            word = line[i:j]
            out.append(Token("KW" if word in KEYWORDS else "IDENT", word, lineno, col))
            i = j
        elif c in ('"', "'"):
            j = _scan_string(line, i, lineno, col)
            out.append(Token("STR", unescape(line[i + 1:j], lineno, col + 1), lineno, col))
            i = j + 1
        elif line[i:i + 2] in TWO_CHAR:
            out.append(Token("OP", line[i:i + 2], lineno, col))
            i += 2
        elif c in ONE_CHAR:
            out.append(Token("OP", c, lineno, col))
            i += 1
        else:
            raise ZyloError(f"unexpected character {c!r}", lineno, col)
