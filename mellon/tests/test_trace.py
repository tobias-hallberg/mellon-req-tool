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


# MELLON-TRACE-SRS blind tests
import random  # noqa: E402

from mellon.repo import load_all  # noqa: E402
from tests.trace_support import Repo  # noqa: E402


def edge_ids(edges) -> list:
    """Library TraceEdge tuples as nested (target_id, children) pairs."""
    return [(e.target_id, edge_ids(e.children)) for e in edges]


# ===========================================================================
# Trace (MELLON-TRACE-SRS-160, -168, -169, -171, -172, -175)
# ===========================================================================


def _graph(tmp_path, n, edges, name="g"):
    r = Repo(tmp_path / name, git=False)
    r.add_doc("CYC", {f"CYC-{i}": f"object {i}" for i in range(1, n + 1)})
    out = {}
    for s_, t in edges:
        out.setdefault(s_, []).append(t)
    for s_, ts in out.items():
        r.set_links("CYC", f"CYC-{s_}", [("CYC", f"CYC-{t}", "refines", None) for t in ts])
    return load_all(r.root)


def test_trace_srs_160_trace_down_ends_at_an_object_reached_a_second_time(tmp_path):
    """MELLON-TRACE-SRS-160: Mellon shall end each path of a trace at the first object that the path
    reaches a second time. Condition: A <- B <- A (A links to B and B to A), traced down from A.
    Expected: the path A, B, A ends at A, its last object."""
    docs = _graph(tmp_path, 2, [(1, 2), (2, 1)])
    assert edge_ids(trace_down(docs, "CYC", "CYC-1")) == [("CYC-2", [("CYC-1", [])])]


def test_trace_srs_160_trace_up_ends_at_an_object_reached_a_second_time(tmp_path):
    """MELLON-TRACE-SRS-160: Mellon shall end each path of a trace at the first object that the path
    reaches a second time. Condition: the cycle A -> B -> C -> A traced up from A, and A -> B -> C -> B
    (a cycle not through the traced object). Expected: each path ends at the repeated object."""
    docs = _graph(tmp_path, 3, [(1, 2), (2, 3), (3, 1)], "g1")
    assert edge_ids(trace_up(docs, "CYC", "CYC-1")) == [("CYC-2", [("CYC-3", [("CYC-1", [])])])]
    docs = _graph(tmp_path, 3, [(1, 2), (2, 3), (3, 2)], "g2")
    assert edge_ids(trace_up(docs, "CYC", "CYC-1")) == [("CYC-2", [("CYC-3", [("CYC-2", [])])])]


def _check_paths(edges, root, adjacency, path=None):
    """Each path of a trace follows every link of each object on it and ends exactly where an object
    repeats or no link leads on."""
    path = [root] if path is None else path
    last = path[-1]
    repeated = last in path[:-1]
    kids = [(e.target_id, e) for e in edges]
    if repeated:
        assert kids == [], f"path {path} goes on past the repeated {last}"
        return
    assert sorted(k for k, _ in kids) == sorted(adjacency.get(last, [])), f"at {path}: {sorted(k for k, _ in kids)}"
    for k, e in kids:
        _check_paths(e.children, root, adjacency, path + [k])


def test_trace_srs_160_paths_end_at_the_first_repeat_in_generated_graphs(tmp_path):
    """MELLON-TRACE-SRS-160: Mellon shall end each path of a trace at the first object that the path
    reaches a second time. Condition (T11): 40 generated link graphs of 5 objects with cycles, traced
    up and down from each object. Expected: each path follows every link and ends at its first repeat."""
    rng = random.Random(160)
    for g in range(40):
        pairs = [(a, b) for a in range(1, 6) for b in range(1, 6) if a != b]
        edges = rng.sample(pairs, rng.randint(3, 8))
        docs = _graph(tmp_path, 5, edges, f"g{g}")
        up, down = {}, {}
        for a, b in edges:
            up.setdefault(f"CYC-{a}", []).append(f"CYC-{b}")
            down.setdefault(f"CYC-{b}", []).append(f"CYC-{a}")
        for i in range(1, 6):
            oid = f"CYC-{i}"
            _check_paths(trace_up(docs, "CYC", oid), oid, up)
            _check_paths(trace_down(docs, "CYC", oid), oid, down)
