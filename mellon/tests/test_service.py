"""DocumentService integration tests.

These exercise the full stack (lxml + filesystem) — no mocks. They lean on the
existing `mcp_repo` fixture (two-doc non-git repo) from conftest, and build a
tiny git-backed repo inline for the handful of git-dependent methods.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from mellon import editor
from mellon.editor import DeleteReport, EditorError
from mellon.repo import DocLoadError
from mellon.service import (
    DocumentService,
    ErrorCode,
    ServiceError,
)
from tests.test_repo import _write_merge_conflict


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def svc(mcp_repo: Path) -> DocumentService:
    """Service pointing at the two-doc non-git repo (PARENT + CHILD)."""
    return DocumentService(mcp_repo)


@pytest.fixture
def svc_cached(mcp_repo: Path) -> DocumentService:
    return DocumentService(mcp_repo, cache=True)


@pytest.fixture
def svc_readonly(mcp_repo: Path) -> DocumentService:
    return DocumentService(mcp_repo, read_only=True)


@pytest.fixture
def git_repo(mcp_repo: Path) -> Path:
    """Turn `mcp_repo` into a committed git repo so git-dependent methods work."""
    repo = pygit2.init_repository(str(mcp_repo), bare=False)
    index = repo.index
    for p in mcp_repo.rglob("*.xml"):
        index.add(str(p.relative_to(mcp_repo)).replace("\\", "/"))
    index.write()
    tree_oid = index.write_tree()
    sig = pygit2.Signature("tester", "t@x")
    repo.create_commit("HEAD", sig, sig, "init", tree_oid, [])
    return mcp_repo


# --- construction & properties -------------------------------------------


def test_construct_without_repo_root_ok():
    svc = DocumentService(None)
    assert svc.repo_root is None
    assert svc.read_only is False
    assert svc.cache is None


def test_methods_needing_repo_root_raise_validation():
    svc = DocumentService(None)
    with pytest.raises(ServiceError) as ei:
        svc.list_documents()
    assert ei.value.code is ErrorCode.VALIDATION


# --- read surface --------------------------------------------------------


def test_list_documents(svc: DocumentService):
    docs = svc.list_documents()
    ids = [d.document.id for d in docs]
    assert ids == ["CHILD", "PARENT"]


def test_get_document(svc: DocumentService):
    parent = svc.get_document("PARENT")
    assert parent.id == "PARENT"
    assert "PARENT-0" in parent.objects


def test_get_document_not_found(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.get_document("NOPE")
    assert ei.value.code is ErrorCode.NOT_FOUND


def test_get_all_documents(svc: DocumentService):
    docs = svc.get_all_documents()
    assert set(docs) == {"PARENT", "CHILD"}


def test_get_object(svc: DocumentService):
    obj = svc.get_object("PARENT", "PARENT-1")
    assert obj.id == "PARENT-1"


def test_get_object_not_found(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.get_object("PARENT", "MISSING")
    assert ei.value.code is ErrorCode.NOT_FOUND


def test_validate_document_clean(svc: DocumentService):
    assert svc.validate_document("PARENT") == []


def test_trace_down_across_docs(svc: DocumentService):
    # CHILD-0 links to PARENT-1, so tracing *down* from PARENT-1 reaches CHILD-0.
    edges = svc.trace_down("PARENT", "PARENT-1")
    assert len(edges) == 1
    e = edges[0]
    assert (e.target_doc, e.target_id, e.link_type) == ("CHILD", "CHILD-0", "satisfies")


def test_trace_up_across_docs(svc: DocumentService):
    edges = svc.trace_up("CHILD", "CHILD-0")
    assert len(edges) == 1
    assert edges[0].target_doc == "PARENT"
    assert edges[0].target_id == "PARENT-1"


def test_trace_down_unknown_object(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.trace_down("PARENT", "NOPE")
    assert ei.value.code is ErrorCode.NOT_FOUND


def test_incoming_links(svc: DocumentService):
    hits = svc.incoming_links("PARENT", "PARENT-1")
    assert len(hits) == 1
    src_doc, src_id, _link = hits[0]
    assert (src_doc, src_id) == ("CHILD", "CHILD-0")


def test_scan_suspects_without_git_raises_git_error(svc: DocumentService):
    # mcp_repo isn't a git repo; scan_document should surface as ErrorCode.GIT.
    with pytest.raises(ServiceError) as ei:
        svc.scan_suspects("CHILD")
    assert ei.value.code is ErrorCode.GIT


# --- git-backed reads ----------------------------------------------------


def test_git_head_and_log(git_repo: Path):
    svc = DocumentService(git_repo)
    branch, short = svc.git_head()
    assert short is not None
    log = svc.git_log("PARENT")
    assert len(log) >= 1
    assert "short_hash" in log[0]


def test_scan_suspects_with_git(git_repo: Path):
    svc = DocumentService(git_repo)
    out = svc.scan_suspects("CHILD")
    assert "CHILD" in out
    # CHILD-0 has an un-acked link -> SUSPECT
    assert any(s.state.value == "suspect" for s in out["CHILD"])


def test_parent_documents(svc: DocumentService, mcp_repo: Path):
    parents = svc.parent_documents("CHILD")
    assert list(parents) == ["PARENT"]
    assert parents["PARENT"].id == "PARENT"
    # A document without parents has none.
    assert svc.parent_documents("PARENT") == {}
    # A parent that fails to load is left out.
    (mcp_repo / "PARENT" / "PARENT.xml").write_text("<broken", encoding="utf-8")
    assert DocumentService(mcp_repo).parent_documents("CHILD") == {}


def test_cached_scan_rescans_after_commit_revises_parent(git_repo: Path):
    """The child's cached scan must not stay OK once the parent's revision
    is committed (the commit touches none of the child's files)."""
    svc = DocumentService(git_repo, cache=True)
    svc.ack_all("CHILD", svc.scan_suspects("CHILD")["CHILD"])
    assert [s.state.value for s in svc.scan_suspects("CHILD")["CHILD"]] == ["ok"]

    svc.set_description("PARENT", "PARENT-1", "Revised parent text.")
    assert [s.state.value for s in svc.scan_suspects("CHILD")["CHILD"]] == ["ok"]
    svc.commit_document("PARENT", "revise parent")

    assert [s.state.value for s in svc.scan_suspects("CHILD")["CHILD"]] == ["suspect"]


def test_cached_scan_hit_does_not_reopen_the_repo(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """The GUI reads the scan once per grid row, so a hit must stay a cheap
    lookup: no repo discovery per call."""
    from mellon.service import document as service_document

    svc = DocumentService(git_repo, cache=True)
    first = svc.scan_suspects("CHILD")
    calls: list[object] = []
    real = service_document.discover_repo
    monkeypatch.setattr(
        service_document,
        "discover_repo",
        lambda path: calls.append(path) or real(path),
    )

    assert svc.scan_suspects("CHILD") == first
    assert calls == []


# --- mutations -----------------------------------------------------------


def test_create_object_returns_refreshed(svc: DocumentService):
    result = svc.create_object("PARENT", description="new child")
    assert result.value.startswith("PARENT-")
    assert result.value in result.refreshed.objects
    assert result.affected_docs == ("PARENT",)


def test_set_attribute_happy(svc: DocumentService):
    result = svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    assert result.refreshed.objects["CHILD-0"].attrs["status"] == "Approved"


def test_set_attribute_unknown_enum_value_is_validation(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.set_attribute("CHILD", "CHILD-0", "status", "NotAnOption")
    assert ei.value.code is ErrorCode.VALIDATION


def test_set_attribute_bad_obj_is_not_found(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.set_attribute("CHILD", "CHILD-99", "status", "Draft")
    assert ei.value.code is ErrorCode.NOT_FOUND


def test_set_description(svc: DocumentService):
    r = svc.set_description("CHILD", "CHILD-0", "new text")
    assert r.refreshed.objects["CHILD-0"].description == "new text"


def test_set_metadata_unknown_field_is_validation(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.set_metadata("CHILD", "not_a_field", "x")
    assert ei.value.code is ErrorCode.VALIDATION


def test_set_metadata_rejects_issue_field(svc: DocumentService):
    """Issue moved to git tags — the manifest no longer carries it."""
    with pytest.raises(ServiceError) as ei:
        svc.set_metadata("CHILD", "issue", "2")
    assert ei.value.code is ErrorCode.VALIDATION


def test_add_parent_duplicate_is_already_exists(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.add_parent("CHILD", "PARENT")  # already declared in conftest
    assert ei.value.code is ErrorCode.ALREADY_EXISTS


def test_remove_parent_happy(svc: DocumentService):
    r = svc.remove_parent("CHILD", "PARENT")
    assert r.value is True
    assert "PARENT" not in r.refreshed.parents


def test_remove_parent_missing_is_noop(svc: DocumentService):
    r = svc.remove_parent("CHILD", "GHOST")
    assert r.value is False
    assert r.affected_docs == ()


def test_add_attribute_happy(svc: DocumentService):
    r = svc.add_attribute("CHILD", "priority", "enum", False, ("Must", "Should"))
    attr = r.refreshed.attribute("priority")
    assert attr is not None
    assert attr.options == ("Must", "Should")


def test_add_attribute_duplicate_is_already_exists(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.add_attribute("CHILD", "status", "text", False)
    assert ei.value.code is ErrorCode.ALREADY_EXISTS


def test_delete_attribute_happy(svc: DocumentService):
    r = svc.delete_attribute("CHILD", "status")
    assert r.refreshed.attribute("status") is None
    # CHILD-0 had status=Draft in conftest — that <attr> is now stripped.
    assert "status" not in r.refreshed.objects["CHILD-0"].attrs


def test_remove_attr_option_happy(svc: DocumentService):
    # CHILD-0 has type=Requirement in conftest; after removing the option,
    # the value is blanked rather than removed.
    r = svc.remove_attr_option("CHILD", "type", "Requirement")
    attr = r.refreshed.attribute("type")
    assert "Requirement" not in attr.options
    assert r.refreshed.objects["CHILD-0"].attrs.get("type") == ""


def test_add_link_duplicate_is_already_exists(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.add_link("CHILD", "CHILD-0", "PARENT", "PARENT-1", "satisfies")
    assert ei.value.code is ErrorCode.ALREADY_EXISTS


def test_add_link_happy(svc: DocumentService):
    r = svc.add_link("CHILD", "CHILD-0", "PARENT", "PARENT-0", "satisfies")
    links = r.refreshed.links["CHILD-0"]
    assert any(l.target_id == "PARENT-0" for l in links)


def test_remove_link_happy(svc: DocumentService):
    r = svc.remove_link("CHILD", "CHILD-0", "PARENT", "PARENT-1")
    assert r.value is True
    assert "CHILD-0" not in r.refreshed.links


# --- composite writes: link pre-validation ---------------------------------

_LINK = {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"}


@pytest.mark.parametrize(
    "links",
    [
        [_LINK, _LINK],
        [dict(_LINK, target_id="PARENT-99")],
        [dict(_LINK, type="sat\x00isfies")],
        [dict(_LINK, type=5)],
    ],
    ids=["duplicate-in-request", "missing-target", "control-char-type", "non-string-type"],
)
def test_create_object_full_rejects_bad_links_before_write(
    svc: DocumentService, mcp_repo: Path, links: list[dict]
):
    before = sorted((mcp_repo / "CHILD" / "objects").iterdir())
    with pytest.raises(ServiceError) as ei:
        svc.create_object_full("CHILD", description="x", links=links)
    assert ei.value.code is ErrorCode.VALIDATION
    assert "partial" not in ei.value.details
    assert sorted((mcp_repo / "CHILD" / "objects").iterdir()) == before


def test_update_object_rejects_existing_link_before_write(
    svc: DocumentService, mcp_repo: Path
):
    # CHILD-0 already links to PARENT-1 (conftest).
    obj_file = mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    before = obj_file.read_text(encoding="utf-8")
    with pytest.raises(ServiceError) as ei:
        svc.update_object("CHILD", "CHILD-0", description="changed", add_links=[_LINK])
    assert ei.value.code is ErrorCode.VALIDATION
    assert obj_file.read_text(encoding="utf-8") == before


def test_composite_rejects_control_char_attr_before_write(
    svc: DocumentService, mcp_repo: Path
):
    svc.add_attribute("CHILD", "rationale", "text", required=False)
    objects = mcp_repo / "CHILD" / "objects"
    before = {p: p.read_text(encoding="utf-8") for p in objects.iterdir()}
    attrs = {"rationale": "be\x00cause"}
    with pytest.raises(ServiceError) as ei:
        svc.create_object_full("CHILD", description="x", attrs=attrs)
    assert ei.value.code is ErrorCode.VALIDATION
    assert "partial" not in ei.value.details
    with pytest.raises(ServiceError) as ei:
        svc.update_object("CHILD", "CHILD-0", description="changed", attrs=attrs)
    assert ei.value.code is ErrorCode.VALIDATION
    assert "partial" not in ei.value.details
    assert {p: p.read_text(encoding="utf-8") for p in objects.iterdir()} == before


@pytest.mark.parametrize(
    "exc, code",
    [(EditorError("boom"), ErrorCode.VALIDATION), (OSError("disk full"), ErrorCode.IO)],
    ids=["editor-error", "os-error"],
)
def test_composite_mid_batch_failure_codes(
    svc: DocumentService,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    code: ErrorCode,
):
    def _fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(editor, "add_link", _fail)
    link = dict(_LINK, target_id="PARENT-0")
    with pytest.raises(ServiceError) as ei:
        svc.create_object_full("CHILD", links=[link])
    assert ei.value.code is code
    assert "partial" in ei.value.details
    with pytest.raises(ServiceError) as ei:
        svc.update_object("CHILD", "CHILD-0", add_links=[link])
    assert ei.value.code is code
    assert "partial" in ei.value.details


def test_move_object_happy(svc: DocumentService):
    # Create a new top-level, then move it under PARENT-0.
    created = svc.create_object("PARENT", description="sib")
    new_id = created.value
    r = svc.move_object("PARENT", new_id, parent_id="PARENT-0")
    outline_ids = r.refreshed.iter_outline_ids()
    # new_id must now come somewhere after its parent PARENT-0.
    assert outline_ids.index("PARENT-0") < outline_ids.index(new_id)


def test_delete_object_blocked_without_force(svc: DocumentService):
    # PARENT-1 has an incoming link from CHILD-0. Soft block when force=False.
    result = svc.delete_object("PARENT", "PARENT-1", force=False)
    assert isinstance(result.report, DeleteReport)
    assert result.report.performed is False
    assert result.affected_docs == ()
    # Blockers reported:
    assert result.report.incoming


def test_delete_object_force_cascades_across_docs(svc: DocumentService):
    result = svc.delete_object("PARENT", "PARENT-1", force=True)
    assert result.report.performed is True
    assert set(result.affected_docs) == {"PARENT", "CHILD"}
    # Both docs got refreshed:
    assert set(result.refreshed_docs) == {"PARENT", "CHILD"}
    # PARENT-1 gone; CHILD-0's link to it gone too.
    assert "PARENT-1" not in result.refreshed_docs["PARENT"].objects
    assert "CHILD-0" not in result.refreshed_docs["CHILD"].links


def test_control_char_in_description_is_validation(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.set_description("CHILD", "CHILD-0", "bad\x00text")
    assert ei.value.code is ErrorCode.VALIDATION
    assert "U+0000" in ei.value.message


def test_lxml_value_error_is_validation(svc: DocumentService):
    # Parent doc ids don't go through the text sanitiser; lxml's own
    # ValueError must still come back as a ServiceError, not escape raw.
    with pytest.raises(ServiceError) as ei:
        svc.add_parent("CHILD", "BAD\x01")
    assert ei.value.code is ErrorCode.VALIDATION


def test_os_error_during_write_is_io(svc: DocumentService, monkeypatch):
    def fail_write(tree, path):
        raise PermissionError(f"{path} is locked by another process")

    monkeypatch.setattr("mellon.editor._write_tree", fail_write)
    with pytest.raises(ServiceError) as ei:
        svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    assert ei.value.code is ErrorCode.IO


def test_os_error_during_ack_all_is_io(git_repo: Path, monkeypatch):
    svc = DocumentService(git_repo)
    statuses = svc.scan_suspects("CHILD")["CHILD"]

    def fail_write(tree, path):
        raise PermissionError(f"{path} is locked by another process")

    # suspects.write_ack writes through editor._write_tree.
    monkeypatch.setattr("mellon.editor._write_tree", fail_write)
    with pytest.raises(ServiceError) as ei:
        svc.ack_all("CHILD", statuses)
    assert ei.value.code is ErrorCode.IO


def test_doc_id_cannot_reach_a_document_outside_the_repo(tmp_path: Path):
    from mellon.editor import create_document

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "X").mkdir()
    create_document(tmp_path / "X", "X")  # tmp_path/X/X/X.xml, outside repo
    manifest = tmp_path / "X" / "X" / "X.xml"
    before = manifest.read_bytes()
    svc = DocumentService(repo)
    with pytest.raises(ServiceError) as ei:
        svc.set_metadata("../X/X", "title", "escaped")
    assert ei.value.code is ErrorCode.NOT_FOUND
    assert manifest.read_bytes() == before


# --- read-only -----------------------------------------------------------


def test_read_only_blocks_mutations(svc_readonly: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc_readonly.create_object("PARENT", description="x")
    assert ei.value.code is ErrorCode.READ_ONLY


def test_read_only_allows_reads(svc_readonly: DocumentService):
    assert svc_readonly.get_document("PARENT").id == "PARENT"


# --- cache ---------------------------------------------------------------


def test_cache_populated_on_first_read(svc_cached: DocumentService):
    assert svc_cached.cache is not None
    assert svc_cached.cache.get("PARENT") is None
    svc_cached.get_document("PARENT")
    assert svc_cached.cache.get("PARENT") is not None


def test_cache_invalidated_on_mutation(svc_cached: DocumentService):
    svc_cached.get_document("CHILD")  # warm
    assert svc_cached.cache.get("CHILD") is not None
    svc_cached.set_description("CHILD", "CHILD-0", "changed")
    # After mutation the service reloads, so the cache entry reflects new state.
    doc = svc_cached.cache.get("CHILD")
    assert doc is not None
    assert doc.document.objects["CHILD-0"].description == "changed"


def test_cache_cross_doc_invalidation_on_force_delete(svc_cached: DocumentService):
    svc_cached.get_all_documents()  # warm both
    assert svc_cached.cache.get("PARENT") is not None
    assert svc_cached.cache.get("CHILD") is not None
    svc_cached.delete_object("PARENT", "PARENT-1", force=True)
    # Both docs must now reflect the cascade.
    parent = svc_cached.cache.get("PARENT")
    child = svc_cached.cache.get("CHILD")
    assert parent is not None and "PARENT-1" not in parent.document.objects
    assert child is not None and "CHILD-0" not in child.document.links


# --- load errors ---------------------------------------------------------


def test_get_all_documents_reports_load_errors(svc: DocumentService, mcp_repo: Path):
    _write_merge_conflict(mcp_repo / "CHILD" / "objects" / "CHILD-0.xml")
    errors: list[DocLoadError] = []
    docs = svc.get_all_documents(load_errors=errors)
    assert set(docs) == {"PARENT"}
    assert [e.doc_id for e in errors] == ["CHILD"]


def test_cache_keeps_load_errors_until_invalidate_all(
    svc_cached: DocumentService, mcp_repo: Path
):
    bad = mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    good = bad.read_bytes()
    _write_merge_conflict(bad)
    svc_cached.list_documents()  # warm the cache with CHILD failing

    bad.write_bytes(good)
    errors: list[DocLoadError] = []
    listed = svc_cached.list_documents(load_errors=errors)
    # Served from the cached snapshot: still without CHILD, and saying why.
    assert [d.document.id for d in listed] == ["PARENT"]
    assert [e.doc_id for e in errors] == ["CHILD"]

    # A single-document load that now succeeds clears CHILD's stale error.
    svc_cached.get_document("CHILD")
    errors.clear()
    assert set(svc_cached.get_all_documents(load_errors=errors)) == {"CHILD", "PARENT"}
    assert errors == []

    svc_cached.cache.invalidate_all()
    errors.clear()
    assert set(svc_cached.get_all_documents(load_errors=errors)) == {"CHILD", "PARENT"}
    assert errors == []


# --- document commit -----------------------------------------------------


def test_prepare_document_commit_clean_tree(git_repo: Path):
    svc = DocumentService(git_repo)
    preview = svc.prepare_document_commit("CHILD")
    assert preview.doc_id == "CHILD"
    assert preview.paths == ()
    assert preview.affected_req_ids == ()
    assert preview.default_message == "Edited CHILD. No affected requirements."
    assert preview.dirty_parents == ()


def test_prepare_document_commit_with_object_edit(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.paths == ("CHILD/objects/CHILD-0.xml",)
    assert preview.affected_req_ids == ("CHILD-0",)
    assert preview.default_message == (
        "Edited CHILD. Affected requirements: CHILD-0."
    )
    assert preview.dirty_parents == ()


def test_prepare_document_commit_figure_only_edit_is_affected(
    git_repo: Path, tmp_path: Path
):
    svg = tmp_path / "fig.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    svc = DocumentService(git_repo)
    svc.set_figure("CHILD", "CHILD-0", svg)
    # draw.io's backup beside the figure is neither committed nor counted.
    (git_repo / "CHILD" / "figures" / ".$CHILD-0.svg.bkp").write_text("x")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.paths == ("CHILD/figures/CHILD-0.svg",)
    assert preview.affected_req_ids == ("CHILD-0",)


def test_prepare_document_commit_manifest_only_has_no_affected(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_metadata("CHILD", "title", "Renamed")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.paths == ("CHILD/CHILD.xml",)
    assert preview.affected_req_ids == ()
    assert preview.default_message == "Edited CHILD. No affected requirements."


def test_prepare_document_commit_includes_suspect_links(git_repo: Path):
    # CHILD-0 -> PARENT/PARENT-1 has no @ack in the conftest fixture, so
    # it's SUSPECT immediately after the initial commit.
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    preview = svc.prepare_document_commit("CHILD")
    assert len(preview.suspect_links) == 1
    s = preview.suspect_links[0]
    assert s.source_id == "CHILD-0"
    assert s.link.target_doc == "PARENT"
    assert s.link.target_id == "PARENT-1"
    # Either SUSPECT (never reviewed) or BROKEN (target not in history yet);
    # both states constitute a warning.
    assert s.state.value in ("suspect", "broken")


def test_prepare_document_commit_no_suspect_links_when_acked(git_repo: Path):
    svc = DocumentService(git_repo)
    # Ack CHILD-0's outgoing link, then commit so it lands in HEAD.
    statuses = svc.scan_suspects("CHILD")["CHILD"]
    ackable = [s for s in statuses if s.current_hash is not None]
    if ackable:
        svc.ack_all("CHILD", ackable)
        svc.commit_document("CHILD", "ack")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.suspect_links == ()


def test_prepare_document_commit_prefills_metadata(git_repo: Path):
    repo = pygit2.Repository(str(git_repo))
    # Seed an issue tag at HEAD so current_issue reads a known value.
    from mellon.git_ops import create_issue_tag
    create_issue_tag(repo, "CHILD", "1", repo.head.target)

    svc = DocumentService(git_repo)
    preview = svc.prepare_document_commit("CHILD")
    # current_* for author/date come from the manifest; issue from the tag.
    assert preview.current_issue == "1"
    assert preview.current_author == "tester"
    assert preview.current_date == "2026-04-22"
    # Nothing is bumped: a plain commit keeps the issue (so cuts no tag) and
    # the manifest's date (an auto-written date is a merge hot spot).
    assert preview.prefill_issue == "1"
    assert preview.prefill_author == "tester"
    assert preview.prefill_date == "2026-04-22"


def test_prepare_document_commit_prefill_issue_dotted(git_repo: Path):
    from mellon.git_ops import create_issue_tag

    repo = pygit2.Repository(str(git_repo))
    create_issue_tag(repo, "CHILD", "12.23.54", repo.head.target)
    svc = DocumentService(git_repo)
    preview = svc.prepare_document_commit("CHILD")
    assert preview.current_issue == "12.23.54"
    assert preview.prefill_issue == "12.23.54"


def test_prepare_document_commit_prefill_issue_empty_when_no_tag(
    git_repo: Path,
):
    """No issue tag => empty current and empty prefill (no tag unless the
    user types an issue)."""
    svc = DocumentService(git_repo)
    preview = svc.prepare_document_commit("CHILD")
    assert preview.current_issue == ""
    assert preview.prefill_issue == ""


def test_commit_with_prefilled_issue_cuts_no_tag(git_repo: Path):
    """Committing what the dialog prefills is a plain commit, tag or not."""
    from mellon.git_ops import create_issue_tag

    svc = DocumentService(git_repo)
    repo = pygit2.Repository(str(git_repo))
    tags_before = [r for r in repo.references if r.startswith("refs/tags/")]
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    preview = svc.prepare_document_commit("CHILD")
    result = svc.commit_document("CHILD", "edit", new_issue=preview.prefill_issue)
    assert (result.tag_name, result.tag_error) == (None, None)
    assert [r for r in repo.references if r.startswith("refs/tags/")] == tags_before

    create_issue_tag(repo, "CHILD", "3", repo.head.target)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Draft")
    preview = svc.prepare_document_commit("CHILD")
    result = svc.commit_document("CHILD", "edit", new_issue=preview.prefill_issue)
    assert (result.tag_name, result.tag_error) == (None, None)
    # Bumping the issue on purpose still tags the commit.
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document("CHILD", "release", new_issue="4")
    assert result.tag_name == "CHILD-issue-4"


def test_prepare_document_commit_prefill_author_falls_back_to_git_user(
    git_repo: Path,
):
    """If the manifest author is blank, fall back to repo's user.name."""
    repo = pygit2.Repository(str(git_repo))
    repo.config["user.name"] = "git-fallback-user"
    svc = DocumentService(git_repo)
    svc.set_metadata("CHILD", "author", "")
    svc.commit_document("CHILD", "clear author")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.current_author == ""
    assert preview.prefill_author == "git-fallback-user"


def test_commit_document_creates_issue_tag(git_repo: Path):
    """commit_document(new_issue=...) creates the matching tag at the new HEAD."""
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document("CHILD", "edit", new_issue="2")
    assert result.tag_name == "CHILD-issue-2"
    assert result.tag_error is None
    repo = pygit2.Repository(str(git_repo))
    tag_oid = repo.references["refs/tags/CHILD-issue-2"].target
    assert tag_oid == repo.head.target


def test_commit_document_skips_tag_when_issue_unchanged(git_repo: Path):
    """If new_issue equals current_issue, no tag is created."""
    from mellon.git_ops import create_issue_tag

    repo = pygit2.Repository(str(git_repo))
    create_issue_tag(repo, "CHILD", "1", repo.head.target)
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document("CHILD", "edit", new_issue="1")
    assert result.tag_name is None
    assert result.tag_error is None


def test_commit_document_reports_tag_conflict(git_repo: Path):
    """Tag-creation failure leaves the commit in place and surfaces tag_error.

    Engineered scenario: an old `CHILD-issue-2` tag points at an *unreachable*
    commit (we made a commit, tagged it, then reset HEAD back). When the user
    commits and asks for issue "2", `current_issue` reads "" (tag isn't
    reachable from HEAD), so the service attempts to create the tag — and
    fails on the existing name.
    """
    from mellon.git_ops import create_issue_tag

    repo = pygit2.Repository(str(git_repo))
    initial_oid = repo.head.target

    svc = DocumentService(git_repo)
    # First commit: will be abandoned via reset.
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    svc.commit_document("CHILD", "abandoned commit")
    abandoned_oid = repo.head.target
    create_issue_tag(repo, "CHILD", "2", abandoned_oid)

    # Reset back so the tag is no longer reachable from HEAD.
    repo.reset(initial_oid, pygit2.GIT_RESET_HARD)

    # Fresh commit + same issue value — name conflict.
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document("CHILD", "real commit", new_issue="2")
    assert result.tag_name is None
    assert result.tag_error is not None
    assert "already exists" in result.tag_error
    # Commit still landed.
    assert svc.has_uncommitted_changes("CHILD") is False


def test_tag_issue_creates_tag_at_head(git_repo: Path):
    svc = DocumentService(git_repo)
    name = svc.tag_issue("CHILD", "5")
    assert name == "CHILD-issue-5"
    repo = pygit2.Repository(str(git_repo))
    assert "refs/tags/CHILD-issue-5" in repo.references


def test_tag_issue_validation_error_for_bad_format(git_repo: Path):
    svc = DocumentService(git_repo)
    with pytest.raises(ServiceError) as ei:
        svc.tag_issue("CHILD", "draft")
    assert ei.value.code is ErrorCode.VALIDATION


def test_tag_issue_git_error_on_duplicate(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.tag_issue("CHILD", "5")
    with pytest.raises(ServiceError) as ei:
        svc.tag_issue("CHILD", "5")
    assert ei.value.code is ErrorCode.GIT


def test_prepare_document_commit_warns_about_dirty_parent(git_repo: Path):
    svc = DocumentService(git_repo)
    # Edit both docs; CHILD links up to PARENT, so PARENT is a parent doc.
    svc.set_attribute("PARENT", "PARENT-1", "status", "Draft")
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.dirty_parents == ("PARENT",)
    # Files list still scoped to CHILD only.
    assert all(p.startswith("CHILD/") for p in preview.paths)


# --- commit with a tag ------------------------------------------------------


def test_commit_document_creates_the_tag(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document(
        "CHILD", "edit", tag="CDR-1.0", tag_message="Critical design review"
    )
    assert (result.repo_tag, result.repo_tag_error) == ("CDR-1.0", None)
    assert (result.tag_name, result.tag_error) == (None, None)
    repo = pygit2.Repository(str(git_repo))
    tag = repo.revparse_single("refs/tags/CDR-1.0")
    assert tag.target == repo.head.target
    assert tag.message.strip() == "Critical design review"


def test_commit_document_cuts_issue_and_tag_together(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document("CHILD", "release", new_issue="1", tag="PDR")
    assert (result.tag_name, result.repo_tag) == ("CHILD-issue-1", "PDR")
    repo = pygit2.Repository(str(git_repo))
    for ref in ("refs/tags/CHILD-issue-1", "refs/tags/PDR"):
        assert repo.references[ref].peel(pygit2.Commit).id == repo.head.target


def test_commit_document_reports_a_tag_failure_and_keeps_the_commit(git_repo: Path):
    svc = DocumentService(git_repo)
    repo = pygit2.Repository(str(git_repo))
    repo.references.create("refs/tags/CDR-1.0", repo.head.target)
    head_before = repo.head.target
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    result = svc.commit_document("CHILD", "edit", tag="CDR-1.0")
    assert result.repo_tag is None
    assert "already exists" in result.repo_tag_error
    assert repo.head.target != head_before
    assert repo.references["refs/tags/CDR-1.0"].target == head_before
    assert svc.has_uncommitted_changes("CHILD") is False


def test_commit_document_without_a_tag_creates_none(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    for tag in (None, ""):
        result = svc.commit_document("CHILD", "edit", tag=tag)
        assert (result.repo_tag, result.repo_tag_error) == (None, None)
        svc.set_attribute("CHILD", "CHILD-0", "status", "Draft")
    repo = pygit2.Repository(str(git_repo))
    assert not [r for r in repo.references if r.startswith("refs/tags/")]


def test_tag_head_tags_the_current_commit(git_repo: Path):
    svc = DocumentService(git_repo)
    repo = pygit2.Repository(str(git_repo))
    head = repo.head.target
    assert svc.tag_head("CHILD", "CDR-1.0", "Critical design review") == "CDR-1.0"
    tag = repo.revparse_single("refs/tags/CDR-1.0")
    assert tag.target == head and repo.head.target == head
    assert tag.message.strip() == "Critical design review"


def test_tag_head_refuses_a_bad_or_taken_name(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.tag_head("CHILD", "PDR")
    for name in ("PDR", "bad name", "CHILD-issue-1"):
        with pytest.raises(ServiceError) as ei:
            svc.tag_head("CHILD", name)
        assert ei.value.code is ErrorCode.VALIDATION


def test_tag_head_refused_read_only(git_repo: Path):
    with pytest.raises(ServiceError):
        DocumentService(git_repo, read_only=True).tag_head("CHILD", "PDR")


def test_prepare_document_commit_lists_existing_tags(git_repo: Path):
    from mellon.git_ops import create_issue_tag

    repo = pygit2.Repository(str(git_repo))
    create_issue_tag(repo, "CHILD", "1", repo.head.target)
    repo.references.create("refs/tags/PDR", repo.head.target)
    preview = DocumentService(git_repo).prepare_document_commit("CHILD")
    assert preview.existing_tags == ("CHILD-issue-1", "PDR")


def _ack_and_commit(svc: DocumentService, root: Path, doc_ids) -> None:
    from mellon.git_ops import commit

    for doc_id in doc_ids:
        statuses = svc.scan_suspects(doc_id)[doc_id]
        svc.ack_all(doc_id, [s for s in statuses if s.current_hash is not None])
    commit(root, "ack")


_CHAIN_SCHEMA = [
    {"name": "type", "kind": "enum", "required": True,
     "options": ["Heading", "Information", "Note", "Requirement"]},
    {"name": "status", "kind": "enum", "required": True,
     "options": ["Draft", "Approved"]},
]


def _fill_required(svc: DocumentService, doc_id: str, obj_id: str) -> None:
    svc.set_attribute(doc_id, obj_id, "type", "Requirement")
    svc.set_attribute(doc_id, obj_id, "status", "Draft")


@pytest.fixture
def chain_repo(git_repo: Path) -> Path:
    """`git_repo` grown to a chain CHILD -> PARENT -> GRAND, plus SIB, which
    links up to PARENT beside CHILD. Committed, every link acked."""
    from mellon.git_ops import commit

    svc = DocumentService(git_repo)
    svc.create_document("GRAND", title="Grandparent", attr_schema=_CHAIN_SCHEMA)
    grand_id = svc.create_object("GRAND", description="The system shall grand.").value
    _fill_required(svc, "GRAND", grand_id)
    svc.add_parent("PARENT", "GRAND")
    svc.add_link("PARENT", "PARENT-1", "GRAND", grand_id, "satisfies")
    svc.create_document(
        "SIB", title="Sibling", parents=["PARENT"], attr_schema=_CHAIN_SCHEMA
    )
    sib_id = svc.create_object("SIB", description="The system shall sib.").value
    _fill_required(svc, "SIB", sib_id)
    svc.add_link("SIB", sib_id, "PARENT", "PARENT-1", "satisfies")
    commit(git_repo, "chain")
    _ack_and_commit(svc, git_repo, ("CHILD", "PARENT", "SIB"))
    return git_repo


def test_upstream_warnings_empty_for_a_clean_chain(chain_repo: Path):
    preview = DocumentService(chain_repo).prepare_document_commit("CHILD")
    assert preview.upstream_warnings == ()


def test_upstream_warnings_reach_up_the_chain_only(chain_repo: Path):
    """The grandparent is checked; the sibling and CHILD itself are not."""
    svc = DocumentService(chain_repo)
    for doc_id, obj_id in (("GRAND", "GRAND-0"), ("SIB", "SIB-0"), ("CHILD", "CHILD-0")):
        svc.set_attribute(doc_id, obj_id, "status", "Approved")
    preview = svc.prepare_document_commit("CHILD")
    assert preview.upstream_warnings == ("GRAND — uncommitted changes",)
    # From PARENT, CHILD and SIB are below: nothing to report there either.
    assert svc.prepare_document_commit("PARENT").upstream_warnings == (
        "GRAND — uncommitted changes",
    )


def test_upstream_warnings_report_suspect_links_and_validation_errors(
    chain_repo: Path,
):
    from mellon.git_ops import commit

    svc = DocumentService(chain_repo)
    # Revising the grandparent's object makes PARENT's link to it suspect.
    svc.set_description("GRAND", "GRAND-0", "The system shall grand, revised.")
    commit(chain_repo, "revise GRAND")
    # A validation error in PARENT: a status outside the schema, by hand.
    obj = chain_repo / "PARENT" / "objects" / "PARENT-1.xml"
    obj.write_text(
        obj.read_text(encoding="utf-8").replace(">Approved<", ">Bogus<"),
        encoding="utf-8",
    )
    preview = svc.prepare_document_commit("CHILD")
    assert preview.upstream_warnings == (
        "PARENT — uncommitted changes",
        "PARENT — 1 suspect or broken link(s)",
        "PARENT — 1 validation error(s)",
    )


def test_upstream_warnings_survive_a_link_cycle(chain_repo: Path):
    """GRAND linking back down to CHILD closes a cycle: the walk ends, and
    CHILD isn't reported as its own upstream."""
    svc = DocumentService(chain_repo)
    svc.add_parent("GRAND", "CHILD")
    svc.add_link("GRAND", "GRAND-0", "CHILD", "CHILD-0", "satisfies")
    preview = svc.prepare_document_commit("CHILD")
    assert all(not w.startswith("CHILD") for w in preview.upstream_warnings)
    assert "GRAND — uncommitted changes" in preview.upstream_warnings


def test_upstream_warnings_name_a_document_that_does_not_load(chain_repo: Path):
    (chain_repo / "GRAND" / "GRAND.xml").write_text("<document", encoding="utf-8")
    preview = DocumentService(chain_repo).prepare_document_commit("CHILD")
    assert "GRAND — does not load" in preview.upstream_warnings


def test_commit_document_only_stages_doc_files(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    svc.set_attribute("PARENT", "PARENT-1", "status", "Draft")

    short_hash = svc.commit_document(
        "CHILD", "Edited CHILD. Affected requirements: CHILD-0."
    ).short_hash
    assert len(short_hash) == 7

    # PARENT edits remain dirty in the working tree.
    repo = pygit2.Repository(str(git_repo))
    statuses = repo.status()
    assert any(
        path.startswith("PARENT/") and flags
        for path, flags in statuses.items()
    ), "expected PARENT changes to remain uncommitted"
    # CHILD edits, however, are gone.
    assert not any(
        path.startswith("CHILD/") and flags
        for path, flags in statuses.items()
    )


# --- discard uncommitted changes -----------------------------------------


def test_discard_document_changes_restores_the_folder(git_repo: Path):
    svc = DocumentService(git_repo, cache=True)
    child = git_repo / "CHILD"
    repo = pygit2.Repository(str(git_repo))
    head_tree = repo.head.peel(pygit2.Commit).tree
    head_bytes = {
        p: repo[head_tree[f"CHILD/{p}"].id].data
        for p in ("CHILD.xml", "objects/CHILD-0.xml", "links/CHILD-0.xml")
    }
    svc.get_all_documents()  # warm the cache

    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")        # modified
    repo.index.add("CHILD/objects/CHILD-0.xml")                         # ...and staged
    repo.index.write()
    new_id = svc.create_object("CHILD", description="added").value      # untracked
    (child / "links" / "CHILD-0.xml").unlink()                          # deleted
    svc.set_attribute("PARENT", "PARENT-1", "status", "Draft")          # other doc
    parent_obj = (git_repo / "PARENT" / "objects" / "PARENT-1.xml").read_bytes()

    changed = svc.discard_document_changes("CHILD")

    assert changed == (
        "CHILD/CHILD.xml",
        "CHILD/links/CHILD-0.xml",
        "CHILD/objects/CHILD-0.xml",
        f"CHILD/objects/{new_id}.xml",
    )
    for rel, data in head_bytes.items():
        assert (child / rel).read_bytes() == data
    assert not (child / "objects" / f"{new_id}.xml").exists()
    assert svc.has_uncommitted_changes("CHILD") is False
    status = pygit2.Repository(str(git_repo)).status()
    assert not [p for p in status if p.startswith("CHILD/")]
    # The other document's edit is untouched.
    assert svc.has_uncommitted_changes("PARENT") is True
    assert (git_repo / "PARENT" / "objects" / "PARENT-1.xml").read_bytes() == parent_obj
    # The cache serves the restored document, in the repo-wide map too.
    doc = svc.get_all_documents()["CHILD"].document
    assert doc.objects["CHILD-0"].attrs["status"] == "Draft"
    assert new_id not in doc.objects
    assert "CHILD-0" in doc.links


def test_discard_document_changes_refreshes_other_documents_suspect_scans(
    git_repo: Path,
):
    # CHILD-0 links to PARENT-1. A scan checks the target file on disk, so
    # restoring it changes CHILD's scan at the same HEAD.
    svc = DocumentService(git_repo, cache=True)
    target = git_repo / "PARENT" / "objects" / "PARENT-1.xml"
    target.unlink()  # removed outside mellon
    assert [s.state.value for s in svc.scan_suspects("CHILD")["CHILD"]] == ["broken"]

    assert svc.discard_document_changes("PARENT") == ("PARENT/objects/PARENT-1.xml",)

    assert target.is_file()
    assert [s.state.value for s in svc.scan_suspects("CHILD")["CHILD"]] == ["suspect"]


def test_discard_document_changes_refuses_a_never_committed_document(
    git_repo: Path,
):
    svc = DocumentService(git_repo)
    svc.create_document("NEW", title="Not committed yet")
    with pytest.raises(ServiceError) as ei:
        svc.discard_document_changes("NEW")
    assert ei.value.code is ErrorCode.GIT
    assert "nothing to restore" in ei.value.message
    assert (git_repo / "NEW" / "NEW.xml").is_file()


def test_discard_document_changes_refuses_during_merge(git_repo: Path):
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    repo = pygit2.Repository(str(git_repo))
    (Path(repo.path) / "MERGE_HEAD").write_text(f"{repo.head.target}\n")
    with pytest.raises(ServiceError) as ei:
        svc.discard_document_changes("CHILD")
    assert ei.value.code is ErrorCode.GIT
    assert "finish the merge" in ei.value.message
    assert svc.has_uncommitted_changes("CHILD") is True


def test_discard_document_changes_outside_git_is_git_error(svc: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc.discard_document_changes("CHILD")
    assert ei.value.code is ErrorCode.GIT


def test_discard_document_changes_read_only(svc_readonly: DocumentService):
    with pytest.raises(ServiceError) as ei:
        svc_readonly.discard_document_changes("CHILD")
    assert ei.value.code is ErrorCode.READ_ONLY


def test_commit_without_paths_reads_status_once(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """All document folders come from one status scan, and files outside
    them stay out of the commit."""
    from mellon.service import document as service_document

    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    svc.set_attribute("PARENT", "PARENT-1", "status", "Draft")
    (git_repo / "notes.txt").write_text("not a document\n", encoding="utf-8")
    calls: list[str] = []
    real = service_document.dirty_paths_under
    monkeypatch.setattr(
        service_document,
        "dirty_paths_under",
        lambda repo, rel: calls.append(rel) or real(repo, rel),
    )

    result = svc.commit("Edited both documents.")

    assert calls == [""]
    assert sorted(p.as_posix() for p in result.paths) == [
        "CHILD/objects/CHILD-0.xml",
        "PARENT/objects/PARENT-1.xml",
    ]
    assert "notes.txt" not in pygit2.Repository(str(git_repo)).head.peel(
        pygit2.Commit
    ).tree


def test_commit_without_paths_commits_a_deleted_document_folder(git_repo: Path):
    """A document folder deleted on disk is no longer a document in the work
    tree, but its deletions must still be committed (HEAD has it)."""
    import shutil

    svc = DocumentService(git_repo)
    shutil.rmtree(git_repo / "PARENT")

    result = svc.commit("Removed PARENT.")

    assert sorted(p.as_posix() for p in result.paths) == [
        "PARENT/PARENT.xml",
        "PARENT/objects/PARENT-0.xml",
        "PARENT/objects/PARENT-1.xml",
    ]
    repo = pygit2.Repository(str(git_repo))
    assert "PARENT" not in repo.head.peel(pygit2.Commit).tree
    assert repo.status() == {}


def test_commit_without_paths_commits_a_renamed_document_folder(git_repo: Path):
    import re

    svc = DocumentService(git_repo)
    (git_repo / "PARENT").rename(git_repo / "SUPER")
    (git_repo / "SUPER" / "PARENT.xml").rename(git_repo / "SUPER" / "SUPER.xml")

    svc.commit("Renamed PARENT to SUPER.")

    repo = pygit2.Repository(str(git_repo))
    tree = repo.head.peel(pygit2.Commit).tree
    assert "PARENT" not in tree
    assert "SUPER/SUPER.xml" in tree
    assert not [p for p in repo.status() if re.match(r"(PARENT|SUPER)/", p)]


def test_commit_document_includes_files_staged_elsewhere(git_repo: Path):
    """A new object staged by another git client (`git add`), then one more
    mellon edit: the commit must hold both, not half a document."""
    svc = DocumentService(git_repo)
    new_id = svc.create_object("CHILD", description="added").value
    repo = pygit2.Repository(str(git_repo))
    repo.index.add(f"CHILD/objects/{new_id}.xml")
    repo.index.add("CHILD/CHILD.xml")
    repo.index.write()
    assert svc.has_uncommitted_changes("CHILD") is True
    assert f"CHILD/objects/{new_id}.xml" in svc.prepare_document_commit("CHILD").paths
    svc.set_metadata("CHILD", "author", "someone else")

    result = svc.commit_document("CHILD", "Added an object.")

    assert f"CHILD/objects/{new_id}.xml" in {p.as_posix() for p in result.paths}
    repo = pygit2.Repository(str(git_repo))
    assert f"CHILD/objects/{new_id}.xml" in repo.head.peel(pygit2.Commit).tree
    assert not [p for p in repo.status() if p.startswith("CHILD/")]


def test_commit_document_with_the_index_locked_commits_nothing(git_repo: Path):
    """A stale index.lock: the commit is refused before HEAD moves, so the
    caller's "commit failed" (and its rollback) is true."""
    svc = DocumentService(git_repo)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    repo = pygit2.Repository(str(git_repo))
    head = repo.head.target
    lock = Path(repo.path) / "index.lock"
    lock.write_bytes(b"")

    with pytest.raises(ServiceError) as ei:
        svc.commit_document("CHILD", "blocked", new_issue="2")

    assert ei.value.code is ErrorCode.GIT
    assert "index is locked" in ei.value.message
    assert pygit2.Repository(str(git_repo)).head.target == head
    lock.unlink()
    result = svc.commit_document("CHILD", "retry", new_issue="2")
    assert result.tag_name == "CHILD-issue-2"


def test_commit_document_clean_tree_raises_validation(git_repo: Path):
    svc = DocumentService(git_repo)
    with pytest.raises(ServiceError) as ei:
        svc.commit_document("CHILD", "no-op")
    assert ei.value.code is ErrorCode.VALIDATION


def test_commit_document_outside_git_raises_git(svc: DocumentService):
    # mcp_repo isn't a git repo at all.
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    with pytest.raises(ServiceError) as ei:
        svc.commit_document("CHILD", "no git")
    assert ei.value.code is ErrorCode.GIT


def test_has_uncommitted_changes(git_repo: Path):
    svc = DocumentService(git_repo)
    assert svc.has_uncommitted_changes("CHILD") is False
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    assert svc.has_uncommitted_changes("CHILD") is True
    assert svc.has_uncommitted_changes("PARENT") is False


def test_load_error_wrapped_as_load(tmp_path: Path):
    bad = tmp_path / "BROKEN"
    bad.mkdir()
    (bad / "BROKEN.xml").write_text("not xml at all", encoding="utf-8")
    svc = DocumentService(tmp_path)
    with pytest.raises(ServiceError) as ei:
        svc.get_document("BROKEN")
    assert ei.value.code is ErrorCode.LOAD


# --- change listeners + undo ------------------------------------------------


def _record(svc: DocumentService) -> list:
    steps: list = []
    svc.add_change_listener(lambda doc_id, label, changes: steps.append(
        (doc_id, label, changes)
    ))
    return steps


def _files(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_change_listener_gets_one_step_per_write_call(
    svc_cached: DocumentService,
):
    steps = _record(svc_cached)
    svc_cached.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    # A composite call is one step, however many files and editor calls.
    svc_cached.update_object(
        "CHILD", "CHILD-0", description="Changed.",
        remove_links=[{"target_doc": "PARENT", "target_id": "PARENT-1"}],
    )
    assert [(d, label) for d, label, _ in steps] == [
        ("CHILD", "set attribute"), ("CHILD", "update object")
    ]
    assert sorted(f"{p.parent.name}/{p.name}" for p in steps[1][2].paths) == [
        "links/CHILD-0.xml", "objects/CHILD-0.xml"
    ]


def test_change_listener_quiet_on_failure_and_no_op(svc_cached: DocumentService):
    steps = _record(svc_cached)
    with pytest.raises(ServiceError):
        svc_cached.set_attribute("CHILD", "CHILD-0", "status", "NotAnOption")
    svc_cached.remove_parent("CHILD", "NOT-A-PARENT")  # nothing to remove
    assert steps == []


def test_undo_and_redo_a_delete_across_documents(
    svc_cached: DocumentService, mcp_repo: Path
):
    """Deleting PARENT-1 strips CHILD-0's link to it; undo brings both back
    and the cache shows it."""
    steps = _record(svc_cached)
    before = _files(mcp_repo)
    svc_cached.delete_object("PARENT", "PARENT-1", force=True)
    after = _files(mcp_repo)
    [(_doc, label, changes)] = steps
    assert label == "delete object"

    assert svc_cached.undo_changes(changes) == ("CHILD", "PARENT")
    assert _files(mcp_repo) == before
    assert "PARENT-1" in svc_cached.get_document("PARENT").objects
    assert svc_cached.get_document("CHILD").links["CHILD-0"]

    assert svc_cached.redo_changes(changes) == ("CHILD", "PARENT")
    assert _files(mcp_repo) == after
    assert "PARENT-1" not in svc_cached.get_document("PARENT").objects
    assert len(steps) == 1  # undo/redo aren't steps themselves


def test_undo_refuses_after_another_change(svc_cached: DocumentService, mcp_repo: Path):
    steps = _record(svc_cached)
    svc_cached.set_description("CHILD", "CHILD-0", "Mine.")
    svc_cached.set_description("CHILD", "CHILD-0", "Theirs.")
    snap = _files(mcp_repo)
    with pytest.raises(ServiceError) as ei:
        svc_cached.undo_changes(steps[0][2])
    assert ei.value.code is ErrorCode.CONFLICT
    assert _files(mcp_repo) == snap


def test_undo_refused_read_only(svc_readonly: DocumentService, mcp_repo: Path):
    from mellon.editor import journal

    with journal(rollback=False) as changes:
        pass
    with pytest.raises(ServiceError) as ei:
        svc_readonly.undo_changes(changes)
    assert ei.value.code is ErrorCode.READ_ONLY
