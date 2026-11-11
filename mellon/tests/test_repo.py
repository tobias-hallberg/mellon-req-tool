from __future__ import annotations

import os
import sys
from pathlib import Path

import pygit2
import pytest

import mellon.mcp.server as mcp_server
from mellon import (
    LinkState,
    discover_documents,
    load_all,
    repo_incoming_links,
    scan_repo,
)
from mellon.loader import LoaderError, load_document
from mellon.repo import (
    DocLoadError,
    doc_folder_names,
    folders_fingerprint,
    parent_doc_ids,
)
from tests.test_suspects import (
    DOC_MANIFEST,
    DOC_OBJ,
    PARENT_MANIFEST,
    PARENT_OBJ_V1,
    _commit_all,
    _link_xml,
)
from tests.store_support import (  # MELLON-STORE-SRS blind tests
    CONFLICTED_DOC_1,
    CONFLICT_HINT,
    DECL,
    links_xml,
    load_with_errors,
    make_doc,
    manifest_xml,
    mcp_at,  # noqa: F401, F811 -- fixture
    object_xml,
    write,
)


@pytest.fixture
def two_doc_repo(tmp_path: Path):
    """Build a repo with PARENT + DOC, DOC-1 satisfies PARENT-42."""
    parent = tmp_path / "PARENT"
    (parent / "objects").mkdir(parents=True)
    (parent / "PARENT.xml").write_text(PARENT_MANIFEST, encoding="utf-8")
    (parent / "objects" / "PARENT-42.xml").write_text(PARENT_OBJ_V1, encoding="utf-8")

    doc = tmp_path / "DOC"
    (doc / "objects").mkdir(parents=True)
    (doc / "links").mkdir()
    (doc / "DOC.xml").write_text(DOC_MANIFEST, encoding="utf-8")
    (doc / "objects" / "DOC-1.xml").write_text(DOC_OBJ, encoding="utf-8")

    repo = pygit2.init_repository(str(tmp_path))
    (doc / "links" / "DOC-1.xml").write_text(_link_xml("0000000"), encoding="utf-8")
    c1 = _commit_all(repo, "init")
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    _commit_all(repo, "ack")
    return tmp_path, parent, doc, c1


def test_discover_documents(two_doc_repo):
    root, parent, doc, _c1 = two_doc_repo
    # Sprinkle some noise the discoverer should skip.
    (root / ".hidden").mkdir()
    (root / "not-a-doc").mkdir()
    (root / "not-a-doc" / "README.txt").write_text("hi", encoding="utf-8")

    found = discover_documents(root)
    assert sorted(p.name for p in found) == ["DOC", "PARENT"]


def test_load_all_returns_keyed_docs(two_doc_repo):
    root, _parent, _doc, _c1 = two_doc_repo
    docs = load_all(root)
    assert set(docs) == {"DOC", "PARENT"}
    assert docs["DOC"].document.id == "DOC"
    assert "DOC-1" in docs["DOC"].document.objects


def test_parent_doc_ids(two_doc_repo):
    root, _parent, _doc, _c1 = two_doc_repo
    docs = load_all(root)
    # DOC links up to PARENT (DOC-1 satisfies PARENT-42).
    assert parent_doc_ids(docs, "DOC") == {"PARENT"}
    # PARENT has no outgoing links — no parents.
    assert parent_doc_ids(docs, "PARENT") == set()
    # Unknown doc → empty set, no exception.
    assert parent_doc_ids(docs, "NOPE") == set()


def test_repo_incoming_links(two_doc_repo):
    root, _parent, _doc, _c1 = two_doc_repo
    docs = load_all(root)
    hits = repo_incoming_links(docs, "PARENT", "PARENT-42")
    assert len(hits) == 1
    src_doc, src_id, link = hits[0]
    assert (src_doc, src_id) == ("DOC", "DOC-1")
    assert link.type == "satisfies"

    assert repo_incoming_links(docs, "PARENT", "PARENT-99") == []


def test_scan_repo_covers_every_doc(two_doc_repo):
    root, _parent, _doc, _c1 = two_doc_repo
    result = scan_repo(root)
    assert set(result) == {"DOC", "PARENT"}
    assert result["PARENT"] == []  # no outgoing links
    assert len(result["DOC"]) == 1
    assert result["DOC"][0].state is LinkState.OK


def test_folders_fingerprint_stable(two_doc_repo):
    _root, parent, doc, _c1 = two_doc_repo
    assert folders_fingerprint([parent, doc]) == folders_fingerprint([parent, doc])


def test_folders_fingerprint_detects_edit(two_doc_repo):
    import os

    _root, parent, doc, _c1 = two_doc_repo
    before = folders_fingerprint([doc])
    target = doc / "objects" / "DOC-1.xml"
    # mtime-only change (same content/size) must be enough on its own.
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert folders_fingerprint([doc]) != before


def test_folders_fingerprint_detects_add_and_delete(two_doc_repo):
    _root, _parent, doc, _c1 = two_doc_repo
    before = folders_fingerprint([doc])
    extra = doc / "objects" / "DOC-2.xml"
    extra.write_text("<object id='DOC-2'/>", encoding="utf-8")
    with_add = folders_fingerprint([doc])
    assert with_add != before
    extra.unlink()
    # Same content/mtimes as before the add → back to the original.
    assert folders_fingerprint([doc]) == before


def test_folders_fingerprint_is_union_over_folders(two_doc_repo):
    _root, parent, doc, _c1 = two_doc_repo
    both = folders_fingerprint([parent, doc])
    assert folders_fingerprint([parent]) | folders_fingerprint([doc]) == both


def test_folders_fingerprint_skips_missing_folder(two_doc_repo):
    _root, parent, doc, _c1 = two_doc_repo
    ghost = doc.parent / "GHOST"
    assert folders_fingerprint([parent, ghost]) == folders_fingerprint([parent])


def test_doc_folder_names(two_doc_repo):
    root, _parent, _doc, _c1 = two_doc_repo
    (root / ".hidden").mkdir()
    (root / "not-a-doc").mkdir()
    (root / "not-a-doc" / "README.txt").write_text("hi", encoding="utf-8")
    assert doc_folder_names(root) == {"DOC", "PARENT"}


def test_doc_folder_names_missing_root_is_empty(tmp_path):
    assert doc_folder_names(tmp_path / "nope") == frozenset()


def test_discover_documents_missing_root_raises_loader_error(tmp_path):
    from mellon.loader import LoaderError

    with pytest.raises(LoaderError, match="cannot read repo root"):
        discover_documents(tmp_path / "nope")
    with pytest.raises(LoaderError, match="cannot read repo root"):
        load_all(tmp_path / "nope")


def _write_merge_conflict(path: Path) -> None:
    """Rewrite object file `path` the way a conflicted `git merge` leaves it."""
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<object id="{path.stem}">\n'
        "<<<<<<< HEAD\n"
        "  <description>ours</description>\n"
        "=======\n"
        "  <description>theirs</description>\n"
        ">>>>>>> feature\n"
        "</object>\n",
        encoding="utf-8",
    )


def test_discover_documents_keeps_a_document_that_fails_to_load(two_doc_repo):
    root, _parent, doc, _c1 = two_doc_repo
    _write_merge_conflict(doc / "objects" / "DOC-1.xml")
    assert [p.name for p in discover_documents(root)] == ["DOC", "PARENT"]


def test_load_all_parses_each_document_once(two_doc_repo, monkeypatch):
    import mellon.repo as repo_mod

    root, _parent, _doc, _c1 = two_doc_repo
    loaded: list[str] = []
    real_load = repo_mod.load_document

    def counting_load(folder):
        loaded.append(Path(folder).name)
        return real_load(folder)

    monkeypatch.setattr(repo_mod, "load_document", counting_load)
    load_all(root)
    assert sorted(loaded) == ["DOC", "PARENT"]


def test_load_all_records_merge_conflict_as_load_error(two_doc_repo):
    root, _parent, doc, _c1 = two_doc_repo
    bad = doc / "objects" / "DOC-1.xml"
    _write_merge_conflict(bad)

    errors: list[DocLoadError] = []
    docs = load_all(root, load_errors=errors)
    assert set(docs) == {"PARENT"}
    (err,) = errors
    assert (err.doc_id, err.folder, err.file, err.line) == ("DOC", doc, bad, 3)
    assert "XML parse error" in err.message
    assert err.message.endswith("(unresolved merge conflict?)")
    # Without a list the failed document is still left out, as before.
    assert set(load_all(root)) == {"PARENT"}


def test_load_all_records_semantic_load_error_without_hint(two_doc_repo):
    root, _parent, doc, _c1 = two_doc_repo
    bad = doc / "objects" / "DOC-1.xml"
    bad.write_text('<object id="DOC-2"/>\n', encoding="utf-8")

    errors: list[DocLoadError] = []
    load_all(root, load_errors=errors)
    (err,) = errors
    assert (err.doc_id, err.file, err.line) == ("DOC", bad, None)
    assert "does not match @id='DOC-2'" in err.message
    assert "merge conflict" not in err.message


def test_doc_load_error_is_exported_next_to_load_all():
    import mellon

    assert mellon.DocLoadError is DocLoadError
    assert "DocLoadError" in mellon.__all__


def test_load_all_records_unreadable_file_as_load_error(two_doc_repo):
    root, _parent, doc, _c1 = two_doc_repo
    # A path lxml can't read raises OSError; it used to abort the whole load.
    (doc / "objects" / "DOC-9.xml").mkdir()

    errors: list[DocLoadError] = []
    docs = load_all(root, load_errors=errors)
    assert set(docs) == {"PARENT"}
    (err,) = errors
    assert err.doc_id == "DOC"
    assert "cannot read file" in err.message


def test_scan_repo_detects_suspect_after_parent_revision(two_doc_repo):
    root, parent, _doc, _c1 = two_doc_repo
    target = parent / "objects" / "PARENT-42.xml"
    target.write_text(
        target.read_text(encoding="utf-8").replace("original", "revised"),
        encoding="utf-8",
    )
    _commit_all(pygit2.Repository(str(root)), "revise")

    result = scan_repo(root)
    assert result["PARENT"] == []
    (status,) = result["DOC"]
    assert status.state is LinkState.SUSPECT
    assert status.source_id == "DOC-1"
    assert status.link.target_doc == "PARENT"
    assert status.link.target_id == "PARENT-42"


def test_folders_fingerprint_keys_and_values(two_doc_repo):
    """Every file below the folder, with its posix path, modified time and size."""
    _root, parent, _doc, _c1 = two_doc_repo
    nested = parent / "figures" / "deep"
    nested.mkdir(parents=True)
    (nested / "x.svg").write_text("<svg/>", encoding="utf-8")
    expected = set()
    for f in parent.rglob("*"):
        if f.is_file():
            st = f.stat()
            expected.add(
                (f"{parent.name}/{f.relative_to(parent).as_posix()}", st.st_mtime_ns, st.st_size)
            )
    assert folders_fingerprint([parent]) == expected
    assert any(key.endswith("/figures/deep/x.svg") for key, _, _ in expected)


def test_folders_fingerprint_sees_a_same_size_replace(two_doc_repo):
    """A rewrite through a temp file, which is how the editor writes, must show
    in the directory listing's metadata."""
    _root, doc, _other, _c1 = two_doc_repo
    path = next((doc / "objects").glob("*.xml"))
    before = folders_fingerprint([doc])
    tmp = path.with_name("x.tmp")
    tmp.write_bytes(path.read_bytes())
    stamp = path.stat().st_mtime_ns + 5_000_000_000
    os.utime(tmp, ns=(stamp, stamp))
    os.replace(tmp, path)
    assert folders_fingerprint([doc]) != before


# MELLON-STORE-SRS blind tests


@pytest.fixture
def discovery_repo(tmp_path):
    root = tmp_path / "disc"
    make_doc(root, "DOC")
    write(root / ".archive" / ".archive.xml", manifest_xml(".archive"))
    write(root / "DOC2" / "doc2.xml", manifest_xml("DOC2"))
    write(root / "build" / "build.xml", DECL + '<project name="build">\n  <target name="all"/>\n</project>\n')
    return root


def test_store_srs_18_dot_folder_isnt_a_document(discovery_repo):
    """MELLON-STORE-SRS-18: a folder whose name starts with a dot isn't a
    document, even holding a valid manifest .archive/.archive.xml: it is neither
    loaded nor listed with a load error."""
    docs, errors = load_with_errors(discovery_repo)
    assert ".archive" not in docs and ".archive" not in errors, (list(docs), list(errors))


@pytest.mark.xfail(
    sys.platform == "win32", strict=True,
    reason="defect: MELLON-STORE-SRS-18: on Windows DOC2/doc2.xml makes DOC2 a document "
    "that loads; the manifest name's letter case isn't checked",
)
def test_store_srs_18_manifest_name_letter_case_counts(discovery_repo):
    """MELLON-STORE-SRS-18: a document's folder holds a file named <folder
    name>.xml, letter case included. DOC2/doc2.xml doesn't make DOC2 a document
    that loads, on a case-insensitive file system as on Linux."""
    docs, _ = load_with_errors(discovery_repo)
    assert "DOC2" not in docs, "DOC2 loaded from doc2.xml"


def test_store_srs_18_matching_non_document_is_listed_with_load_error(discovery_repo, mcp_at):
    """MELLON-STORE-SRS-18: a folder that matches but isn't a valid document,
    build/build.xml with another root element, is listed with a load error (by
    the library and by list_documents)."""
    docs, errors = load_with_errors(discovery_repo)
    assert "build" in errors and "build" not in docs, (list(docs), list(errors))
    mcp_at(discovery_repo)
    listed = {d["doc_id"]: d for d in mcp_server.list_documents()}
    assert "load_error" in listed.get("build", {}), listed
    assert ".archive" not in listed, listed


def test_store_srs_18_valid_document_beside_them_loads(discovery_repo):
    """MELLON-STORE-SRS-18: the valid document DOC beside these folders is a
    document and loads without a load error."""
    docs, errors = load_with_errors(discovery_repo)
    assert "DOC" in docs and "DOC" not in errors
    assert set(docs["DOC"].document.objects) == {"DOC-1", "DOC-2"}


def test_store_srs_25_document_with_manifest_only(tmp_path):
    """MELLON-STORE-SRS-25: Mellon shall read a document whose folder holds only
    the manifest: no objects, links, figures, views or filters folder and no
    rules file."""
    root = tmp_path / "r"
    write(root / "DOC" / "DOC.xml", manifest_xml("DOC"))
    assert [p.name for p in (root / "DOC").iterdir()] == ["DOC.xml"], "precondition"
    docs, errors = load_with_errors(root)
    assert errors == {}, errors
    doc = docs["DOC"].document
    assert doc.objects == {} and doc.links == {}
    assert doc.rules == () and doc.views == () and doc.filters == ()
    assert doc.outline == ()


@pytest.fixture
def conflict_repo(tmp_path):
    root = tmp_path / "c"
    for d in ("CTRL", "NOEND"):
        make_doc(root, d, n=1)
    write(root / "CTRL" / "objects" / "CTRL-1.xml",
          CONFLICTED_DOC_1.replace("DOC-1", "CTRL-1"))
    write(root / "NOEND" / "objects" / "NOEND-1.xml",
          DECL + '<object id="NOEND-1">\n  <attr name="type">Requirement</attr>\n'
          "  <description>No end tag</description>\n")
    docs, errors = load_with_errors(root)
    assert CONFLICT_HINT.search(errors["CTRL"].message), (
        "precondition: a file with all three markers is called a merge conflict"
    )
    return errors


def test_store_srs_30_missing_end_tag_isnt_called_a_merge_conflict(conflict_repo):
    """MELLON-STORE-SRS-30: the conflict note is for files holding conflict
    markers (definitions: all three lines). An object file without its end tag
    and no marker is a load error that doesn't say merge conflict."""
    err = conflict_repo["NOEND"]
    assert err.file is not None and err.file.name == "NOEND-1.xml", err
    assert not CONFLICT_HINT.search(err.message), err.message


def _assert_whole_document_refused(root: Path, doc_id: str, bad_name: str):
    docs, errors = load_with_errors(root)
    assert doc_id in errors, f"{doc_id} has no load error"
    assert errors[doc_id].file is not None and errors[doc_id].file.name == bad_name, errors[doc_id]
    assert doc_id not in docs, "the document was read in part"
    with pytest.raises(LoaderError):
        load_document(root / doc_id)


def test_store_srs_31_truncated_object_file_is_load_error(tmp_path):
    """MELLON-STORE-SRS-31: a data file that isn't well-formed XML is a load error
    for its document, and the parser doesn't recover: an object file cut in half
    inside an element fails, and no object of the document is read."""
    root = tmp_path / "t"
    folder = make_doc(root, n=2)
    full = object_xml("DOC-1", description="The system shall keep each requirement whole on disk. " * 6)
    cut = len(full) // 2
    start = full.index("<description>") + len("<description>")
    assert start < cut < full.index("</description>"), "precondition: cut inside an element"
    write(folder / "objects" / "DOC-1.xml", full[:cut])
    _assert_whole_document_refused(root, "DOC", "DOC-1.xml")


def test_store_srs_31_zero_filled_object_file_is_load_error(tmp_path):
    """MELLON-STORE-SRS-31: an object file of 512 zero bytes is a load error for
    its document, and no object of the document is read."""
    root = tmp_path / "z"
    folder = make_doc(root, n=2)
    write(folder / "objects" / "DOC-2.xml", b"\x00" * 512)
    _assert_whole_document_refused(root, "DOC", "DOC-2.xml")


def test_store_srs_85_duplicate_attr_name_is_load_error(tmp_path):
    """MELLON-STORE-SRS-85: if an object file holds two attr elements with the
    same name (status Draft and Approved), Mellon shall report a load error,
    naming the object file."""
    root = tmp_path / "r"
    folder = make_doc(root)
    write(folder / "objects" / "DOC-1.xml",
          object_xml("DOC-1", attrs=(("type", "Requirement"), ("status", "Draft"), ("status", "Approved"))))
    docs, errors = load_with_errors(root)
    assert "DOC" in errors and "DOC" not in docs
    assert errors["DOC"].file is not None and errors["DOC"].file.name == "DOC-1.xml", errors["DOC"]


@pytest.mark.parametrize(
    "table_xml",
    [
        '<columns><col/><col/></columns><row><cell colspan="3">wide</cell></row>',
        '<columns><col/><col/></columns><row><cell>a</cell><cell>b</cell><cell>c</cell></row>',
        '<columns><col/></columns><row><cell rowspan="3">tall</cell></row>',
        '<columns><col/><col/></columns><row><cell rowspan="2">a</cell><cell>b</cell></row>'
        '<row><cell>c</cell><cell>d</cell></row>',
    ],
    ids=["colspan-past-last-column", "too-many-cells", "rowspan-past-last-row", "covered-twice"],
)
def test_store_srs_113_table_that_doesnt_tile_is_load_error(tmp_path, table_xml):
    """MELLON-STORE-SRS-113: if a table's cells don't cover each grid position
    exactly once, or a cell spans past the last column or row, Mellon shall
    report a load error."""
    root = tmp_path / "r"
    folder = make_doc(root)
    write(folder / "objects" / "DOC-1.xml", object_xml("DOC-1", table=table_xml))
    docs, errors = load_with_errors(root)
    assert "DOC" in errors and "DOC" not in docs, "table that doesn't tile was read"


def test_store_srs_119_links_source_must_match_file_name(tmp_path):
    """MELLON-STORE-SRS-119: if a links file's name without .xml differs from its
    source attribute, Mellon shall report a load error. links/DOC-1.xml with
    source DOC-2 (both objects exist) is a load error naming the links file, so
    no link is read from either object."""
    root = tmp_path / "r"
    folder = make_doc(root, n=3)
    write(folder / "links" / "DOC-1.xml", links_xml("DOC-2", [("DOC", "DOC-3", "refines")]))
    docs, errors = load_with_errors(root)
    assert "DOC" in errors and "DOC" not in docs
    err = errors["DOC"]
    assert err.file is not None and err.file.parent.name == "links" and err.file.name == "DOC-1.xml", err
