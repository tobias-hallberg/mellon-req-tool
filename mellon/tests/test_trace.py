"""Tests for recursive traceability (trace_up/trace_down)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mellon import LoadedDoc, load_document, trace_down, trace_up


def _write_manifest(folder: Path, doc_id: str, children_ids: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "objects").mkdir(exist_ok=True)
    (folder / "links").mkdir(exist_ok=True)
    refs = "\n    ".join(f'<ref id="{c}"/>' for c in children_ids)
    (folder / f"{doc_id}.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<document id="{doc_id}">
  <metadata><title>{doc_id}</title></metadata>
  <attributes>
    <attribute name="status" kind="enum" required="true"><option>Draft</option></attribute>
  </attributes>
  <outline>
    {refs}
  </outline>
</document>
""",
        encoding="utf-8",
    )


def _write_obj(folder: Path, obj_id: str, description: str) -> None:
    (folder / "objects" / f"{obj_id}.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<object id="{obj_id}">
  <attr name="status">Draft</attr>
  <description>{description}</description>
</object>
""",
        encoding="utf-8",
    )


def _write_link(
    folder: Path, source_id: str, target_doc: str, target_id: str, link_type: str
) -> None:
    (folder / "links" / f"{source_id}.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<links source="{source_id}">
  <link target-doc="{target_doc}" target-id="{target_id}" type="{link_type}"/>
</links>
""",
        encoding="utf-8",
    )


@pytest.fixture
def three_level_repo(tmp_path: Path):
    """StRS-1 <- SyRS-1 <- SRS-1 (three-level trace chain)."""
    strs = tmp_path / "StRS"
    syrs = tmp_path / "SyRS"
    srs = tmp_path / "SRS"

    _write_manifest(strs, "StRS", ["StRS-1"])
    _write_obj(strs, "StRS-1", "Stakeholder need")

    _write_manifest(syrs, "SyRS", ["SyRS-1"])
    _write_obj(syrs, "SyRS-1", "System requirement")
    _write_link(syrs, "SyRS-1", "StRS", "StRS-1", "refines")

    _write_manifest(srs, "SRS", ["SRS-1"])
    _write_obj(srs, "SRS-1", "Software requirement")
    _write_link(srs, "SRS-1", "SyRS", "SyRS-1", "satisfies")

    return tmp_path, strs, syrs, srs


def _docs_fixture(paths: list[Path]) -> dict[str, LoadedDoc]:
    out: dict[str, LoadedDoc] = {}
    for p in paths:
        doc = load_document(p)
        out[doc.id] = LoadedDoc(folder=p, document=doc)
    return out


def test_trace_up_follows_chain(three_level_repo):
    _root, strs, syrs, srs = three_level_repo
    docs = _docs_fixture([strs, syrs, srs])

    edges = trace_up(docs, "SRS", "SRS-1")
    assert len(edges) == 1
    (e1,) = edges
    assert (e1.link_type, e1.target_doc, e1.target_id) == ("satisfies", "SyRS", "SyRS-1")
    assert len(e1.children) == 1
    (e2,) = e1.children
    assert (e2.link_type, e2.target_doc, e2.target_id) == ("refines", "StRS", "StRS-1")
    assert e2.children == ()  # Top of the chain.


def test_trace_down_follows_chain(three_level_repo):
    _root, strs, syrs, srs = three_level_repo
    docs = _docs_fixture([strs, syrs, srs])

    edges = trace_down(docs, "StRS", "StRS-1")
    assert len(edges) == 1
    (e1,) = edges
    assert (e1.link_type, e1.target_doc, e1.target_id) == ("refines", "SyRS", "SyRS-1")
    (e2,) = e1.children
    assert (e2.link_type, e2.target_doc, e2.target_id) == ("satisfies", "SRS", "SRS-1")


def test_trace_cycle_safe(tmp_path: Path):
    """A -> B -> A should not loop."""
    a = tmp_path / "A"
    b = tmp_path / "B"
    _write_manifest(a, "A", ["A-1"])
    _write_obj(a, "A-1", "a")
    _write_link(a, "A-1", "B", "B-1", "x")

    _write_manifest(b, "B", ["B-1"])
    _write_obj(b, "B-1", "b")
    _write_link(b, "B-1", "A", "A-1", "x")

    docs = _docs_fixture([a, b])
    edges = trace_up(docs, "A", "A-1")
    assert len(edges) == 1
    (e,) = edges
    assert (e.target_doc, e.target_id) == ("B", "B-1")
    (inner,) = e.children
    assert (inner.target_doc, inner.target_id) == ("A", "A-1")
    assert inner.children == ()  # Cycle cut here.


