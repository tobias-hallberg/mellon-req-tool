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
