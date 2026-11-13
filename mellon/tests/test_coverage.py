"""Tests for coverage_report (`mellon.coverage`).

Fixtures follow the V-model link convention used in the rest of the codebase:
verification/refinement links are authored on the *child* document and point
*up* at the parent. So in a fixture where STS tests verify SRS reqs, the
links live on STS objects and target SRS objects with type="verifies".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mellon import LoadedDoc, load_document
from mellon.coverage import coverage_report
from mellon.mcp.serialize import coverage_report_to_dict


# --- fixture builders -----------------------------------------------------


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
    <attribute name="type" kind="enum" required="false">
      <option>Heading</option>
      <option>Functional</option>
    </attribute>
  </attributes>
  <outline>
    {refs}
  </outline>
</document>
""",
        encoding="utf-8",
    )


def _write_obj(
    folder: Path, obj_id: str, description: str, *, type_attr: str = "Functional"
) -> None:
    (folder / "objects" / f"{obj_id}.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<object id="{obj_id}">
  <attr name="type">{type_attr}</attr>
  <description>{description}</description>
</object>
""",
        encoding="utf-8",
    )


def _write_link(
    folder: Path,
    source_id: str,
    target_doc: str,
    target_id: str,
    link_type: str,
) -> None:
    (folder / "links" / f"{source_id}.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<links source="{source_id}">
  <link target-doc="{target_doc}" target-id="{target_id}" type="{link_type}"/>
</links>
""",
        encoding="utf-8",
    )


def _docs_fixture(paths: list[Path]) -> dict[str, LoadedDoc]:
    out: dict[str, LoadedDoc] = {}
    for p in paths:
        doc = load_document(p)
        out[doc.id] = LoadedDoc(folder=p, document=doc)
    return out


@pytest.fixture
def two_doc_repo(tmp_path: Path):
    """SRS (3 reqs) covered by STS (2 tests verifying SRS-0 and SRS-1).

    STS-0 -[verifies]-> SRS-0
    STS-1 -[verifies]-> SRS-1
    SRS-2 has no verifying test.
    """
    srs = tmp_path / "SRS"
    sts = tmp_path / "STS"
    _write_manifest(srs, "SRS", ["SRS-0", "SRS-1", "SRS-2"])
    _write_obj(srs, "SRS-0", "Covered req 0")
    _write_obj(srs, "SRS-1", "Covered req 1")
    _write_obj(srs, "SRS-2", "Uncovered req")

    _write_manifest(sts, "STS", ["STS-0", "STS-1"])
    _write_obj(sts, "STS-0", "Test 0")
    _write_obj(sts, "STS-1", "Test 1")
    _write_link(sts, "STS-0", "SRS", "SRS-0", "verifies")
    _write_link(sts, "STS-1", "SRS", "SRS-1", "verifies")

    return tmp_path, srs, sts


@pytest.fixture
def three_level_repo(tmp_path: Path):
    """V-model chain: STS verifies SRS, SRS satisfies SyRS.

    STS-0 -[verifies]-> SRS-0 -[satisfies]-> SyRS-0
    SRS-1 has no STS test (uncovered direct & transitive against STS).
    SyRS-0 has no direct STS test but is reachable transitively via SRS-0.
    """
    syrs = tmp_path / "SyRS"
    srs = tmp_path / "SRS"
    sts = tmp_path / "STS"

    _write_manifest(syrs, "SyRS", ["SyRS-0"])
    _write_obj(syrs, "SyRS-0", "System req")

    _write_manifest(srs, "SRS", ["SRS-0", "SRS-1"])
    _write_obj(srs, "SRS-0", "Software req covered")
    _write_obj(srs, "SRS-1", "Software req uncovered")
    _write_link(srs, "SRS-0", "SyRS", "SyRS-0", "satisfies")

    _write_manifest(sts, "STS", ["STS-0"])
    _write_obj(sts, "STS-0", "Test 0")
    _write_link(sts, "STS-0", "SRS", "SRS-0", "verifies")

    return tmp_path, syrs, srs, sts


@pytest.fixture
def headings_repo(tmp_path: Path):
    """SRS with one Heading + two Functional reqs (one tested by STS, one not)."""
    srs = tmp_path / "SRS"
    sts = tmp_path / "STS"
    _write_manifest(srs, "SRS", ["SRS-0", "SRS-1", "SRS-2"])
    _write_obj(srs, "SRS-0", "Section title", type_attr="Heading")
    _write_obj(srs, "SRS-1", "Tested req")
    _write_obj(srs, "SRS-2", "Untested req")

    _write_manifest(sts, "STS", ["STS-0"])
    _write_obj(sts, "STS-0", "Test")
    _write_link(sts, "STS-0", "SRS", "SRS-1", "verifies")

    return tmp_path, srs, sts


# --- direct, incoming-direction (V-model default) ------------------------


def test_coverage_partial_incoming(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    docs = _docs_fixture([srs, sts])

    report = coverage_report(docs, "SRS", "STS")  # incoming is default
    assert report.direction == "incoming"
    assert report.total == 3
    assert report.covered == 2
    assert report.coverage_percent == pytest.approx(66.666666, rel=1e-3)
    by_id = {row.source_id: row for row in report.rows}
    assert by_id["SRS-0"].covered is True
    assert by_id["SRS-0"].link_count == 1
    assert by_id["SRS-1"].covered is True
    assert by_id["SRS-2"].covered is False
    assert by_id["SRS-2"].link_count == 0


def test_coverage_full_incoming(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    # Add a third test verifying SRS-2 -> 100% coverage.
    _write_obj(sts, "STS-2", "Test 2")
    # Append STS-2 to STS outline.
    (sts / "STS.xml").write_text(
        (sts / "STS.xml").read_text(encoding="utf-8").replace(
            '<ref id="STS-1"/>',
            '<ref id="STS-1"/>\n    <ref id="STS-2"/>',
        ),
        encoding="utf-8",
    )
    _write_link(sts, "STS-2", "SRS", "SRS-2", "verifies")
    docs = _docs_fixture([srs, sts])

    report = coverage_report(docs, "SRS", "STS")
    assert report.total == 3
    assert report.covered == 3
    assert report.coverage_percent == 100.0


def test_coverage_zero_incoming(tmp_path: Path) -> None:
    srs = tmp_path / "SRS"
    sts = tmp_path / "STS"
    _write_manifest(srs, "SRS", ["SRS-0"])
    _write_obj(srs, "SRS-0", "Lonely")
    _write_manifest(sts, "STS", [])
    docs = _docs_fixture([srs, sts])

    report = coverage_report(docs, "SRS", "STS")
    assert report.total == 1
    assert report.covered == 0
    assert report.coverage_percent == 0.0


def test_coverage_link_type_filter_incoming(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    # Replace STS-0's link type so it no longer matches a "verifies" filter.
    _write_link(sts, "STS-0", "SRS", "SRS-0", "informs")
    docs = _docs_fixture([srs, sts])

    report = coverage_report(docs, "SRS", "STS", link_type="verifies")
    by_id = {row.source_id: row for row in report.rows}
    # SRS-0's only incoming link is "informs" -> filtered out -> uncovered.
    assert by_id["SRS-0"].covered is False
    # SRS-1 still has an incoming "verifies" link -> covered.
    assert by_id["SRS-1"].covered is True


# --- outgoing direction ---------------------------------------------------


def test_coverage_outgoing_direction(three_level_repo) -> None:
    """Outgoing checks justification: every SRS should satisfy a SyRS.

    SRS-0 has an outgoing link to SyRS-0, SRS-1 has none.
    """
    _root, syrs, srs, _sts = three_level_repo
    docs = _docs_fixture([syrs, srs])

    report = coverage_report(docs, "SRS", "SyRS", direction="outgoing")
    by_id = {row.source_id: row for row in report.rows}
    assert by_id["SRS-0"].covered is True
    assert by_id["SRS-0"].link_count == 1
    assert by_id["SRS-1"].covered is False


# --- headings ------------------------------------------------------------


def test_coverage_excludes_headings_by_default(headings_repo) -> None:
    _root, srs, sts = headings_repo
    docs = _docs_fixture([srs, sts])

    report = coverage_report(docs, "SRS", "STS")
    assert report.total == 2  # Heading dropped from denominator.
    ids = {row.source_id for row in report.rows}
    assert ids == {"SRS-1", "SRS-2"}


def test_coverage_include_headings(headings_repo) -> None:
    _root, srs, sts = headings_repo
    docs = _docs_fixture([srs, sts])

    report = coverage_report(docs, "SRS", "STS", include_headings=True)
    assert report.total == 3
    by_id = {row.source_id: row for row in report.rows}
    # Heading counts as uncovered (no test verifies it).
    assert by_id["SRS-0"].covered is False


# --- transitive ----------------------------------------------------------


def test_coverage_direct_misses_transitive_chain(three_level_repo) -> None:
    _root, syrs, srs, sts = three_level_repo
    docs = _docs_fixture([syrs, srs, sts])

    # Direct: STS doesn't link straight to SyRS-0 -> uncovered.
    report = coverage_report(docs, "SyRS", "STS", transitive=False)
    by_id = {row.source_id: row for row in report.rows}
    assert by_id["SyRS-0"].covered is False
    assert by_id["SyRS-0"].link_count == 0


def test_coverage_transitive_follows_chain(three_level_repo) -> None:
    _root, syrs, srs, sts = three_level_repo
    docs = _docs_fixture([syrs, srs, sts])

    # Transitive: STS reaches SyRS-0 via SRS-0 -> covered.
    report = coverage_report(docs, "SyRS", "STS", transitive=True)
    by_id = {row.source_id: row for row in report.rows}
    assert by_id["SyRS-0"].covered is True
    # Direct link_count is still zero.
    assert by_id["SyRS-0"].link_count == 0


def test_coverage_transitive_uncovered_remains_uncovered(three_level_repo) -> None:
    _root, syrs, srs, sts = three_level_repo
    docs = _docs_fixture([syrs, srs, sts])

    # SRS-1 has no test (and nothing reaches it transitively either).
    report = coverage_report(docs, "SRS", "STS", transitive=True)
    by_id = {row.source_id: row for row in report.rows}
    assert by_id["SRS-0"].covered is True
    assert by_id["SRS-1"].covered is False


# --- error cases ---------------------------------------------------------


def test_coverage_missing_source_doc_raises(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    docs = _docs_fixture([srs, sts])
    with pytest.raises(KeyError, match="source document"):
        coverage_report(docs, "MISSING", "STS")


def test_coverage_missing_target_doc_raises(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    docs = _docs_fixture([srs, sts])
    with pytest.raises(KeyError, match="target document"):
        coverage_report(docs, "SRS", "MISSING")


def test_coverage_unknown_direction_raises(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    docs = _docs_fixture([srs, sts])
    with pytest.raises(ValueError, match="unknown direction"):
        coverage_report(docs, "SRS", "STS", direction="sideways")  # type: ignore[arg-type]


# --- JSON serialization --------------------------------------------------


def test_coverage_report_to_dict_shape(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    docs = _docs_fixture([srs, sts])
    payload = coverage_report_to_dict(
        coverage_report(docs, "SRS", "STS"), include_lists=True
    )
    assert payload["total"] == 3
    assert payload["covered"] == 2
    assert payload["direction"] == "incoming"
    assert payload["transitive"] is False
    uncovered_ids = [u["obj_id"] for u in payload["uncovered"]]
    assert uncovered_ids == ["SRS-2"]


def test_coverage_report_to_dict_default_omits_lists(two_doc_repo) -> None:
    _root, srs, sts = two_doc_repo
    docs = _docs_fixture([srs, sts])
    payload = coverage_report_to_dict(coverage_report(docs, "SRS", "STS"))
    assert "uncovered" not in payload
    assert "covered_objects" not in payload
    assert payload["total"] == 3
    assert payload["covered"] == 2


# MELLON-TRACE-SRS blind tests
from mellon.repo import load_all  # noqa: E402
from tests.trace_support import Repo  # noqa: E402


# ===========================================================================
# Coverage (MELLON-TRACE-SRS-184, -197, -200)
# ===========================================================================


def _rows(report) -> dict:
    return {row.source_id: row.covered for row in report.rows}


def test_trace_srs_184_transitive_coverage_follows_a_chain_in_the_reports_direction(tmp_path):
    """MELLON-TRACE-SRS-184: When Mellon works out transitive coverage, Mellon shall count an object of
    the reported document as covered when a chain of links, each in the report's direction, joins the
    object to the covering document. Condition: STS-1 -> SRS-1 -> SYS-1, STS-2 unlinked, SYS-2 unlinked.
    Expected: outgoing STS against SYS and incoming SYS against STS cover STS-1 / SYS-1 transitively
    only; STS-2 and SYS-2 stay uncovered."""
    r = Repo(tmp_path / "repo", git=False)
    r.add_doc("SYS", {"SYS-1": "s1", "SYS-2": "s2"})
    r.add_doc("SRS", {"SRS-1": "r1"}, parents=["SYS"])
    r.add_doc("STS", {"STS-1": ("t1", "Test Case"), "STS-2": ("t2", "Test Case")}, parents=["SRS"])
    r.set_links("SRS", "SRS-1", [("SYS", "SYS-1", "satisfies", None)])
    r.set_links("STS", "STS-1", [("SRS", "SRS-1", "verifies", None)])
    docs = load_all(r.root)
    assert _rows(coverage_report(docs, "STS", "SYS", direction="outgoing", transitive=True)) == {"STS-1": True, "STS-2": False}
    assert _rows(coverage_report(docs, "STS", "SYS", direction="outgoing")) == {"STS-1": False, "STS-2": False}
    assert _rows(coverage_report(docs, "SYS", "STS", direction="incoming", transitive=True)) == {"SYS-1": True, "SYS-2": False}
    assert _rows(coverage_report(docs, "SYS", "STS", direction="incoming")) == {"SYS-1": False, "SYS-2": False}


# --- code view findings (CV-CHECK-*) ---


@pytest.mark.parametrize("direction", ["outgoing", "incoming"])
def test_check_srs_107_108_each_object_counted_once(tmp_path, direction) -> None:
    """CV-CHECK-06, MELLON-CHECK-SRS-107 and -108: the counts are of the document's
    objects. C's outline names C-1 twice and leaves C-2 out; C-2 links to P-1. C has
    2 objects, 1 linking to P; P has 1 object, linked to by C."""
    p = tmp_path / "P"
    c = tmp_path / "C"
    _write_manifest(p, "P", ["P-1"])
    _write_obj(p, "P-1", "Parent requirement")
    _write_manifest(c, "C", ["C-1", "C-1"])
    _write_obj(c, "C-1", "Child one")
    _write_obj(c, "C-2", "Child two")
    _write_link(c, "C-2", "P", "P-1", "satisfies")
    docs = _docs_fixture([p, c])
    if direction == "outgoing":
        report = coverage_report(docs, "C", "P", direction="outgoing")
        assert (report.total, report.covered) == (2, 1)
        assert sorted(r.source_id for r in report.rows) == ["C-1", "C-2"]
    else:
        report = coverage_report(docs, "P", "C", direction="incoming")
        assert (report.total, report.covered) == (1, 1)
