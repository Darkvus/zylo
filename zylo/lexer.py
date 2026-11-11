from dataclasses import dataclass

from .errors import ZyloError

KEYWORDS = {
    "fn", "if", "elif", "else", "while", "for", "in", "return", "break", "continue",
    "and", "or", "not", "true", "false", "struct", "is", "none",
}
TWO_CHAR = {"==", "!=", "<=", ">=", "->", "+=", "-=", "*=", "/=", "%="}
ONE_CHAR = set("+-*/%<>=(),:[].?")


@dataclass(frozen=True)
class Token:
    kind: str  # INT FLOAT STR IDENT KW OP NEWLINE INDENT DEDENT EOF
    value: str
    line: int
    col: int


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    indents = [0]
    end = (1, 1)  # position after the last line with code
    for lineno, raw in enumerate(src.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        width = len(line) - len(line.lstrip(" "))
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
        _lex_line(line, width, lineno, tokens)
        tokens.append(Token("NEWLINE", "", lineno, len(line) + 1))
        end = (lineno, len(line) + 1)
    while len(indents) > 1:
        indents.pop()
        tokens.append(Token("DEDENT", "", *end))
    tokens.append(Token("EOF", "", *end))
    return tokens


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
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            word = line[i:j]
            out.append(Token("KW" if word in KEYWORDS else "IDENT", word, lineno, col))
            i = j
        elif c == '"':
            j = line.find('"', i + 1)
            if j == -1:
                raise ZyloError("unterminated string", lineno, col)
            out.append(Token("STR", line[i + 1:j], lineno, col))
            i = j + 1
        elif line[i:i + 2] in TWO_CHAR:
            out.append(Token("OP", line[i:i + 2], lineno, col))
            i += 2
        elif c in ONE_CHAR:
            out.append(Token("OP", c, lineno, col))
            i += 1
        else:
            raise ZyloError(f"unexpected character {c!r}", lineno, col)
