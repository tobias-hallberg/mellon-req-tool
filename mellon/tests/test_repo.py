from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from mellon import (
    LinkState,
    discover_documents,
    load_all,
    repo_incoming_links,
    scan_repo,
)
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
