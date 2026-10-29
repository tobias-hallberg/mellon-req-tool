from __future__ import annotations

from pathlib import Path

from mellon import load_document


def test_outline_path(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert doc.outline_path("DOC-0") == ("DOC-0",)
    assert doc.outline_path("DOC-1") == ("DOC-0", "DOC-1")
    assert doc.outline_path("DOC-999") is None


def test_incoming_links_same_doc(doc_folder: Path) -> None:
    # Redirect the existing link to point at DOC-0 so we can observe "incoming".
    (doc_folder / "links" / "DOC-1.xml").write_text(
        """<?xml version="1.0"?>
<links source="DOC-1">
  <link target-doc="DOC" target-id="DOC-0" type="refines" ack="abcdef1"/>
</links>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    incoming = doc.incoming_links("DOC-0")
    assert len(incoming) == 1
    src, link = incoming[0]
    assert src == "DOC-1"
    assert link.type == "refines"
    assert doc.incoming_links("DOC-1") == []
