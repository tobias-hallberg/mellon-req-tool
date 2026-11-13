"""Tests for the document commit dialog and the controller's commit flow.

The dialog half is a pure widget test — built from a synthetic
`DocCommitPreview` so no repo is needed. The controller half exercises
`RepoController.commit_document` against a small inline git repo so we
verify the end-to-end behaviour: only the current document's files are
committed, sibling docs stay dirty, and the `committed` signal fires.
"""

from __future__ import annotations

import os
from pathlib import Path

import pygit2
import pytest

from tests.test_git_ops import no_git_identity  # noqa: F401 — fixture
from tests.store_support import (  # MELLON-STORE-SRS blind tests
    CONFLICT_HINT,
    CONFLICT_LINE,
    PartialWrites,
    _break_doc_1,
    _shows_load_error,
    git,
    git_commit_all,
    git_init,
    make_doc,
    write,
)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# --- dialog ----------------------------------------------------------------


_VALID_META = dict(
    current_issue="1",
    current_author="alice",
    current_date="2026-04-21",
    prefill_issue="2",
    prefill_author="alice",
    prefill_date="2026-04-26",
)


def test_commit_dialog_pre_fills_default_message(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="DOC",
        paths=("DOC/objects/DOC-1.xml", "DOC/links/DOC-1.xml"),
        affected_req_ids=("DOC-1",),
        default_message="Edited DOC. Affected requirements: DOC-1.",
        dirty_parents=(),
        **_VALID_META,
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)

    assert dlg.message() == "Edited DOC. Affected requirements: DOC-1."
    # OK button enabled because message + paths both present.
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()
    assert ok.text() == "Commit"
    # Both paths shown in the file list.
    assert dlg._files.count() == 2


def test_commit_dialog_disables_ok_when_message_empty(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="DOC",
        paths=("DOC/DOC.xml",),
        affected_req_ids=(),
        default_message="Edited DOC. No affected requirements.",
        dirty_parents=(),
        **_VALID_META,
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)

    dlg._message.setPlainText("")
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()


def test_commit_dialog_shows_suspect_warning(qtbot):
    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.schema import Link
    from mellon.service import DocCommitPreview
    from mellon.suspects import LinkState, LinkStatus

    suspect = LinkStatus(
        source_id="CHILD-0",
        link=Link(
            target_doc="PARENT",
            target_id="PARENT-1",
            type="satisfies",
            ack=None,
        ),
        state=LinkState.SUSPECT,
        current_hash="abc1234",
        reason="never reviewed",
    )
    preview = DocCommitPreview(
        doc_id="CHILD",
        paths=("CHILD/objects/CHILD-0.xml",),
        affected_req_ids=("CHILD-0",),
        default_message="Edited CHILD. Affected requirements: CHILD-0.",
        dirty_parents=(),
        suspect_links=(suspect,),
        **_VALID_META,
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)

    assert dlg._suspect_list is not None
    assert dlg._suspect_list.count() == 1
    text = dlg._suspect_list.item(0).text()
    assert "CHILD-0" in text
    assert "PARENT/PARENT-1" in text
    assert "suspect" in text


def test_commit_dialog_shows_parent_warning(qtbot):
    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="CHILD",
        paths=("CHILD/objects/CHILD-0.xml",),
        affected_req_ids=("CHILD-0",),
        default_message="Edited CHILD. Affected requirements: CHILD-0.",
        dirty_parents=("PARENT",),
        **_VALID_META,
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)

    assert dlg._warning_list is not None
    assert dlg._warning_list.count() == 1
    assert dlg._warning_list.item(0).text() == "PARENT"


def test_commit_dialog_metadata_prefilled(qtbot):
    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="DOC",
        paths=("DOC/objects/DOC-1.xml",),
        affected_req_ids=("DOC-1",),
        default_message="Edited DOC. Affected requirements: DOC-1.",
        dirty_parents=(),
        current_issue="1",
        current_author="alice",
        current_date="2026-04-21",
        prefill_issue="2",
        prefill_author="alice",
        prefill_date="2026-04-26",
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)

    assert dlg.issue() == "2"
    assert dlg.author() == "alice"
    assert dlg.date() == "2026-04-26"
    # 1 → 2 is a raised issue: the hint names the tag the commit will get.
    assert "DOC-issue-2" in dlg._issue_hint.text()


def _metadata_preview(**overrides):
    from mellon.service import DocCommitPreview

    fields = dict(
        doc_id="DOC",
        paths=("DOC/objects/DOC-1.xml",),
        affected_req_ids=("DOC-1",),
        default_message="msg",
        dirty_parents=(),
        current_issue="1",
        current_author="alice",
        current_date="2026-04-21",
        prefill_issue="1",
        prefill_author="alice",
        prefill_date="2026-04-21",
    )
    fields.update(overrides)
    return DocCommitPreview(**fields)


def test_commit_dialog_allows_empty_issue_for_untagged_document(qtbot):
    """A document without an issue tag gets an empty prefill; that is a plain
    commit, not an error."""
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview(current_issue="", prefill_issue=""))
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert dlg.issue() == ""
    assert ok.isEnabled()
    assert dlg._meta_error.text() == ""
    assert "no issue yet" in dlg._issue_hint.text()
    assert "DOC-issue-<n>" in dlg._issue_edit.toolTip()

    # Typing one cuts the first issue; a non-numeric one is still refused.
    dlg._issue_edit.setText("1")
    assert ok.isEnabled()
    assert "DOC-issue-1" in dlg._issue_hint.text()
    dlg._issue_edit.setText("v1")
    assert not ok.isEnabled()
    assert "dot-separated numeric" in dlg._meta_error.text()


def test_commit_dialog_unchanged_issue_is_a_plain_commit(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview(paths=()))
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert dlg.issue() == "1"
    assert "change it to cut a new issue" in dlg._issue_hint.text()
    # Nothing to commit: neither the current issue nor a cleared field is a
    # metadata change (both mean "no tag").
    assert not ok.isEnabled()
    dlg._issue_edit.setText("")
    assert not ok.isEnabled()
    assert dlg._meta_error.text() == ""


def test_commit_dialog_allows_empty_manifest_date(qtbot):
    """The manifest's own date is prefilled (no auto-today); a manifest
    without one must not block the commit."""
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview(current_date="", prefill_date=""))
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert dlg.date() == ""
    assert ok.isEnabled()
    dlg._date_edit.setText("21/04/2026")
    assert not ok.isEnabled()
    assert "YYYY-MM-DD" in dlg._meta_error.text()


def test_commit_dialog_refuses_clearing_an_existing_date(qtbot):
    """Only a manifest without a date may commit an empty one; clearing a
    date the manifest has would commit `<date></date>`."""
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview())
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()

    dlg._date_edit.setText("")
    assert not ok.isEnabled()
    assert "YYYY-MM-DD" in dlg._meta_error.text()


def test_commit_dialog_blocks_ok_on_lower_issue(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="DOC",
        paths=("DOC/objects/DOC-1.xml",),
        affected_req_ids=("DOC-1",),
        default_message="msg",
        dirty_parents=(),
        current_issue="3",
        current_author="alice",
        current_date="2026-04-21",
        prefill_issue="3",
        prefill_author="alice",
        prefill_date="2026-04-21",
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()
    dlg._issue_edit.setText("2")
    assert not ok.isEnabled()
    assert "higher than the current issue 3" in dlg._meta_error.text()
    dlg._issue_edit.setText("4")
    assert ok.isEnabled()


def test_commit_dialog_blocks_ok_on_invalid_issue(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="DOC",
        paths=("DOC/objects/DOC-1.xml",),
        affected_req_ids=("DOC-1",),
        default_message="msg",
        dirty_parents=(),
        current_issue="1",
        current_author="alice",
        current_date="2026-04-21",
        prefill_issue="2",
        prefill_author="alice",
        prefill_date="2026-04-26",
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()

    dlg._issue_edit.setText("draft")
    assert not ok.isEnabled()
    assert "dot-separated numeric" in dlg._meta_error.text()


def test_commit_dialog_allows_metadata_only_commit(qtbot):
    """With no dirty paths, OK enables once author or date differs: the
    caller writes those to the manifest. A new issue alone is only a tag,
    which needs a commit to sit on, so it does not enable OK."""
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.service import DocCommitPreview

    preview = DocCommitPreview(
        doc_id="DOC",
        paths=(),
        affected_req_ids=(),
        default_message="Edited DOC. No affected requirements.",
        dirty_parents=(),
        current_issue="1",
        current_author="alice",
        current_date="2026-04-21",
        # Prefill matches current → no metadata change initially.
        prefill_issue="1",
        prefill_author="alice",
        prefill_date="2026-04-21",
    )
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    # No dirty paths and no metadata change yet → OK disabled.
    assert not ok.isEnabled()

    # Bumping the issue writes nothing to disk: still nothing to commit.
    dlg._issue_edit.setText("2")
    assert not ok.isEnabled()
    assert "nothing to commit" in dlg._issue_hint.text()

    # An author change is written to the manifest → OK enables, and the
    # raised issue now tags that commit.
    dlg._author_edit.setText("bob")
    assert ok.isEnabled()
    assert "tags the commit DOC-issue-2" in dlg._issue_hint.text()


def test_commit_dialog_tag_is_optional_and_checked_as_typed(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview(existing_tags=("DOC-issue-1", "PDR")))
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert dlg.tag() == "" and ok.isEnabled()

    for bad, reason in (
        ("CDR 1.0", "not a valid tag name"),
        ("PDR", "already exists"),
        ("DOC-issue-2", "form of an issue tag"),
    ):
        dlg._tag_edit.setText(bad)
        assert not ok.isEnabled()
        assert reason in dlg._meta_error.text()

    dlg._tag_edit.setText("  CDR-1.0 ")
    dlg._tag_message_edit.setText("Critical design review")
    assert ok.isEnabled() and dlg._meta_error.text() == ""
    assert (dlg.tag(), dlg.tag_message()) == ("CDR-1.0", "Critical design review")


def test_commit_dialog_with_nothing_to_commit_tags_the_current_commit(qtbot):
    """A tag alone on a clean document: the button becomes Tag, the commit
    message is out of play, and a raised issue (which needs a commit of its
    own) blocks it. A metadata change makes it a commit again."""
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview(paths=()))
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled() and ok.text() == "Commit"
    assert "tags the current commit" in dlg._tag_hint.text()
    assert dlg._files.item(0).text() == "(no file changes)"

    dlg._tag_edit.setText("CDR-1.0")
    assert dlg.tag_only()
    assert ok.isEnabled() and ok.text() == "Tag"
    assert not dlg._message.isEnabled()

    dlg._issue_edit.setText("2")
    assert not ok.isEnabled()
    dlg._issue_edit.setText("1")
    dlg._tag_edit.setText("bad name")
    assert not ok.isEnabled()
    dlg._tag_edit.setText("CDR-1.0")
    assert ok.isEnabled()

    dlg._author_edit.setText("bob")
    assert not dlg.tag_only()
    assert ok.isEnabled() and ok.text() == "Commit"
    assert dlg._message.isEnabled()
    assert dlg._tag_hint.text() == "Tags every document at this commit."


def test_commit_dialog_shows_upstream_points_only_with_a_tag(qtbot):
    """The open points up the link chain show while a tag is entered, and
    the direct-parent warning steps aside meanwhile. Commit stays enabled."""
    from PySide6.QtWidgets import QDialogButtonBox

    from mellon.gui.dialogs.commit_dialog import CommitDialog

    warnings = ("GRAND — 1 validation error(s)", "PARENT — uncommitted changes")
    dlg = CommitDialog(
        _metadata_preview(dirty_parents=("PARENT",), upstream_warnings=warnings)
    )
    qtbot.addWidget(dlg)
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert [
        dlg._upstream_list.item(i).text() for i in range(dlg._upstream_list.count())
    ] == list(warnings)

    assert dlg._upstream_label.isHidden() and dlg._upstream_list.isHidden()
    assert not dlg._warning_label.isHidden()

    dlg._tag_edit.setText("CDR-1.0")
    assert not dlg._upstream_label.isHidden() and not dlg._upstream_list.isHidden()
    assert dlg._warning_label.isHidden() and dlg._warning_list.isHidden()
    assert ok.isEnabled()

    dlg._tag_edit.setText("")
    assert dlg._upstream_label.isHidden()
    assert not dlg._warning_label.isHidden()


def test_commit_dialog_without_upstream_points_has_no_block(qtbot):
    from mellon.gui.dialogs.commit_dialog import CommitDialog

    dlg = CommitDialog(_metadata_preview())
    qtbot.addWidget(dlg)
    dlg._tag_edit.setText("CDR-1.0")
    assert dlg._upstream_label is None and dlg._upstream_list is None


# --- controller commit flow -----------------------------------------------


@pytest.fixture
def git_doc_repo(tmp_path: Path, mcp_repo) -> Path:
    """Two-doc git repo seeded with one commit (PARENT + CHILD)."""
    repo = pygit2.init_repository(str(mcp_repo), bare=False)
    index = repo.index
    for p in mcp_repo.rglob("*.xml"):
        index.add(str(p.relative_to(mcp_repo)).replace("\\", "/"))
    index.write()
    tree_oid = index.write_tree()
    sig = pygit2.Signature("tester", "t@x")
    repo.create_commit("HEAD", sig, sig, "init", tree_oid, [])
    return mcp_repo


def test_controller_commit_document_emits_signal(qtbot, git_doc_repo: Path):
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")

    captured: list[tuple[str, str]] = []
    controller.committed.connect(
        lambda doc_id, short: captured.append((doc_id, short))
    )

    result = controller.commit_document(
        "CHILD", "Edited CHILD. Affected requirements: CHILD-0."
    )
    assert len(result.short_hash) == 7
    assert captured == [("CHILD", result.short_hash)]

    # Working tree is clean for CHILD now.
    assert svc.has_uncommitted_changes("CHILD") is False


def test_controller_commit_document_leaves_other_docs_alone(
    qtbot, git_doc_repo: Path
):
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    svc.set_attribute("PARENT", "PARENT-1", "status", "Draft")
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")

    controller.commit_document(
        "CHILD", "Edited CHILD. Affected requirements: CHILD-0."
    )
    # CHILD now clean, PARENT still dirty.
    assert svc.has_uncommitted_changes("CHILD") is False
    assert svc.has_uncommitted_changes("PARENT") is True


# --- window commit flow (dialog metadata) ---------------------------------


def _accepted_commit_dialog(author: str, date: str):
    """Stand-in for `CommitDialog` that the user accepts with these values."""
    from PySide6.QtWidgets import QDialog

    class _Accepted:
        def __init__(self, preview, parent=None):
            self._preview = preview

        def exec(self):
            return QDialog.DialogCode.Accepted

        def message(self):
            return "Edited CHILD."

        def author(self):
            return author

        def date(self):
            return date

        def issue(self):
            return self._preview.current_issue

        def tag(self):
            return ""

        def tag_message(self):
            return ""

        def tag_only(self):
            return False

    return _Accepted


@pytest.fixture
def commit_window(qtbot, git_doc_repo: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service import DocumentService

    w = DocumentEditorWindow("CHILD", DocumentService(git_doc_repo, cache=True))
    w._set_read_only(False)
    qtbot.addWidget(w)
    return w


def test_failed_commit_leaves_no_metadata_edit_behind(
    commit_window, git_doc_repo: Path, monkeypatch
):
    from mellon.gui import main_window as mw
    from mellon.service import ErrorCode, ServiceError

    w = commit_window
    w.controller.require_service().set_attribute("CHILD", "CHILD-0", "status", "Approved")
    manifest = git_doc_repo / "CHILD" / "CHILD.xml"
    before = manifest.read_bytes()
    monkeypatch.setattr(mw, "CommitDialog", _accepted_commit_dialog("bob", "2026-05-01"))

    def fail(*_args, **_kwargs):
        raise ServiceError(ErrorCode.GIT, "index.lock exists")

    monkeypatch.setattr(w.controller, "commit_document", fail)
    boxes = _capture_critical(monkeypatch)
    w._commit_current_doc()

    assert "Commit failed" in w._status.currentMessage()
    assert boxes == ["index.lock exists"]
    assert manifest.read_bytes() == before
    assert w.controller.document("CHILD").metadata.author == "tester"


def _capture_critical(monkeypatch) -> list[str]:
    from PySide6.QtWidgets import QMessageBox

    texts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda _parent, _title, text, *a, **k: texts.append(text) or 0),
    )
    return texts


@pytest.mark.usefixtures("no_git_identity")
def test_missing_git_identity_is_shown_in_full(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """The refusal carries the `git config` commands to run; a 6-second
    status-bar message can't show them, so it goes to a dialog."""
    from mellon.gui import main_window as mw

    w = commit_window
    w.controller.require_service().set_attribute("CHILD", "CHILD-0", "status", "Approved")
    monkeypatch.setattr(mw, "CommitDialog", _accepted_commit_dialog("bob", "2026-05-01"))
    boxes = _capture_critical(monkeypatch)

    w._commit_current_doc()

    assert len(boxes) == 1
    assert "git config --global user.name" in boxes[0]
    assert "git config --global user.email" in boxes[0]
    assert w.controller.require_service().has_uncommitted_changes("CHILD")


def test_unwrapped_git_error_still_rolls_back_metadata(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """Another git client holding index.lock surfaces as a raw
    pygit2.GitError, not a ServiceError; the manifest is rolled back anyway."""
    from mellon.gui import main_window as mw

    w = commit_window
    w.controller.require_service().set_attribute("CHILD", "CHILD-0", "status", "Approved")
    manifest = git_doc_repo / "CHILD" / "CHILD.xml"
    before = manifest.read_bytes()
    monkeypatch.setattr(mw, "CommitDialog", _accepted_commit_dialog("bob", "2026-05-01"))

    def fail(*_args, **_kwargs):
        raise pygit2.GitError("failed to create locked file 'index.lock'")

    monkeypatch.setattr(w.controller, "commit_document", fail)
    with pytest.raises(pygit2.GitError):  # left for the excepthook to report
        w._commit_current_doc()

    assert manifest.read_bytes() == before
    assert w.controller.document("CHILD").metadata.author == "tester"


def test_error_after_the_commit_landed_keeps_the_committed_manifest(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """When HEAD moved, the commit holds the dialog's metadata: putting the
    old manifest back would revert it with the next commit."""
    from mellon.gui import main_window as mw
    from mellon.service import ErrorCode, ServiceError

    w = commit_window
    svc = w.controller.require_service()
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    manifest = git_doc_repo / "CHILD" / "CHILD.xml"
    monkeypatch.setattr(mw, "CommitDialog", _accepted_commit_dialog("bob", "2026-05-01"))

    def land_then_fail(doc_id, message, **_kwargs):
        svc.commit_document(doc_id, message)
        raise ServiceError(ErrorCode.GIT, "failed after the commit")

    monkeypatch.setattr(w.controller, "commit_document", land_then_fail)
    _capture_critical(monkeypatch)
    w._commit_current_doc()

    repo = pygit2.Repository(str(git_doc_repo))
    committed = repo.revparse_single("HEAD:CHILD/CHILD.xml").data
    assert b"<author>bob</author>" in committed
    assert manifest.read_bytes().replace(b"\r\n", b"\n") == committed.replace(
        b"\r\n", b"\n"
    )


def test_commit_includes_dialog_metadata(commit_window, git_doc_repo: Path, monkeypatch):
    from mellon.gui import main_window as mw

    w = commit_window
    # Metadata-only commit: the author/date edit is the whole change, so it
    # has to be written before the commit, not after.
    monkeypatch.setattr(mw, "CommitDialog", _accepted_commit_dialog("bob", "2026-05-01"))
    w._commit_current_doc()

    repo = pygit2.Repository(str(git_doc_repo))
    committed = repo.revparse_single("HEAD:CHILD/CHILD.xml").data.decode("utf-8")
    assert "<author>bob</author>" in committed
    assert w.controller.require_service().has_uncommitted_changes("CHILD") is False


def _real_commit_dialog(
    monkeypatch,
    issue: str | None = None,
    tag: str | None = None,
    tag_message: str = "",
) -> list:
    """Show the real `CommitDialog`, prefilled by the service, and accept it
    after typing `issue` and `tag` / `tag_message` (if given). Returns the
    dialogs shown."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox

    from mellon.gui import main_window as mw

    shown: list = []

    class _Dialog(mw.CommitDialog):
        def exec(self):
            shown.append(self)
            if issue is not None:
                self._issue_edit.setText(issue)
            if tag is not None:
                self._tag_edit.setText(tag)
                self._tag_message_edit.setText(tag_message)
            ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.ok_enabled = ok.isEnabled()
            if not self.ok_enabled:
                return QDialog.DialogCode.Rejected
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw, "CommitDialog", _Dialog)
    return shown


def _tags(repo: pygit2.Repository) -> list[str]:
    return sorted(
        r[len("refs/tags/"):] for r in repo.references if r.startswith("refs/tags/")
    )


@pytest.mark.parametrize("existing_issue", [None, "1"])
def test_default_commit_cuts_no_issue_tag(
    commit_window, git_doc_repo: Path, monkeypatch, existing_issue
):
    """Accepting the dialog as prefilled is a plain commit: no new issue tag,
    with or without an issue already, and the manifest date stays as it is."""
    w = commit_window
    svc = w.controller.require_service()
    if existing_issue is not None:
        svc.tag_issue("CHILD", existing_issue)
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    repo = pygit2.Repository(str(git_doc_repo))
    tags_before = _tags(repo)
    shown = _real_commit_dialog(monkeypatch)

    w._commit_current_doc()

    assert len(shown) == 1 and shown[0].ok_enabled, shown[0]._meta_error.text()
    assert shown[0].issue() == (existing_issue or "")
    assert svc.has_uncommitted_changes("CHILD") is False
    assert repo.head.peel(pygit2.Commit).message.startswith("Edited CHILD")
    assert _tags(repo) == tags_before
    manifest = repo.revparse_single("HEAD:CHILD/CHILD.xml").data.decode("utf-8")
    assert "<date>2026-04-22</date>" in manifest
    assert "tag" not in w._status.currentMessage()


def test_raising_the_issue_cuts_a_new_issue_tag(
    commit_window, git_doc_repo: Path, monkeypatch
):
    w = commit_window
    svc = w.controller.require_service()
    svc.tag_issue("CHILD", "1")
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    _real_commit_dialog(monkeypatch, issue="2")

    w._commit_current_doc()

    repo = pygit2.Repository(str(git_doc_repo))
    assert _tags(repo) == ["CHILD-issue-1", "CHILD-issue-2"]
    tagged = repo.references["refs/tags/CHILD-issue-2"].peel(pygit2.Commit)
    assert tagged.id == repo.head.target
    assert "CHILD-issue-2" in w._status.currentMessage()
    assert svc.current_issue("CHILD") == "2"


def test_a_tag_in_the_dialog_tags_the_new_commit(
    commit_window, git_doc_repo: Path, monkeypatch
):
    w = commit_window
    svc = w.controller.require_service()
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    _real_commit_dialog(monkeypatch, tag="CDR-1.0", tag_message="Critical design review")

    w._commit_current_doc()

    repo = pygit2.Repository(str(git_doc_repo))
    assert _tags(repo) == ["CDR-1.0"]
    tag = repo.revparse_single("refs/tags/CDR-1.0")
    assert tag.target == repo.head.target
    assert tag.message.strip() == "Critical design review"
    assert "+ tag CDR-1.0" in w._status.currentMessage()
    assert svc.has_uncommitted_changes("CHILD") is False


def test_a_tag_on_a_clean_document_tags_the_current_commit(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """Everything committed, as at a milestone: Commit stays available, and
    the dialog's Tag marks HEAD without making a commit."""
    w = commit_window
    w._refresh_action_states()
    assert w._commit_act.isEnabled()
    repo = pygit2.Repository(str(git_doc_repo))
    head_before = repo.head.target
    shown = _real_commit_dialog(monkeypatch, tag="CDR-1.0", tag_message="CDR")

    w._commit_current_doc()

    assert shown[0].ok_enabled and shown[0].tag_only()
    assert repo.head.target == head_before
    tag = repo.revparse_single("refs/tags/CDR-1.0")
    assert tag.target == head_before and tag.message.strip() == "CDR"
    assert "Tagged the current commit CDR-1.0" in w._status.currentMessage()
    assert w._read_only is True


def test_a_failed_tag_on_a_clean_document_is_shown(
    commit_window, git_doc_repo: Path, monkeypatch
):
    w = commit_window
    repo = pygit2.Repository(str(git_doc_repo))
    real_tag_head = w.controller.tag_head

    def tag_after_a_rival_tag(*args, **kwargs):
        repo.references.create("refs/tags/CDR-1.0", repo.head.target)
        return real_tag_head(*args, **kwargs)

    monkeypatch.setattr(w.controller, "tag_head", tag_after_a_rival_tag)
    critical = _capture_critical(monkeypatch)
    _real_commit_dialog(monkeypatch, tag="CDR-1.0")

    w._commit_current_doc()

    assert "Tag failed" in w._status.currentMessage()
    assert critical and "already exists" in critical[0]
    assert w._read_only is False


def test_issue_and_tag_in_one_commit_name_both(
    commit_window, git_doc_repo: Path, monkeypatch
):
    w = commit_window
    svc = w.controller.require_service()
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    _real_commit_dialog(monkeypatch, issue="1", tag="PDR")

    w._commit_current_doc()

    repo = pygit2.Repository(str(git_doc_repo))
    assert _tags(repo) == ["CHILD-issue-1", "PDR"]
    assert "+ tags CHILD-issue-1, PDR" in w._status.currentMessage()


def test_a_tag_taken_after_the_dialog_opened_fails_but_the_commit_stands(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """Someone else's tag with the same name, created between opening the
    dialog and committing: the commit lands and the status bar says why the
    tag didn't."""
    w = commit_window
    svc = w.controller.require_service()
    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    repo = pygit2.Repository(str(git_doc_repo))
    head_before = repo.head.target
    real_commit = w.controller.commit_document

    def commit_after_a_rival_tag(*args, **kwargs):
        repo.references.create("refs/tags/CDR-1.0", head_before)
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(w.controller, "commit_document", commit_after_a_rival_tag)
    _real_commit_dialog(monkeypatch, tag="CDR-1.0")

    w._commit_current_doc()

    assert repo.head.target != head_before
    assert svc.has_uncommitted_changes("CHILD") is False
    assert "Tag failed" in w._status.currentMessage()
    assert "already exists" in w._status.currentMessage()


# --- discard uncommitted changes ------------------------------------------


def _answer_question(monkeypatch, button) -> list[str]:
    from PySide6.QtWidgets import QMessageBox

    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda _parent, _title, text, *a, **k: asked.append(text) or button),
    )
    return asked


def _edit_and_add_object(w) -> tuple[str, str]:
    """Edit CHILD-0's description and add an object, both uncommitted.
    Returns (original description, new object id)."""
    svc = w.controller.require_service()
    original = svc.get_object("CHILD", "CHILD-0").description
    svc.set_description("CHILD", "CHILD-0", "Edited, not committed")
    new_id = svc.create_object("CHILD", parent_id=None).value
    w.controller.reload_document("CHILD")
    w.controller.select_object("CHILD", "CHILD-0")
    return original, new_id


def test_discard_restores_edits_and_removes_new_objects(
    commit_window, git_doc_repo: Path, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    w = commit_window
    svc = w.controller.require_service()
    original, new_id = _edit_and_add_object(w)
    asked = _answer_question(monkeypatch, QMessageBox.StandardButton.Discard)

    w._main_menu.aboutToShow.emit()
    assert w._discard_act.isEnabled()
    w._discard_act.trigger()

    assert len(asked) == 1
    assert "All edits since the last commit to CHILD will be lost" in asked[0]
    assert "including new objects" in asked[0]
    assert "views and filters" in asked[0]
    assert "left dangling" in asked[0]
    assert svc.get_object("CHILD", "CHILD-0").description == original
    assert new_id not in w.controller.document("CHILD").objects
    assert not (git_doc_repo / "CHILD" / "objects" / f"{new_id}.xml").exists()
    assert svc.has_uncommitted_changes("CHILD") is False
    # The window shows the restored document, and its own discard doesn't
    # read as a change made on disk by someone else.
    assert w.inspector._description.toPlainText() == original
    model = w.grid_pane._model
    assert model.rowCount() == len(w.controller.document("CHILD").objects)
    assert not w.controller.is_doc_stale()
    assert not w.grid_pane.is_stale()
    assert "Discarded uncommitted changes to CHILD" in w._status.currentMessage()
    w._main_menu.aboutToShow.emit()
    assert not w._discard_act.isEnabled()


def test_discard_moves_selection_off_a_removed_new_object(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """The usual case: add an object (which selects it), then discard. The
    window must not keep showing, or editing, the deleted object."""
    from PySide6.QtWidgets import QMessageBox

    w = commit_window
    svc = w.controller.require_service()
    new_id = svc.create_object("CHILD", parent_id=None).value
    svc.set_description("CHILD", new_id, "brand new object text")
    w.controller.reload_document("CHILD")
    w.controller.select_object("CHILD", new_id)
    assert w.inspector._description.toPlainText() == "brand new object text"
    _answer_question(monkeypatch, QMessageBox.StandardButton.Discard)

    w._discard_document_changes()

    doc = w.controller.document("CHILD")
    assert new_id not in doc.objects
    selected = w.controller.current_obj_id()
    assert selected == doc.iter_outline_ids()[0]
    assert w.inspector._current_obj_id == selected
    assert w.inspector._description.toPlainText() == doc.objects[selected].description


def test_discard_unavailable_for_a_never_committed_document(
    qtbot, commit_window, git_doc_repo: Path, monkeypatch
):
    """No last commit to go back to: the action is off, and calling it says
    why instead of asking for a confirmation it can't honour."""
    from PySide6.QtWidgets import QMessageBox

    from mellon.gui.main_window import DocumentEditorWindow

    svc = commit_window.controller.require_service()
    svc.create_document("NEWDOC")
    w = DocumentEditorWindow("NEWDOC", svc)
    w._set_read_only(False)
    qtbot.addWidget(w)
    asked = _answer_question(monkeypatch, QMessageBox.StandardButton.Discard)
    assert svc.has_uncommitted_changes("NEWDOC")

    w._main_menu.aboutToShow.emit()
    assert not w._discard_act.isEnabled()
    w._discard_document_changes()

    assert asked == []
    assert "never been committed" in w._status.currentMessage()
    assert (git_doc_repo / "NEWDOC" / "NEWDOC.xml").exists()


def test_discard_cancel_changes_nothing(
    commit_window, git_doc_repo: Path, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    w = commit_window
    svc = w.controller.require_service()
    _original, new_id = _edit_and_add_object(w)
    obj_file = git_doc_repo / "CHILD" / "objects" / "CHILD-0.xml"
    before = obj_file.read_bytes()
    asked = _answer_question(monkeypatch, QMessageBox.StandardButton.Cancel)

    w._discard_document_changes()

    assert len(asked) == 1
    assert obj_file.read_bytes() == before
    assert (git_doc_repo / "CHILD" / "objects" / f"{new_id}.xml").exists()
    assert new_id in w.controller.document("CHILD").objects
    assert svc.has_uncommitted_changes("CHILD") is True
    assert "cancelled" in w._status.currentMessage()


def test_discard_drops_pending_inspector_text(
    commit_window, git_doc_repo: Path, monkeypatch
):
    """Text typed in the Inspector but not yet saved is discarded too, not
    written back over the restored file by the reload afterwards."""
    from PySide6.QtWidgets import QMessageBox

    w = commit_window
    svc = w.controller.require_service()
    original, _new_id = _edit_and_add_object(w)
    w.inspector._description.setPlainText("Typed, never saved")
    _answer_question(monkeypatch, QMessageBox.StandardButton.Discard)

    w._discard_document_changes()

    assert svc.get_object("CHILD", "CHILD-0").description == original
    assert svc.has_uncommitted_changes("CHILD") is False
    assert w.inspector._description.toPlainText() == original


def test_discard_unavailable_when_read_only_or_clean(commit_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    w = commit_window
    asked = _answer_question(monkeypatch, QMessageBox.StandardButton.Discard)

    # Clean working tree: nothing to discard.
    w._main_menu.aboutToShow.emit()
    assert not w._discard_act.isEnabled()

    _edit_and_add_object(w)
    w._set_read_only(True)
    w._main_menu.aboutToShow.emit()
    assert not w._discard_act.isEnabled()
    w._discard_document_changes()
    assert asked == []
    assert w.controller.require_service().has_uncommitted_changes("CHILD") is True


def test_discard_failure_is_shown_in_a_message_box(commit_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from mellon.service import ErrorCode, ServiceError

    w = commit_window
    svc = w.controller.require_service()
    _edit_and_add_object(w)
    _answer_question(monkeypatch, QMessageBox.StandardButton.Discard)

    def fail(_doc_id):
        raise ServiceError(ErrorCode.GIT, "a merge is in progress")

    monkeypatch.setattr(svc, "discard_document_changes", fail)
    boxes = _capture_critical(monkeypatch)

    w._discard_document_changes()

    assert boxes == ["a merge is in progress"]
    assert "Discard failed" in w._status.currentMessage()
    assert svc.has_uncommitted_changes("CHILD") is True


# --- per-object dirty detection ------------------------------------------


def test_build_dirty_ids_clean_repo(qtbot, git_doc_repo: Path):
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    assert controller.build_dirty_ids("PARENT") == frozenset()
    assert controller.build_dirty_ids("CHILD") == frozenset()


def test_build_dirty_ids_picks_up_object_edit(qtbot, git_doc_repo: Path):
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")

    assert controller.build_dirty_ids("CHILD") == frozenset({"CHILD-0"})
    # Sibling doc unaffected.
    assert controller.build_dirty_ids("PARENT") == frozenset()


def test_build_dirty_ids_picks_up_link_edit(qtbot, git_doc_repo: Path):
    """A change under `links/<id>.xml` flips the source object's pill yellow."""
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    # Add a second outgoing link to CHILD-0 — touches links/CHILD-0.xml only.
    svc.add_link("CHILD", "CHILD-0", "PARENT", "PARENT-0", "satisfies")
    assert controller.build_dirty_ids("CHILD") == frozenset({"CHILD-0"})


def test_build_dirty_ids_clears_after_commit(qtbot, git_doc_repo: Path):
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    svc.set_attribute("CHILD", "CHILD-0", "status", "Approved")
    assert controller.build_dirty_ids("CHILD") == frozenset({"CHILD-0"})

    controller.commit_document(
        "CHILD", "Edited CHILD. Affected requirements: CHILD-0."
    )
    assert controller.build_dirty_ids("CHILD") == frozenset()


def test_build_dirty_ids_returns_empty_outside_git(qtbot, mcp_repo: Path):
    """No git repo → empty set, not an exception."""
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(mcp_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    # mcp_repo has no .git, so dirty detection cleanly degrades.
    assert controller.build_dirty_ids("CHILD") == frozenset()


def test_build_dirty_ids_picks_up_figure_edit(qtbot, git_doc_repo: Path):
    """A new or modified `figures/<id>.svg` flips the object's pill yellow."""
    from mellon.gui.controller import RepoController
    from mellon.service import DocumentService

    svc = DocumentService(git_doc_repo, cache=True)
    controller = RepoController()
    controller.attach_service(svc)

    figures = git_doc_repo / "CHILD" / "figures"
    figures.mkdir()
    (figures / "CHILD-0.svg").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>\n',
        encoding="utf-8",
    )

    assert controller.build_dirty_ids("CHILD") == frozenset({"CHILD-0"})
    assert controller.build_dirty_ids("PARENT") == frozenset()


# MELLON-STORE-SRS blind tests


@pytest.fixture
def authorless_editor_repo(tmp_path, git_env):
    """DOC's manifest has no author, so the Commit dialog writes one and so reads
    DOC again. (With author and date already set, 0.3.0 reads no object file
    while committing, so there is no re-read to observe.)"""
    root = git_init(git_env, tmp_path / "ed")
    make_doc(root, "DOC", n=3, metadata="<title>T</title>")
    git_commit_all(git_env, root, "seed")
    return root


def _commit_via_dialog(gui, ed):
    from PySide6.QtWidgets import QDialog

    def accept(dialog):
        gui.messages.append(("dialog", dialog.windowTitle(), ""))
        dialog.accept()
        return dialog.result()

    gui.monkeypatch.setattr(QDialog, "exec", accept)
    gui.trigger(ed, "Commit")


def _commit_refused_then_f5(gui, ed, root):
    """DOC-2 is edited, then another program puts conflict markers in
    DOC-1.xml: the commit is refused under the Changed on disk banner
    (MELLON-GUI-SRS-111), and F5 reads the document again. Returns what the
    window showed."""
    assert gui.grid_edit(ed, "DOC-2", "rationale", "to commit")
    head = (root / ".git" / "HEAD").read_bytes()
    _break_doc_1(root)
    _commit_via_dialog(gui, ed)
    assert ed.grid_pane.is_stale(), "precondition: the commit is refused under the banner"
    assert (root / ".git" / "HEAD").read_bytes() == head
    gui.messages.clear()
    gui.press_f5(ed)
    return _shows_load_error(gui, ed)


def test_store_srs_37_reread_in_commit_dialog_names_file_and_line(gui, authorless_editor_repo):
    """MELLON-STORE-SRS-37: when the Document Editor reads DOC again and
    DOC-1.xml holds conflict markers, the window shows the load error's file
    and line, not a general failure. A commit after another program broke the
    file is refused first (MELLON-GUI-SRS-111); the re-read comes with F5."""
    root = authorless_editor_repo
    ed = gui.editor(root, "DOC")
    gui.set_read_only(ed, False)
    shown = _commit_refused_then_f5(gui, ed, root)
    assert "DOC-1.xml" in shown, f"load error file not shown: {shown!r}"
    assert f"line {CONFLICT_LINE}" in shown or f":{CONFLICT_LINE}" in shown, shown


def test_store_srs_37_reread_in_commit_dialog_says_merge_conflict(gui, authorless_editor_repo):
    """MELLON-STORE-SRS-37 with -30: the load error shown on the re-read after
    a refused commit says the file looks like an unresolved merge conflict."""
    root = authorless_editor_repo
    ed = gui.editor(root, "DOC")
    gui.set_read_only(ed, False)
    shown = _commit_refused_then_f5(gui, ed, root)
    assert "DOC-1.xml" in shown, f"precondition: the load error was shown: {shown!r}"
    assert CONFLICT_HINT.search(shown), shown


def test_store_srs_167_commit_dialog_put_back_failing_partway(gui, tmp_path, git_env):
    """MELLON-STORE-SRS-167 (CF-S34): in the Commit dialog, when the commit fails
    and then the put-back of the manifest fails partway, the manifest holds its
    content before the put-back or the full content put back."""
    root = git_init(git_env, tmp_path / "pb")
    make_doc(root, metadata="<title>T</title>")  # no author: the dialog writes one
    git_commit_all(git_env, root, "seed")
    manifest = root / "DOC" / "DOC.xml"
    original = manifest.read_bytes()
    ed = gui.editor(root, "DOC")
    gui.set_read_only(ed, False)
    assert gui.grid_edit(ed, "DOC-2", "rationale", "to commit")
    head = git(git_env, root, "rev-parse", "HEAD")
    write(root / ".git" / "index.lock", b"")  # the commit fails
    inj = PartialWrites(gui.monkeypatch, root / "DOC", nth=2, watch=manifest)
    with gui.qtbot.capture_exceptions():  # what the window does with the error isn't measured here
        _commit_via_dialog(gui, ed)
    inj.armed = False
    (root / ".git" / "index.lock").unlink()
    assert git(git_env, root, "rev-parse", "HEAD") == head, "precondition: the commit failed"
    assert inj.fired, f"test error: the put-back wasn't intercepted ({inj.count} writes)"
    assert inj.content_before is not None and inj.content_before != original, (
        "precondition: the dialog had written an author before the commit"
    )
    now = manifest.read_bytes()
    assert now in (original, inj.content_before), f"manifest left cut: {now!r}"


# MELLON-VER-SRS blind tests
import hashlib  # noqa: E402
import re  # noqa: E402
import warnings  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAction, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from mellon.gui.dialogs.commit_dialog import CommitDialog  # noqa: E402
from mellon.gui.main_window import DocumentEditorWindow  # noqa: E402
from mellon.service.document import DocumentService  # noqa: E402
from tests.edit_b_support import _plain  # noqa: E402
from tests.ver_support import (  # noqa: E402
    ANN,
    GIT_OPERATIONS,
    SVG_RED,
    _action,
    _activate,
    _assert_hooks_live,
    _assert_identity,
    _assert_only_branch_and_tags_moved,
    _branch,
    _button,
    _changed,
    _commit,
    _dates,
    _description,
    _git,
    _grid_index,
    _head,
    _index_entries,
    _init,
    _inspector_description,
    _install_hooks,
    _make_repo,
    _manifest,
    _many_refs,
    _merge_dialog,
    _obj,
    _open_editor,
    _parents,
    _refs,
    _remote,
    _remote_state,
    _rev,
    _start_clean_merge,
    _status,
    _status_labels,
    _stop_git_operation,
    _texts,
    _tree,
    _type_into,
    _unborn_doc,
    _unmerged,
    _work_files,
    _write,
    _xml,
)


def _refresh_menu(action: QAction) -> None:
    """Emit aboutToShow on each menu holding `action`, as opening it would."""
    from PySide6.QtWidgets import QMenu

    for obj in action.associatedObjects():
        if isinstance(obj, QMenu):
            obj.aboutToShow.emit()


def _make_active(qtbot, window: QWidget) -> None:
    window.raise_()
    window.activateWindow()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        QApplication.setActiveWindow(window)
    qtbot.wait(20)


def _status_all(window) -> str:
    return " | ".join([window.statusBar().currentMessage(), *_status_labels(window)])


def _form_field(dialog: QWidget, label: str) -> QLineEdit:
    """The line edit in the form row whose label reads `label`."""
    for fl in dialog.findChildren(QFormLayout):
        for r in range(fl.rowCount()):
            li = fl.itemAt(r, QFormLayout.ItemRole.LabelRole)
            fi = fl.itemAt(r, QFormLayout.ItemRole.FieldRole)
            if not li or not li.widget() or not isinstance(li.widget(), QLabel):
                continue
            if _plain(li.widget().text()).rstrip(":") != label:
                continue
            if fi.widget() and isinstance(fi.widget(), QLineEdit):
                return fi.widget()
            lay = fi.layout()
            if lay is not None:
                for i in range(lay.count()):
                    w = lay.itemAt(i).widget()
                    if isinstance(w, QLineEdit):
                        return w
    raise AssertionError(f"no form field labelled {label!r}")


def _accept(dlg) -> QDialog.DialogCode:
    return QDialog.DialogCode.Accepted


def _gui_commit(qtbot, ui, root: Path, doc: str = "CHILD", on_dialog=_accept) -> DocumentEditorWindow:
    """Open `doc` writable and choose Commit, accepting the commit dialog."""
    w = _open_editor(qtbot, root, doc)
    ui.on_commit_dialog = on_dialog
    _action(w, "Commit").trigger()
    qtbot.wait(20)
    return w


def test_ver_srs_91_commit_dialog_leaves_side_files_out_of_the_commit(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-91: a commit from the commit dialog holds the latest
    commit's files with the document's changed files, and no editor side file."""
    root = _make_repo(tmp_path / "repo")
    before = _tree(root)
    edited = _obj("CHILD-0", "Edited in the working copy.")
    _write(root / "CHILD/objects/CHILD-0.xml", edited)
    _write(root / "CHILD/figures/.$CHILD-0.svg.bkp", SVG_RED)
    _write(root / "CHILD/~$CHILD.xml", "lock file")
    old_head = _head(root)

    _gui_commit(qtbot, ui, root)

    assert _head(root) != old_head, f"no commit was made; messages: {ui.message_text()}"
    expected = dict(before)
    expected["CHILD/objects/CHILD-0.xml"] = edited.encode()
    assert _tree(root) == expected, "a side file, or another file, went into the commit"


def test_ver_srs_93_first_commit_from_commit_dialog_holds_only_the_documents_files(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-93: the first commit from the commit dialog holds only
    the selected paths, the document's files, as the working copy holds them."""
    root = tmp_path / "repo"
    _init(root)
    files = _unborn_doc(root)

    _gui_commit(qtbot, ui, root, "DOC")

    assert _head(root) is not None, f"no first commit was made; messages: {ui.message_text()}"
    assert _parents(root) == [], "the first commit has a parent"
    assert _tree(root) == {
        "DOC/DOC.xml": files["DOC/DOC.xml"].encode(),
        "DOC/objects/DOC-0.xml": files["DOC/objects/DOC-0.xml"].encode(),
    }


@pytest.mark.parametrize("missing", ["user.email", "user.name"])
def test_ver_srs_112_document_editor_commit_refused_when_one_identity_setting_is_missing(
    tmp_path, qtbot, ui, only_repo_git_config, missing
):
    """MELLON-VER-SRS-112: a commit from the Document Editor is refused when
    `user.name` or `user.email` isn't set."""
    root = _make_repo(tmp_path / "repo")
    _git(root, "config", "--unset", missing)
    assert missing not in pygit2.Repository(str(root)).config
    old_head = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Waiting for an identity."))

    _gui_commit(qtbot, ui, root)

    assert _head(root) == old_head, f"the commit was made without {missing}"
    assert any(line.endswith("CHILD/objects/CHILD-0.xml") for line in _status(root))


def test_ver_srs_115_commit_dialog_records_git_identity_as_author_and_committer(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-115: When Mellon commits, Mellon records git's
    `user.name` and `user.email` as the commit's author and committer."""
    root = _make_repo(tmp_path / "repo", ANN)
    old_head = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Committed by Ann."))

    _gui_commit(qtbot, ui, root)

    assert _head(root) != old_head, f"no commit; messages: {ui.message_text()}"
    _assert_identity(root, *ANN)


def test_ver_srs_117_commit_dialog_refused_during_merge_without_conflict(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-117: a commit from the commit dialog is refused while a
    merge stopped without a conflict is in progress."""
    root = _make_repo(tmp_path / "repo")
    _start_clean_merge(root)
    old_head = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Edited during the merge."))

    _gui_commit(qtbot, ui, root)

    assert _head(root) == old_head, "a commit was made during the merge"
    assert (root / ".git/MERGE_HEAD").exists(), "the merge is no longer in progress"


@pytest.mark.parametrize("op", GIT_OPERATIONS)
def test_ver_srs_118_commit_dialog_refused_while_git_operation_in_progress(tmp_path, qtbot, ui, op):
    """MELLON-VER-SRS-118: a commit from the commit dialog is refused while a
    rebase, cherry-pick or revert is in progress."""
    root = _make_repo(tmp_path / "repo")
    _stop_git_operation(root, op)
    old_head = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", f"Edited during {op}."))

    _gui_commit(qtbot, ui, root)

    assert _head(root) == old_head, f"a commit was made during {op}"


def test_ver_srs_121_commit_dialog_with_index_lock_says_commit_again(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-121: a commit from the commit dialog while another
    program holds the index lock makes no commit and says to commit again."""
    root = _make_repo(tmp_path / "repo")
    old_head = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Edited under a lock."))
    lock = root / ".git/index.lock"

    def take_lock(dlg):
        _write(lock, "")
        return QDialog.DialogCode.Accepted

    try:
        w = _gui_commit(qtbot, ui, root, on_dialog=take_lock)
        shown = ui.message_text() + "\n" + _status_all(w)
    finally:
        if lock.exists():
            lock.unlink()

    assert _head(root) == old_head
    assert re.search(r"\bagain\b", shown.lower()), f"nothing says to commit again: {shown}"


def test_ver_srs_131_commit_dialog_runs_no_git_hook(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-131: When Mellon commits, Mellon runs no git hook. A
    failing pre-commit hook doesn't stop the commit dialog's commit, and no
    hook leaves its marker."""
    root = _make_repo(tmp_path / "repo")
    markers = tmp_path / "markers"
    _install_hooks(root, markers)
    old_head = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Committed past the hooks."))

    _gui_commit(qtbot, ui, root)

    assert _parents(root) == [old_head], f"no commit was made; messages: {ui.message_text()}"
    assert sorted(p.name for p in markers.iterdir()) == [], "a git hook ran"
    _assert_hooks_live(root, markers)


# ==========================================================================
# The commit dialog
# ==========================================================================


def test_ver_srs_141_commit_action_opens_commit_dialog_for_the_open_document(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-141: The Document Editor has a Commit action that opens
    the commit dialog for the open document, listing its changed file."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Changed for the dialog."))
    _write(root / "PARENT/objects/PARENT-1.xml", _obj("PARENT-1", "Another document's change."))
    w = _open_editor(qtbot, root)

    _action(w, "Commit").trigger()

    assert len(ui.commit_dialogs) == 1, "the Commit action didn't open the commit dialog"
    files = [i for i in ui.commit_dialog_items[0] if re.search(r"\.(xml|svg)$", i)]
    assert files == ["CHILD/objects/CHILD-0.xml"], f"listed files: {ui.commit_dialog_items[0]}"
    assert any("CHILD" in t for t in ui.commit_dialog_texts[0])


def test_ver_srs_142_commit_action_shortcuts_are_exactly_ctrl_s_and_ctrl_k(tmp_path, qtbot):
    """MELLON-VER-SRS-142: The Commit action has the shortcuts Ctrl+S and Ctrl+K."""
    root = _make_repo(tmp_path / "repo")
    w = _open_editor(qtbot, root, writable=False)

    keys = {s.toString(QKeySequence.SequenceFormat.PortableText) for s in _action(w, "Commit").shortcuts()}

    assert keys == {"Ctrl+S", "Ctrl+K"}


@pytest.mark.parametrize("key", ["S", "K"])
def test_ver_srs_142_shortcut_opens_the_commit_dialog(tmp_path, qtbot, ui, key):
    """MELLON-VER-SRS-142: pressing Ctrl+S, or Ctrl+K, opens the commit dialog."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Changed."))
    w = _open_editor(qtbot, root)
    _make_active(qtbot, w)

    QTest.keyClick(w.windowHandle(), getattr(Qt.Key, f"Key_{key}"), Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(20)

    assert len(ui.commit_dialogs) == 1, f"Ctrl+{key} didn't open the commit dialog"


def test_ver_srs_143_commit_dialog_lists_the_file_of_text_typed_in_a_grid_cell(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-143: When the user commits from a Document Editor,
    Mellon first saves the pending edits: text typed in a grid cell without
    leaving the cell is saved before the commit dialog lists the files, so
    the object file is listed (there was no other change)."""
    root = _make_repo(tmp_path / "repo")
    w = _open_editor(qtbot, root)
    table = w.grid_pane.table
    idx = _grid_index(w, "CHILD-0", "Description")
    table.setCurrentIndex(idx)
    table.edit(idx)
    qtbot.wait(20)
    editor = QApplication.focusWidget()
    assert isinstance(editor, (QTextEdit, QPlainTextEdit, QLineEdit)), f"no cell editor opened: {editor}"
    _type_into(qtbot, editor, "Typed in the grid.")

    _action(w, "Commit").trigger()

    assert ui.commit_dialogs, "the commit dialog didn't open"
    assert any("CHILD/objects/CHILD-0.xml" in i for i in ui.commit_dialog_items[0]), ui.commit_dialog_items[0]


def test_ver_srs_143_committed_text_typed_in_a_grid_cell(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-143: the new commit holds the text typed in a grid cell
    that was still open when the user chose Commit."""
    root = _make_repo(tmp_path / "repo")
    w = _open_editor(qtbot, root)
    ui.on_commit_dialog = _accept
    table = w.grid_pane.table
    idx = _grid_index(w, "CHILD-0", "Description")
    table.setCurrentIndex(idx)
    table.edit(idx)
    qtbot.wait(20)
    editor = QApplication.focusWidget()
    assert isinstance(editor, (QTextEdit, QPlainTextEdit, QLineEdit)), f"no cell editor opened: {editor}"
    _type_into(qtbot, editor, "Typed in the grid.")
    old_head = _head(root)

    _action(w, "Commit").trigger()
    qtbot.wait(20)

    assert _head(root) != old_head, f"no commit; messages: {ui.message_text()}"
    assert _description(_tree(root)["CHILD/objects/CHILD-0.xml"]) == "Typed in the grid."


def test_ver_srs_143_committed_text_typed_in_the_inspector(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-143: the commit dialog lists, and the new commit holds,
    description text typed in the Inspector and not yet saved."""
    root = _make_repo(tmp_path / "repo")
    w = _open_editor(qtbot, root)
    ui.on_commit_dialog = _accept
    w.grid_pane.table.setCurrentIndex(_grid_index(w, "CHILD-0", "Description"))
    qtbot.wait(20)
    field = _inspector_description(w)
    assert field.toPlainText() == "Text of CHILD-0.", "the Inspector doesn't show CHILD-0"
    _type_into(qtbot, field, "Typed in the Inspector.")
    old_head = _head(root)

    _action(w, "Commit").trigger()
    qtbot.wait(20)

    assert ui.commit_dialogs, "the commit dialog didn't open"
    assert any("CHILD/objects/CHILD-0.xml" in i for i in ui.commit_dialog_items[0]), ui.commit_dialog_items[0]
    assert _head(root) != old_head, f"no commit; messages: {ui.message_text()}"
    assert _description(_tree(root)["CHILD/objects/CHILD-0.xml"]) == "Typed in the Inspector."


def test_ver_srs_144_commit_refused_while_changed_on_disk_banner_shows(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-144: While the Document Editor shows the Changed on disk
    banner, the Document Editor refuses to commit: no dialog, no commit."""
    root = _make_repo(tmp_path / "repo")
    w = _open_editor(qtbot, root)
    ui.on_commit_dialog = _accept
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Changed outside Mellon."))
    _make_active(qtbot, w)
    _activate(qtbot, w)
    assert w.grid_pane.is_stale(), "the Changed on disk banner didn't show"
    old_head = _head(root)

    _action(w, "Commit").trigger()
    QTest.keyClick(w.windowHandle(), Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(20)

    assert ui.commit_dialogs == [], "the commit dialog opened under the banner"
    assert _head(root) == old_head


def test_ver_srs_144_commit_opens_again_after_reload(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-144, other side: once the window reloads and the banner
    is gone, Commit opens the commit dialog again."""
    root = _make_repo(tmp_path / "repo")
    w = _open_editor(qtbot, root)
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Changed outside Mellon."))
    _activate(qtbot, w)
    assert w.grid_pane.is_stale()
    _action(w, "Reload from disk").trigger()
    qtbot.wait(20)
    assert not w.grid_pane.is_stale()
    ro = _action(w, "Read-only")
    if ro.isChecked():
        ro.trigger()

    _action(w, "Commit").trigger()

    assert len(ui.commit_dialogs) == 1


def test_ver_srs_145_commit_refused_in_read_only_mode(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-145: While the Document Editor is in read-only mode, it
    refuses to commit: neither the menu's Commit nor Ctrl+S opens the dialog.
    Control: out of read-only mode, Ctrl+S opens it."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "An uncommitted change."))
    w = _open_editor(qtbot, root, writable=False)
    ro = _action(w, "Read-only")
    if not ro.isChecked():
        ro.trigger()
    assert ro.isChecked()
    ui.on_commit_dialog = _accept
    _make_active(qtbot, w)
    old_head = _head(root)

    _action(w, "Commit").trigger()
    QTest.keyClick(w.windowHandle(), Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(20)

    assert ui.commit_dialogs == [], "the commit dialog opened in read-only mode"
    assert _head(root) == old_head
    ui.on_commit_dialog = lambda dlg: QDialog.DialogCode.Rejected
    ro.trigger()
    _make_active(qtbot, w)
    QTest.keyClick(w.windowHandle(), Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(20)
    assert len(ui.commit_dialogs) == 1, "control: Ctrl+S doesn't open the dialog out of read-only mode"


def test_ver_srs_146_commit_dialog_leaves_out_a_file_git_ignores(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-146: The commit dialog lists each uncommitted change in
    the open document's folder. A file git ignores isn't an uncommitted change."""
    root = _make_repo(tmp_path / "repo")
    _write(root / ".gitignore", "*.tmp\n")
    _commit(root, "ignore tmp files", tick=2)
    _write(root / "CHILD/scratch.tmp", "ignored\n")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Changed."))
    w = _open_editor(qtbot, root)

    _action(w, "Commit").trigger()

    assert ui.commit_dialogs, "the commit dialog didn't open"
    items = ui.commit_dialog_items[0]
    assert any("CHILD/objects/CHILD-0.xml" in i for i in items), items
    assert not any("scratch.tmp" in i for i in items), f"an ignored file is listed: {items}"


def test_ver_srs_146_commit_dialog_leaves_out_editor_side_files(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-146: an editor side file isn't an uncommitted change, so
    the commit dialog doesn't list it."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/figures/.$CHILD-0.svg.bkp", SVG_RED)
    _write(root / "CHILD/~$CHILD.xml", "lock file")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Changed."))
    w = _open_editor(qtbot, root)

    _action(w, "Commit").trigger()

    assert ui.commit_dialogs, "the commit dialog didn't open"
    items = ui.commit_dialog_items[0]
    assert any("CHILD/objects/CHILD-0.xml" in i for i in items), items
    assert not any("$" in i for i in items), f"a side file is listed: {items}"


def test_ver_srs_149_commit_dialog_lists_a_broken_link(tmp_path, qtbot):
    """MELLON-VER-SRS-149: The commit dialog lists the open document's broken
    outgoing links: CHILD-0's link to PARENT-1, whose object file is gone."""
    root = _make_repo(tmp_path / "repo")
    (root / "PARENT/objects/PARENT-1.xml").unlink()
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "A change to commit."))
    preview = DocumentService(root).prepare_document_commit("CHILD")
    dlg = CommitDialog(preview)
    qtbot.addWidget(dlg)
    dlg.show()

    lines = [t for t in _texts(dlg) if "PARENT-1" in t]

    assert lines, f"the dialog doesn't show the link to PARENT-1: {_texts(dlg)}"
    assert any("CHILD-0" in t and "broken" in t.lower() for t in lines) or (
        any("CHILD-0" in t for t in lines) and any("broken" in t.lower() for t in _texts(dlg))
    ), f"the link isn't listed as broken: {lines}"


def test_ver_srs_161_commit_dialog_refuses_an_empty_author(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-161: If the commit dialog's author is empty, the commit
    dialog refuses the commit: no commit, and the manifest keeps its author."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Changed."))
    old_head = _head(root)
    seen = {}

    def clear_author(dlg):
        field = _form_field(dlg, "Author")
        field.setText("")
        seen["author"] = dlg.author()
        button = _button(dlg, "Commit")
        seen["enabled"] = button.isEnabled()
        button.click()
        seen["result"] = dlg.result()
        return dlg.result()

    _gui_commit(qtbot, ui, root, on_dialog=clear_author)

    assert seen.get("author") == "", "the author field wasn't cleared"
    assert seen.get("result") != QDialog.DialogCode.Accepted, "the dialog accepted an empty author"
    assert _head(root) == old_head, "a commit was made with an empty author"
    assert _xml((root / "CHILD/CHILD.xml").read_bytes()).findtext("metadata/author") == "Ann Author"


def _commit_elsewhere(root: Path) -> str:
    """A git client's commit on the current branch, without touching the index."""
    new = _git(root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "git client commit", env=_dates(5)).stdout.strip()
    _git(root, "update-ref", "HEAD", new)
    return new


@pytest.mark.parametrize(
    "cause",
    [
        "head_moved_while_recording",
        "index_lock",
    ],
)
def test_ver_srs_168_failed_dialog_commit_restores_manifest_author_and_date(tmp_path, qtbot, ui, monkeypatch, cause):
    """MELLON-VER-SRS-168: If a commit from the commit dialog fails, Mellon
    restores the manifest's author and date to their values before the
    dialog's edit. Failures: a git client commits while Mellon records the
    commit (MELLON-VER-SRS-123 refuses it), and a held index lock.

    The git client's commit is injected at libgit2's commit creation, the
    moment Mellon records; a commit made while the dialog is open isn't
    refused by 0.3.0 (MELLON-VER-SRS-124 is only Proposed)."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Changed."))
    old_head = _head(root)
    lock = root / ".git/index.lock"
    moved = {}

    def edit_then_fail(dlg):
        _form_field(dlg, "Author").setText("bob")
        _form_field(dlg, "Date").setText("2026-02-02")
        assert dlg.author() == "bob"
        if cause == "head_moved_while_recording":
            original = pygit2.Repository.create_commit

            def racing(self, *args, **kwargs):
                if "head" not in moved:
                    moved["head"] = _commit_elsewhere(root)
                return original(self, *args, **kwargs)

            monkeypatch.setattr(pygit2.Repository, "create_commit", racing)
        else:
            _write(lock, "")
        return QDialog.DialogCode.Accepted

    try:
        _gui_commit(qtbot, ui, root, on_dialog=edit_then_fail)
    finally:
        if lock.exists():
            lock.unlink()

    if cause == "head_moved_while_recording":
        assert "head" in moved, "setup: the git client's commit wasn't injected while Mellon recorded"
    expected_head = moved.get("head", old_head)
    assert _head(root) == expected_head, (
        f"the commit didn't fail ({cause}), so the test can't check the restore; messages: {ui.message_text()}"
    )
    meta = _xml((root / "CHILD/CHILD.xml").read_bytes())
    assert (meta.findtext("metadata/author"), meta.findtext("metadata/date")) == ("Ann Author", "2026-01-01"), (
        f"the manifest keeps the dialog's edit after the failure: {ui.message_text()}"
    )


def _discard(qtbot, ui, w, answer) -> None:
    action = _action(w, "Discard uncommitted changes to this document")
    _refresh_menu(action)
    ui.answer = answer
    action.trigger()
    qtbot.wait(20)


def _assert_folder_is_head(root: Path, folder: str) -> None:
    head = {p: hashlib.sha256(b).hexdigest() for p, b in _tree(root).items() if p.startswith(folder + "/")}
    assert _work_files(root, folder) == head, "the folder isn't as the latest commit holds it"
    assert _status(root, folder) == []


def test_ver_srs_196_cancelled_discard_changes_no_file_and_no_index_entry(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-196: If the user cancels the discard confirmation,
    Mellon changes no file and no git index entry (a staged change stays
    staged with the same content)."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Staged change."))
    _git(root, "add", "CHILD/objects/CHILD-0.xml")
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Unstaged change."))
    w = _open_editor(qtbot, root)
    files = _work_files(root, include_git=True)
    index = _index_entries(root)

    _discard(qtbot, ui, w, QMessageBox.StandardButton.Cancel)

    assert any(k in ("question", "exec") for k, _, _ in ui.messages), "no confirmation was asked"
    assert _index_entries(root) == index, "a git index entry changed"
    assert sorted(_changed(files, _work_files(root, include_git=True))) == []
    staged = _git(root, "diff", "--cached", "--name-only").stdout.split()
    assert staged == ["CHILD/objects/CHILD-0.xml"]


def test_ver_srs_204_discard_enabled_and_works_while_changed_on_disk_banner_shows(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-204: The Document Editor enables the Discard action
    without regard to the Changed on disk banner; confirming it under the
    banner puts the folder back as the latest commit holds it."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "An uncommitted change."))
    w = _open_editor(qtbot, root)
    action = _action(w, "Discard uncommitted changes to this document")
    _refresh_menu(action)
    before_banner = action.isEnabled()
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Changed outside Mellon."))
    _activate(qtbot, w)
    assert w.grid_pane.is_stale(), "the Changed on disk banner didn't show"
    _refresh_menu(action)
    under_banner = action.isEnabled()

    _discard(qtbot, ui, w, QMessageBox.StandardButton.Discard)

    assert (before_banner, under_banner) == (True, True)
    _assert_folder_is_head(root, "CHILD")


def test_ver_srs_209_discard_refused_when_latest_commit_holds_a_submodule_in_the_folder(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-209: If the latest commit holds a submodule in the
    document's folder, Mellon refuses to discard: no file in the folder
    changes. The submodule is a gitlink entry with its .gitmodules record,
    not checked out (as after `git submodule deinit`)."""
    root = _make_repo(tmp_path / "repo")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{_head(root)},CHILD/vendor")
    _write(root / ".gitmodules", '[submodule "vendor"]\n\tpath = CHILD/vendor\n\turl = ../vendor.git\n')
    _git(root, "add", ".gitmodules")
    _git(root, "commit", "-q", "-m", "add a submodule", env=_dates(2))
    assert _tree(root)["CHILD/vendor"] == b"<commit>", "the submodule entry isn't committed"
    assert "CHILD/vendor" in _git(root, "submodule", "status").stdout
    (root / "CHILD/vendor").mkdir(exist_ok=True)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "An uncommitted change."))
    w = _open_editor(qtbot, root)
    before = _work_files(root, "CHILD")

    _discard(qtbot, ui, w, QMessageBox.StandardButton.Discard)

    assert _work_files(root, "CHILD") == before, "the discard changed a file in the folder"
    assert (root / "CHILD/vendor").is_dir()


def test_ver_srs_211_discard_completes_when_run_again_after_failing_partway(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-211: If a discard fails partway, Mellon completes the
    discard when the user discards again. The folder has one edited object,
    one added and one deleted; a read-only added file makes the first discard fail."""
    root = _make_repo(tmp_path / "repo")
    head_files = {p: b for p, b in _tree(root).items() if p.startswith("CHILD/")}
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Edited."))
    _write(root / "CHILD/objects/CHILD-2.xml", _obj("CHILD-2", "Added."))
    (root / "CHILD/objects/CHILD-1.xml").unlink()
    _write(root / "CHILD/CHILD.xml", _manifest("CHILD", ["CHILD-0", "CHILD-2"], parents=("PARENT",), title="Child", next_number=3))
    stuck = root / "CHILD/objects/CHILD-2.xml"
    real_unlink = Path.unlink

    def refuse_stuck(self, *args, **kwargs):
        # What Windows does for a read-only file. Linux deletes a read-only
        # file (only the folder's permission counts), so chmod can't stand in.
        if os.path.abspath(self) == os.path.abspath(stuck):
            raise PermissionError(13, "refused by the test", str(self))
        return real_unlink(self, *args, **kwargs)

    w = _open_editor(qtbot, root)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "unlink", refuse_stuck)
        _discard(qtbot, ui, w, QMessageBox.StandardButton.Discard)
    assert stuck.exists(), "the first discard didn't fail"
    restored = [p for p in ("CHILD/CHILD.xml", "CHILD/objects/CHILD-0.xml", "CHILD/objects/CHILD-1.xml")
                if (root / p).exists() and (root / p).read_bytes() == head_files[p]]
    partway = bool(restored)

    _discard(qtbot, ui, w, QMessageBox.StandardButton.Discard)

    _assert_folder_is_head(root, "CHILD")
    assert partway, "the first discard restored nothing, so it didn't fail partway (setup)"


def test_ver_srs_292_failed_tag_after_commit_keeps_commit_and_names_the_tag(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-292: If tagging fails after Mellon made the commit,
    Mellon keeps the commit and reports which tag failed and why. Another
    program created the tag name CDR-1.0 after the dialog opened; the raised
    issue's tag is made."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Release text."))
    old_head = _head(root)

    def raise_and_tag(dlg):
        _form_field(dlg, "Issue").setText("1")
        _form_field(dlg, "Name").setText("CDR-1.0")
        assert (dlg.issue(), dlg.tag()) == ("1", "CDR-1.0")
        _git(root, "tag", "-a", "CDR-1.0", "-m", "made elsewhere", old_head)
        return QDialog.DialogCode.Accepted

    w = _gui_commit(qtbot, ui, root, on_dialog=raise_and_tag)

    new_head = _head(root)
    assert _parents(root) == [old_head], f"the commit wasn't kept; messages: {ui.message_text()}"
    assert _rev(root, "refs/tags/CHILD-issue-1^{commit}") == new_head
    assert _rev(root, "refs/tags/CDR-1.0^{commit}") == old_head, "the existing tag moved"
    report = [t for t in (ui.message_text() + "\n" + _status_all(w)).splitlines() if "CDR-1.0" in t]
    assert report, f"no report names CDR-1.0: {ui.message_text()} {_status_all(w)}"
    assert any("exist" in t.lower() for t in report), f"the report doesn't say the tag exists: {report}"


def _gui_operations(qtbot, ui, root: Path) -> None:
    """Commit with a raised issue and a tag, discard, compare and resolve a
    merge, all through the GUI. The caller has made branch `side`, which
    changes CHILD-1."""
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Mine."))

    def issue_and_tag(dlg):
        _form_field(dlg, "Issue").setText("1")
        _form_field(dlg, "Name").setText("M1")
        return QDialog.DialogCode.Accepted

    old_head = _head(root)
    w = _gui_commit(qtbot, ui, root, on_dialog=issue_and_tag)
    assert _parents(root) == [old_head], f"the GUI commit failed: {ui.message_text()}"
    assert _rev(root, "refs/tags/M1^{commit}") == _head(root), "the GUI tag failed"

    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "To be discarded."))
    _action(w, "Reload from disk").trigger()
    ro = _action(w, "Read-only")
    if ro.isChecked():
        ro.trigger()
    _discard(qtbot, ui, w, QMessageBox.StandardButton.Discard)
    assert _status(root, "CHILD") == [], f"the GUI discard failed: {ui.message_text()}"

    dlg = w.open_compare()
    qtbot.waitUntil(lambda: not dlg.is_busy(), timeout=15000)

    _git(root, "merge", "side", check=False)
    assert _unmerged(root), "the merge didn't stop on a conflict"
    md = _merge_dialog(qtbot, root)
    _button(md, "Resolve").click()
    qtbot.wait(20)
    assert _unmerged(root) == [], f"the GUI resolve failed: {ui.message_text()}"


def _side_branch(root: Path) -> None:
    _branch(root, "side")
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Theirs."))
    _commit(root, "side changes CHILD-1", tick=3)
    _git(root, "checkout", "-q", "main")


def test_ver_srs_373_gui_operations_dont_fetch_pull_or_push(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-373: Mellon doesn't fetch from, pull from or push to a
    git remote. After a GUI commit with an issue and a tag, a discard, a
    compare and a merge resolve: the remote's references are unchanged, no
    FETCH_HEAD is written, the remote-tracking branches don't move, and no
    credential helper ran."""
    root = _make_repo(tmp_path / "repo")
    _side_branch(root)
    bare, marker = _remote(root, tmp_path)
    before = _remote_state(root, bare)

    _gui_operations(qtbot, ui, root)

    assert _remote_state(root, bare) == before
    assert not marker.exists(), "a credential helper ran"


def test_ver_srs_374_gui_operations_change_only_the_current_branch_and_their_own_tags(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-374: Mellon changes no git reference other than the
    current branch, by adding a commit, and the tags Mellon creates (GUI
    commit with issue and tag, discard, compare, merge resolve)."""
    root = _make_repo(tmp_path / "repo")
    _side_branch(root)
    _many_refs(root, tmp_path)
    before = _refs(root)

    _gui_operations(qtbot, ui, root)

    _assert_only_branch_and_tags_moved(before, _refs(root), 1, {"CHILD-issue-1", "M1"}, root)


# MELLON-GUI-SRS blind tests
from tests.gui_support import (  # noqa: E402
    SRS,
    STS,
    SVG,
    SYRS,
    _attach_figure_and_commit,
    _change_elsewhere,
    _commit_from,
    _doc_dirty,
    _head as gui_head,
    _obj_file,
    _reactivated,
    _read_only_banner_shown,
    _refs as gui_refs,
    _stale_shown,
    _uncommitted_change,
    _write as gui_write,
)


_CASES_111 = {
    "own_file_before_commit": (SRS, "VCU3-SRS-2", False),
    "own_file_while_dialog_open": (SRS, "VCU3-SRS-2", True),
    "parent_file_before_commit": (SYRS, "VCU3-SyRS-42", False),
}


@pytest.mark.parametrize("case", list(_CASES_111))
def test_gui_srs_111_commit_refused_after_change_on_disk(gui_gui, repo, case):
    """MELLON-GUI-SRS-111: if a file of the document or of a parent document changed on disk since the Document
    Editor read the file, other than a figure redrawn since, the Document Editor refuses to commit: the latest
    commit and the references stay as they were."""
    doc, oid, in_dialog = _CASES_111[case]
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    refs = gui_refs(repo)
    _commit_from(gui_gui, ed, lambda: _change_elsewhere(_obj_file(repo, doc, oid)), in_dialog)
    if in_dialog:
        assert gui_gui.opened("CommitDialog"), "precondition: the commit dialog opened"
    assert gui_refs(repo) == refs, "the commit went through although a file changed on disk"


def test_gui_srs_111_commit_goes_through_without_change_on_disk(gui_gui, repo):
    """MELLON-GUI-SRS-111 (control): with no file changed on disk since the Document Editor read it, the same
    commit goes through."""
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    head = gui_head(repo)
    _commit_from(gui_gui, ed)
    assert gui_head(repo) != head, "the commit didn't go through"
    assert _doc_dirty(repo, SRS) == []


def test_gui_srs_111_redrawn_figure_doesnt_stop_commit(gui_gui, repo):
    """MELLON-GUI-SRS-111 (control): a figure of the document saved in place by another program and redrawn by
    the Document Editor (MELLON-GUI-SRS-98) doesn't stop the commit."""
    fig = _attach_figure_and_commit(repo, "VCU3-SRS-2")
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = fig.stat()
    gui_write(fig, SVG.format(c="ff0000"))  # draw.io saves the figure in place
    os.utime(fig, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    _reactivated(gui_gui, ed, pe)
    assert not _stale_shown(ed), "precondition: the figure is redrawn, not reported as a change on disk"
    head = gui_head(repo)
    _commit_from(gui_gui, ed)
    assert gui_head(repo) != head, "the commit didn't go through"


@pytest.mark.parametrize("other_mode", ["editing", "read_only"])
def test_gui_srs_88_commit_returns_committing_window_to_read_only(gui_gui, repo, other_mode):
    """MELLON-GUI-SRS-88: when a commit from a Document Editor succeeds, that Document Editor returns to read-only
    mode; another Document Editor keeps its mode."""
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    other = gui_gui.open_doc(pe, STS, editing=(other_mode == "editing"))
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    head = gui_head(repo)
    _commit_from(gui_gui, ed)
    assert gui_head(repo) != head, "precondition: the commit succeeded"
    assert gui_gui.is_read_only(ed), "the committing window stayed out of read-only mode"
    assert _read_only_banner_shown(ed)
    assert gui_gui.is_read_only(other) == (other_mode == "read_only"), "the other window's mode changed"
