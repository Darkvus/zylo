"""Language Server Protocol server: `zylo lsp`.

Speaks JSON-RPC over standard input and output (messages framed by `Content-Length` headers) and provides, to any
editor that supports the protocol: live diagnostics (every error, checked on the unsaved text), formatting, the
outline of a document, and completion. The logic lives in the application layer; this module only translates.
"""

import json
import re
import sys
import traceback
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from zylo import __version__
from zylo.application.compile import diagnose_source
from zylo.application.editor import completions, document_symbols
from zylo.application.format import format_text
from zylo.domain.errors import ZyloError

ERROR = 1  # DiagnosticSeverity
SYMBOL_KINDS = {"function": 12, "struct": 23, "enum": 10, "method": 6, "field": 8, "variant": 22, "variable": 13}
COMPLETION_KINDS = {"keyword": 14, "function": 3, "struct": 22, "enum": 13, "variable": 6}


def uri_to_path(uri: str) -> Path:
    """Convert a `file://` URI to a path.

    Args:
        uri: The document's URI.

    Returns:
        The local path.
    """
    parsed = urlparse(uri)
    return Path(url2pathname(unquote(parsed.path)))


def error_range(e: ZyloError, source: str) -> dict:
    """The LSP range of an error: from its position to the end of the word there (or one character).

    Args:
        e: The error (its line and column are 1-based; 0 means unknown).
        source: The text of the document it is in.

    Returns:
        A `Range` with 0-based lines and characters.
    """
    line = max(e.line - 1, 0)
    col = max(e.col - 1, 0)
    lines = source.splitlines()
    text = lines[line] if line < len(lines) else ""
    m = re.match(r"\w+", text[col:])
    end = col + (len(m.group(0)) if m else 1)
    return {"start": {"line": line, "character": col}, "end": {"line": line, "character": max(end, col + 1)}}


def to_diagnostics(errors: list[ZyloError], source: str, path: Path) -> list[dict]:
    """Turn errors into LSP diagnostics for one document.

    Errors that belong to another file (an imported module) are shown at the top of this document, naming the file.

    Args:
        errors: The errors from `diagnose_source`.
        source: The document's text.
        path: The document's path.

    Returns:
        LSP `Diagnostic` objects.
    """
    out = []
    for e in errors:
        elsewhere = e.file and Path(e.file).resolve() != path.resolve()
        if elsewhere:
            where = f"{Path(e.file).name}:{e.line}:{e.col}"
            out.append({"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                        "severity": ERROR, "source": "zylo", "message": f"in {where}: {e.msg}"})
        else:
            out.append({"range": error_range(e, source), "severity": ERROR, "source": "zylo", "message": e.msg})
    return out


def symbol_json(sym) -> dict:
    """Convert an application `Symbol` to an LSP `DocumentSymbol`."""
    start = {"line": sym.line - 1, "character": 0}
    name_start = {"line": sym.line - 1, "character": sym.col - 1}
    name_end = {"line": sym.line - 1, "character": sym.col - 1 + len(sym.name)}
    return {"name": sym.name, "detail": sym.detail, "kind": SYMBOL_KINDS[sym.kind],
            "range": {"start": start, "end": {"line": sym.end_line - 1, "character": 1000}},
            "selectionRange": {"start": name_start, "end": name_end},
            "children": [symbol_json(c) for c in sym.children]}


class Server:
    """A language server speaking over a pair of byte streams.

    Attributes:
        documents: The text of every open document, by URI.
    """

    def __init__(self, reader: BinaryIO, writer: BinaryIO):
        """Create a server.

        Args:
            reader: Where client messages come from (standard input).
            writer: Where replies and notifications go (standard output).
        """
        self.reader, self.writer = reader, writer
        self.documents: dict[str, str] = {}
        self.shutdown_requested = False

    # ---- transport ----

    def read_message(self) -> dict | None:
        """Read one framed message.

        Returns:
            The decoded message, or None at the end of the input.
        """
        length = None
        while True:
            header = self.reader.readline()
            if not header:
                return None
            header = header.strip()
            if not header:
                break
            name, _, value = header.decode("ascii", "replace").partition(":")
            if name.lower() == "content-length":
                length = int(value.strip())
        if length is None:
            return {}
        return json.loads(self.reader.read(length).decode("utf-8"))

    def send(self, message: dict) -> None:
        """Write one framed message."""
        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        self.writer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        self.writer.flush()

    def reply(self, msg: dict, result) -> None:
        """Answer a request."""
        self.send({"id": msg["id"], "result": result})

    def fail(self, msg: dict, code: int, text: str) -> None:
        """Answer a request with an error."""
        self.send({"id": msg["id"], "error": {"code": code, "message": text}})

    # ---- main loop ----

    def serve(self) -> int:
        """Handle messages until the client exits.

        Returns:
            The process exit code: 0 after a proper `shutdown` + `exit`, 1 if `exit` came without `shutdown`.
        """
        while True:
            msg = self.read_message()
            if msg is None:
                return 0 if self.shutdown_requested else 1
            if not msg:
                continue
            method = msg.get("method")
            if method == "exit":
                return 0 if self.shutdown_requested else 1
            try:
                self.dispatch(method, msg)
            except Exception as e:  # a bug in one request must not kill the editor session
                traceback.print_exc(file=sys.stderr)
                if "id" in msg:
                    self.fail(msg, -32603, f"internal error: {e}")

    def dispatch(self, method: str | None, msg: dict) -> None:
        """Route one message to its handler."""
        params = msg.get("params") or {}
        is_request = "id" in msg
        handler = {
            "initialize": self.on_initialize,
            "shutdown": self.on_shutdown,
            "textDocument/didOpen": self.on_open,
            "textDocument/didChange": self.on_change,
            "textDocument/didSave": self.on_save,
            "textDocument/didClose": self.on_close,
            "textDocument/formatting": self.on_formatting,
            "textDocument/documentSymbol": self.on_symbols,
            "textDocument/completion": self.on_completion,
        }.get(method)
        if handler is None:
            if is_request:
                self.fail(msg, -32601, f"method not found: {method}")
            return
        result = handler(params)
        if is_request:
            self.reply(msg, result)

    # ---- handlers ----

    def on_initialize(self, params: dict) -> dict:
        """Describe what this server can do."""
        return {"capabilities": {
            "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
            "documentFormattingProvider": True,
            "documentSymbolProvider": True,
            "completionProvider": {"triggerCharacters": ["."]},
        }, "serverInfo": {"name": "zylo", "version": __version__}}

    def on_shutdown(self, params: dict):
        """Remember that the client asked to shut down."""
        self.shutdown_requested = True
        return None

    def on_open(self, params: dict) -> None:
        """A document was opened: store its text and report its errors."""
        doc = params["textDocument"]
        self.documents[doc["uri"]] = doc["text"]
        self.publish(doc["uri"])

    def on_change(self, params: dict) -> None:
        """A document changed (full-text sync): store the new text and report its errors."""
        uri = params["textDocument"]["uri"]
        self.documents[uri] = params["contentChanges"][-1]["text"]
        self.publish(uri)

    def on_save(self, params: dict) -> None:
        """A document was saved: check it again, since the files it imports may have changed too."""
        uri = params["textDocument"]["uri"]
        if uri in self.documents:
            self.publish(uri)

    def on_close(self, params: dict) -> None:
        """A document was closed: forget it and clear its diagnostics."""
        uri = params["textDocument"]["uri"]
        self.documents.pop(uri, None)
        self.send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})

    def on_formatting(self, params: dict) -> list[dict]:
        """Format the whole document; nothing is returned for a document with a syntax error."""
        text = self.documents.get(params["textDocument"]["uri"], "")
        try:
            formatted = format_text(text)
        except ZyloError:
            return []
        if formatted == text:
            return []
        end_line = len(text.splitlines())
        return [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": end_line + 1, "character": 0}},
                 "newText": formatted}]

    def on_symbols(self, params: dict) -> list[dict]:
        """The outline of the document."""
        return [symbol_json(s) for s in document_symbols(self.documents.get(params["textDocument"]["uri"], ""))]

    def on_completion(self, params: dict) -> list[dict]:
        """Completion candidates for the document."""
        text = self.documents.get(params["textDocument"]["uri"], "")
        return [{"label": c.label, "kind": COMPLETION_KINDS[c.kind], "detail": c.detail} for c in completions(text)]

    # ---- diagnostics ----

    def publish(self, uri: str) -> None:
        """Check a document and send its diagnostics."""
        text = self.documents[uri]
        path = uri_to_path(uri)
        try:
            diagnostics = to_diagnostics(diagnose_source(text, path), text, path)
        except ZyloError as e:  # a broken manifest or environment, not a mistake in the program
            diagnostics = [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                            "severity": ERROR, "source": "zylo", "message": e.msg}]
        self.send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diagnostics}})


def main() -> int:
    """Run the server on standard input and output.

    Returns:
        The process exit code.
    """
    return Server(sys.stdin.buffer, sys.stdout.buffer).serve()
