"""Every code block marked `zylo` in the documentation must be a valid, type-correct program."""
import re
from pathlib import Path

import pytest

from zylo.semantics.checker import check
from zylo.syntax.parser import parse

ROOT = Path(__file__).parent.parent
DOCS = [ROOT / "README.md", ROOT / "docs" / "guide.md"]
BLOCK = re.compile(r"```zylo\n(.*?)```", re.S)

CASES = [(doc, i, code) for doc in DOCS for i, code in enumerate(BLOCK.findall(doc.read_text()), start=1)]


def test_docs_have_zylo_blocks():
    assert len(CASES) >= 12


@pytest.mark.parametrize("doc,i,code", CASES, ids=[f"{d.name}#{i}" for d, i, _ in CASES])
def test_doc_block_is_valid(doc: Path, i: int, code: str):
    check(parse(code))
