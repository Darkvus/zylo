"""The language server: protocol framing, diagnostics, formatting, symbols and completion."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from zylo import __version__
from zylo.application.editor import completions, document_symbols
from zylo.domain.errors import ZyloError
from zylo.interface.lsp import Server, error_range, uri_to_path

URI = "file:///tmp/zylo_lsp_test/doc.zy"


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


def run(*messages: dict, raw: bytes = b"") -> tuple[list[dict], int]:
    """Feed messages to an in-process server; return everything it wrote and its exit code."""
    reader, writer = io.BytesIO(raw + b"".join(frame(m) for m in messages)), io.BytesIO()
    code = Server(reader, writer).serve()
    return parse_frames(writer.getvalue()), code


def open_doc(text: str, uri: str = URI) -> dict:
    return {"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "text": text}}}


def diagnostics_of(replies: list[dict]) -> list[list[dict]]:
    return [m["params"]["diagnostics"] for m in replies if m.get("method") == "textDocument/publishDiagnostics"]


def result(replies: list[dict], id_: int):
    return next(m for m in replies if m.get("id") == id_)


# ---- protocol ---------------------------------------------------------------------------------------

def test_initialize_announces_the_capabilities():
    replies, _ = run({"id": 1, "method": "initialize", "params": {}})
    caps = result(replies, 1)["result"]
    assert caps["serverInfo"] == {"name": "zylo", "version": __version__}
    c = caps["capabilities"]
    assert c["textDocumentSync"]["change"] == 1 and c["documentFormattingProvider"] and c["documentSymbolProvider"]
    assert "." in c["completionProvider"]["triggerCharacters"]


def test_shutdown_then_exit_is_a_clean_exit():
    _, code = run({"id": 1, "method": "shutdown"}, {"method": "exit"})
    assert code == 0
    assert run({"method": "exit"})[1] == 1  # `exit` without `shutdown`
    assert run()[1] == 1  # the client vanished


def test_unknown_requests_get_an_error_and_unknown_notifications_are_ignored():
    replies, _ = run({"id": 7, "method": "textDocument/nope"}, {"method": "$/cancelRequest", "params": {}},
                    {"id": 8, "method": "shutdown"})
    assert result(replies, 7)["error"]["code"] == -32601
    assert result(replies, 8)["result"] is None and len(replies) == 2


def test_a_broken_request_does_not_kill_the_server():
    replies, _ = run({"id": 1, "method": "textDocument/documentSymbol", "params": {}}, {"id": 2, "method": "shutdown"})
    assert result(replies, 1)["error"]["code"] == -32603
    assert result(replies, 2)["result"] is None  # still answering


def test_a_message_without_content_length_is_skipped():
    replies, _ = run({"id": 1, "method": "shutdown"}, raw=b"Some-Header: x\r\n\r\n")
    assert result(replies, 1)["result"] is None


def test_uri_to_path_decodes():
    assert uri_to_path("file:///tmp/a%20b/c.zy") == Path("/tmp/a b/c.zy")


# ---- diagnostics ----------------------------------------------------------------------------------------

def test_opening_a_document_reports_every_error():
    replies, _ = run(open_doc('x = 1 + "a"\nprint(zzz)\n'))
    (diags,) = diagnostics_of(replies)
    assert [(d["range"]["start"]["line"], d["message"]) for d in diags] == [
        (0, "`+` between int and str: types must match"), (1, "undefined variable 'zzz'")]
    assert all(d["severity"] == 1 and d["source"] == "zylo" for d in diags)


def test_the_range_covers_the_word_at_the_error():
    replies, _ = run(open_doc("print(zzz)\n"))
    (diags,) = diagnostics_of(replies)
    assert diags[0]["range"] == {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 9}}


def test_error_range_handles_unknown_positions_and_symbols():
    assert error_range(ZyloError("x"), "abc") == {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}
    assert error_range(ZyloError("x", 1, 1), "")["end"]["character"] == 1
    assert error_range(ZyloError("x", 1, 3), "a + b")["start"]["character"] == 2
    assert error_range(ZyloError("x", 5, 1), "one line")["start"]["line"] == 4  # past the end: still safe


def test_syntax_errors_are_all_reported_before_any_type_error():
    replies, _ = run(open_doc("x = (\nprint(zzz)\nfn f(:\n"))
    (diags,) = diagnostics_of(replies)
    assert [d["range"]["start"]["line"] for d in diags] == [0, 2]


def test_changing_the_document_rechecks_it_and_closing_clears_it():
    change = {"method": "textDocument/didChange", "params": {"textDocument": {"uri": URI},
                                                              "contentChanges": [{"text": "print(1)\n"}]}}
    close = {"method": "textDocument/didClose", "params": {"textDocument": {"uri": URI}}}
    replies, _ = run(open_doc("print(zzz)\n"), change, close)
    first, fixed, cleared = diagnostics_of(replies)
    assert len(first) == 1 and fixed == [] and cleared == []


def test_only_the_last_change_counts():
    change = {"method": "textDocument/didChange", "params": {"textDocument": {"uri": URI},
                                                              "contentChanges": [{"text": "x = (\n"}, {"text": "x = 1\n"}]}}
    replies, _ = run(open_doc("x = 1\n"), change)
    assert diagnostics_of(replies) == [[], []]


def test_an_error_in_an_imported_module_is_shown_at_the_top_of_the_importer(tmp_path):
    (tmp_path / "lib.zy").write_text('fn f() -> int:\n    return "oops"\n')
    main = tmp_path / "main.zy"
    main.write_text("import lib\nprint(lib.f())\n")
    replies, _ = run(open_doc(main.read_text(), main.as_uri()))
    (diags,) = diagnostics_of(replies)
    assert len(diags) == 1 and diags[0]["range"]["start"] == {"line": 0, "character": 0}
    assert diags[0]["message"].startswith("in lib.zy:2:") and "returning str" in diags[0]["message"]


def test_unsaved_text_is_checked_not_the_file_on_disk(tmp_path):
    main = tmp_path / "main.zy"
    main.write_text("print(1)\n")  # fine on disk
    replies, _ = run(open_doc("print(zzz)\n", main.as_uri()))
    assert len(diagnostics_of(replies)[0]) == 1


def test_a_broken_manifest_is_reported_as_a_diagnostic(tmp_path):
    (tmp_path / "zylo.toml").write_text("not [valid")
    main = tmp_path / "main.zy"
    replies, _ = run(open_doc("print(1)\n", main.as_uri()))
    (diags,) = diagnostics_of(replies)
    assert "invalid TOML" in diags[0]["message"]


# ---- formatting, symbols, completion ---------------------------------------------------------------------

def test_formatting_returns_one_edit_for_the_whole_document():
    replies, _ = run(open_doc("x=1+2\n"), {"id": 1, "method": "textDocument/formatting", "params": {"textDocument": {"uri": URI}}})
    (edit,) = result(replies, 1)["result"]
    assert edit["newText"] == "x = 1 + 2\n" and edit["range"]["start"] == {"line": 0, "character": 0}


def test_formatting_has_nothing_to_do_for_formatted_or_broken_documents():
    ask = {"id": 1, "method": "textDocument/formatting", "params": {"textDocument": {"uri": URI}}}
    assert result(run(open_doc("x = 1\n"), ask)[0], 1)["result"] == []
    assert result(run(open_doc("x = (\n"), ask)[0], 1)["result"] == []


def test_document_symbols_form_an_outline():
    src = "x = 1\n\nfn add(a: int, b: int) -> int:\n    return a + b\n\nstruct P:\n    n: int\n\n    fn get(self) -> int:\n        return self.n\n"
    syms = document_symbols(src)
    assert [(s.name, s.kind) for s in syms] == [("x", "variable"), ("add", "function"), ("P", "struct")]
    assert syms[1].detail == "fn add(a: int, b: int) -> int" and (syms[1].line, syms[1].end_line) == (3, 4)
    assert [(c.name, c.kind) for c in syms[2].children] == [("n", "field"), ("get", "method")]
    assert syms[2].children[1].detail == "fn get(self) -> int"


def test_symbols_survive_syntax_errors():
    names = [s.name for s in document_symbols("fn good() -> int:\n    return 1\nx = (\nfn also():\n    return\n")]
    assert names == ["good", "also"]


def test_the_symbols_reach_the_client_as_lsp_document_symbols():
    replies, _ = run(open_doc("struct P:\n    n: int\n"), {"id": 1, "method": "textDocument/documentSymbol", "params": {"textDocument": {"uri": URI}}})
    (p,) = result(replies, 1)["result"]
    assert p["name"] == "P" and p["kind"] == 23 and p["children"][0]["kind"] == 8
    assert p["selectionRange"]["start"] == {"line": 0, "character": 7}


def test_completion_offers_keywords_builtins_and_the_documents_own_names():
    items = completions("fn helper() -> int:\n    return 1\nstruct Point:\n    x: int\ncount = 0\n")
    labels = {c.label: c.kind for c in items}
    assert labels["fn"] == "keyword" and labels["print"] == "function" and labels["helper"] == "function"
    assert labels["Point"] == "struct" and labels["count"] == "variable"
    assert len([c for c in items if c.label == "print"]) == 1  # no duplicates


def test_completion_over_the_protocol():
    replies, _ = run(open_doc("fn mine():\n    return\n"), {"id": 1, "method": "textDocument/completion", "params": {"textDocument": {"uri": URI}}})
    labels = {i["label"]: i["kind"] for i in result(replies, 1)["result"]}
    assert labels["mine"] == 3 and labels["fn"] == 14


# ---- the real thing, over standard input and output -----------------------------------------------------

def test_the_server_works_as_a_subprocess():
    messages = [{"id": 1, "method": "initialize", "params": {}}, open_doc("print(zzz)\n"),
                {"id": 2, "method": "shutdown"}, {"method": "exit"}]
    p = subprocess.run([sys.executable, "-m", "zylo", "lsp"], input=b"".join(frame(m) for m in messages),
                       capture_output=True, timeout=60)
    replies = parse_frames(p.stdout)
    assert p.returncode == 0
    assert result(replies, 1)["result"]["serverInfo"]["name"] == "zylo"
    assert diagnostics_of(replies)[0][0]["message"] == "undefined variable 'zzz'"
    assert result(replies, 2)["result"] is None


@pytest.mark.parametrize("bad", [b"", b"\r\n\r\n"])
def test_garbage_does_not_crash_the_server(bad):
    assert run(raw=bad)[1] in (0, 1)
