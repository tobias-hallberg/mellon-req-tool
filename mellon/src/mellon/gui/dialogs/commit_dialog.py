"""Commit dialog for the currently-open document.

Built from a `DocCommitPreview` so the dialog itself does no I/O. The user
edits the commit message, the issue, and the manifest metadata fields
author / date, all prefilled from the preview. The issue is prefilled with
the document's current one (empty when it has none yet), so a plain commit
cuts no tag; changing it to a new value tags the new commit
`<doc-id>-issue-<n>`. Author/date changes are written to the
manifest by the caller before the commit so they land in the same commit as
the rest of the dirty files; the issue tag is created at the new commit OID
post-commit. When the document has **parent documents** with their own
uncommitted changes, the dialog shows a warning naming them — purely
informational. This commit only ever stages files inside the current
document's folder; sibling and parent docs are never included regardless of
their dirty state.

The optional **Tag** section names a tag for the new commit (e.g. CDR-1.0),
which marks every document as last committed. While a name is entered, the
open points of the documents this one links up to (the whole chain upwards)
are listed as a warning; they never block the commit. With nothing to commit,
a tag alone turns the button into **Tag**: it tags the current commit
(`tag_only`), the way to mark a milestone once everything is committed.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...git_ops import tag_name_error
from ...service import DocCommitPreview


_ISSUE_RE = re.compile(r"^\d+(\.\d+)*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _issue_key(issue: str) -> tuple[int, ...]:
    return tuple(int(p) for p in issue.split("."))


class CommitDialog(QDialog):
    def __init__(
        self,
        preview: DocCommitPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._preview = preview
        self.setWindowTitle(f"Commit — {preview.doc_id}")
        self.resize(560, 600)

        self._title = QLabel(f"Commit changes to {preview.doc_id}")
        self._title.setStyleSheet("font-weight: bold; font-size: 10pt;")

        self._message_label = QLabel("Commit message")
        self._message_label.setStyleSheet("font-weight: 600; margin-top: 4px;")
        self._message = QPlainTextEdit()
        self._message.setPlainText(preview.default_message)
        self._message.setMinimumHeight(96)

        self._meta_label = QLabel("Document metadata")
        self._meta_label.setStyleSheet("font-weight: 600; margin-top: 6px;")

        self._issue_edit = QLineEdit(preview.prefill_issue)
        self._issue_edit.setPlaceholderText("none")
        issue_tip = (
            "Leave the issue as it is for a plain commit. Change it (for "
            "example 2 → 3) to cut a new issue: the commit is then tagged "
            f"{preview.doc_id}-issue-<n>."
        )
        self._issue_edit.setToolTip(issue_tip)
        self._author_edit = QLineEdit(preview.prefill_author)
        self._date_edit = QLineEdit(preview.prefill_date)
        self._date_edit.setPlaceholderText("YYYY-MM-DD")

        # What the issue field will do; kept current by `_refresh_ok`.
        self._issue_hint = QLabel()
        self._issue_hint.setStyleSheet("color: palette(mid); font-size: 9pt;")
        self._issue_hint.setToolTip(issue_tip)

        issue_row = QHBoxLayout()
        issue_row.addWidget(self._issue_edit, 1)
        issue_row.addWidget(self._issue_hint)

        issue_label = QLabel("Issue:")
        issue_label.setToolTip(issue_tip)
        meta_form = QFormLayout()
        meta_form.addRow(issue_label, issue_row)
        meta_form.addRow("Author:", self._author_edit)
        meta_form.addRow("Date:", self._date_edit)

        self._tag_label = QLabel("Tag (optional)")
        self._tag_label.setStyleSheet("font-weight: 600; margin-top: 6px;")
        tag_tip = (
            "Enter a name (for example CDR-1.0) to also tag this commit. The "
            "tag marks every document as last committed. Publish it with "
            "git push --tags."
        )
        self._tag_edit = QLineEdit()
        self._tag_edit.setPlaceholderText("none")
        self._tag_edit.setToolTip(tag_tip)
        self._tag_message_edit = QLineEdit()
        self._tag_message_edit.setPlaceholderText("optional")
        # What the tag will mark; kept current by `_refresh_ok`.
        self._tag_hint = QLabel()
        self._tag_hint.setStyleSheet("color: palette(mid); font-size: 9pt;")
        self._tag_hint.setWordWrap(True)
        self._tag_hint.setToolTip(tag_tip)
        # Its own section, in the metadata form so the fields line up.
        meta_form.addRow(self._tag_label)
        meta_form.addRow("Name:", self._tag_edit)
        meta_form.addRow("Message:", self._tag_message_edit)
        meta_form.addRow(self._tag_hint)

        # Open points up the link chain; shown only while a tag is entered.
        self._upstream_label: QLabel | None = None
        self._upstream_list: QListWidget | None = None
        if preview.upstream_warnings:
            self._upstream_label = QLabel(
                "⚠ The tag also marks the documents this one links up to, "
                "as last committed. They have open points:"
            )
            self._upstream_label.setStyleSheet(
                "color: palette(highlight); font-weight: 600; margin-top: 6px;"
            )
            self._upstream_label.setWordWrap(True)
            self._upstream_list = QListWidget()
            self._upstream_list.setSelectionMode(
                QListWidget.SelectionMode.NoSelection
            )
            self._upstream_list.setMaximumHeight(100)
            for line in preview.upstream_warnings:
                item = QListWidgetItem(line)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._upstream_list.addItem(item)

        self._meta_error = QLabel("")
        self._meta_error.setStyleSheet(
            "color: palette(highlight); font-size: 9pt;"
        )
        self._meta_error.setWordWrap(True)

        self._files_label = QLabel(
            f"Files to commit ({len(preview.paths)})"
        )
        self._files_label.setStyleSheet("font-weight: 600; margin-top: 6px;")
        self._files = QListWidget()
        self._files.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for path in preview.paths:
            item = QListWidgetItem(path)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._files.addItem(item)
        if not preview.paths:
            empty = QListWidgetItem("(no file changes)")
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._files.addItem(empty)

        self._warning_label: QLabel | None = None
        self._warning_list: QListWidget | None = None
        if preview.dirty_parents:
            self._warning_label = QLabel(
                "⚠ Parent document(s) also have uncommitted changes. "
                "Only this document will be committed:"
            )
            self._warning_label.setStyleSheet(
                "color: palette(highlight); font-weight: 600; margin-top: 6px;"
            )
            self._warning_label.setWordWrap(True)
            self._warning_list = QListWidget()
            self._warning_list.setSelectionMode(
                QListWidget.SelectionMode.NoSelection
            )
            self._warning_list.setMaximumHeight(80)
            for parent_id in preview.dirty_parents:
                item = QListWidgetItem(parent_id)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._warning_list.addItem(item)

        self._suspect_label: QLabel | None = None
        self._suspect_list: QListWidget | None = None
        if preview.suspect_links:
            n = len(preview.suspect_links)
            self._suspect_label = QLabel(
                f"⚠ {n} unreviewed link(s) on this document. "
                "Consider acknowledging before committing:"
            )
            self._suspect_label.setStyleSheet(
                "color: palette(highlight); font-weight: 600; margin-top: 6px;"
            )
            self._suspect_label.setWordWrap(True)
            self._suspect_list = QListWidget()
            self._suspect_list.setSelectionMode(
                QListWidget.SelectionMode.NoSelection
            )
            self._suspect_list.setMaximumHeight(120)
            for status in preview.suspect_links:
                line = (
                    f"[{status.state.value}] {status.source_id} → "
                    f"{status.link.target_doc}/{status.link.target_id}"
                )
                if status.reason:
                    line += f"  ({status.reason})"
                item = QListWidgetItem(line)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._suspect_list.addItem(item)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Commit")
        ok_btn.setDefault(True)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._message.textChanged.connect(self._refresh_ok)
        self._issue_edit.textChanged.connect(self._refresh_ok)
        self._author_edit.textChanged.connect(self._refresh_ok)
        self._date_edit.textChanged.connect(self._refresh_ok)
        self._tag_edit.textChanged.connect(self._refresh_ok)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._message_label)
        layout.addWidget(self._message)
        layout.addWidget(self._meta_label)
        layout.addLayout(meta_form)
        if self._upstream_label is not None and self._upstream_list is not None:
            layout.addWidget(self._upstream_label)
            layout.addWidget(self._upstream_list)
        layout.addWidget(self._meta_error)
        layout.addWidget(self._files_label)
        layout.addWidget(self._files)
        if self._warning_label is not None and self._warning_list is not None:
            layout.addWidget(self._warning_label)
            layout.addWidget(self._warning_list)
        if self._suspect_label is not None and self._suspect_list is not None:
            layout.addWidget(self._suspect_label)
            layout.addWidget(self._suspect_list)
        layout.addWidget(self._buttons)

        self._refresh_ok()

    def _metadata_error(self) -> str:
        issue = self._issue_edit.text().strip()
        # Empty means "no issue tag" (a document without one yet).
        if issue and not _ISSUE_RE.match(issue):
            return (
                "Issue must be a dot-separated numeric sequence "
                "(e.g. '1', '1.2', '12.23.54')."
            )
        current = self._preview.current_issue
        if (
            self._cuts_new_issue()
            and _ISSUE_RE.match(current)
            and _issue_key(issue) <= _issue_key(current)
        ):
            return f"A new issue must be higher than the current issue {current}."
        if not self._author_edit.text().strip():
            return "Author must not be empty."
        date_val = self._date_edit.text().strip()
        # The manifest's own date is prefilled and may be empty; an existing
        # date can be changed but not cleared.
        if (date_val or self._preview.current_date) and not _DATE_RE.match(
            date_val
        ):
            return "Date must be in YYYY-MM-DD format."
        tag = self.tag()
        if tag:
            return tag_name_error(tag, self._preview.existing_tags)
        return ""

    def _cuts_new_issue(self) -> bool:
        """True when the issue field asks for a new issue tag: non-empty and
        different from the current issue. Anything else is a plain commit."""
        issue = self._issue_edit.text().strip()
        return bool(issue) and issue != self._preview.current_issue

    def _has_changes(self) -> bool:
        """Something to commit: dirty files, or an author/date edit that the
        caller writes to the manifest first. A new issue lives only in its
        tag, so on its own it is not a change."""
        p = self._preview
        return (
            bool(p.paths)
            or self._author_edit.text().strip() != p.current_author
            or self._date_edit.text().strip() != p.current_date
        )

    def _refresh_issue_hint(self) -> None:
        current = self._preview.current_issue
        issue = self._issue_edit.text().strip()
        if self._cuts_new_issue() and _ISSUE_RE.match(issue):
            tag = f"{self._preview.doc_id}-issue-{issue}"
            if self._has_changes():
                text = f"new issue: tags the commit {tag}"
            else:
                text = f"nothing to commit: make a change to cut {tag}"
        elif current:
            text = f"current issue {current}; change it to cut a new issue"
        else:
            text = "no issue yet; enter one to cut the first issue"
        self._issue_hint.setText(text)

    def _refresh_tag_warning(self) -> None:
        """Show the upstream open points while a tag is entered. They cover
        the direct parents too, so the parent warning steps aside meanwhile."""
        if self._upstream_label is None or self._upstream_list is None:
            return
        show = bool(self.tag())
        self._upstream_label.setVisible(show)
        self._upstream_list.setVisible(show)
        if self._warning_label is not None and self._warning_list is not None:
            self._warning_label.setVisible(not show)
            self._warning_list.setVisible(not show)

    def tag_only(self) -> bool:
        """Nothing to commit and a tag entered: OK tags the current commit
        instead of committing."""
        return not self._has_changes() and bool(self.tag())

    def _refresh_ok(self) -> None:
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._refresh_issue_hint()
        self._refresh_tag_warning()
        if self._has_changes():
            self._tag_hint.setText("Tags every document at this commit.")
        else:
            self._tag_hint.setText(
                "Nothing to commit: tags the current commit, marking every "
                "document as last committed."
            )
        err = self._metadata_error()
        self._meta_error.setText(err)
        tag_only = self.tag_only()
        ok_btn.setText("Tag" if tag_only else "Commit")
        # The message is the new commit's; a tag on its own makes none.
        self._message.setEnabled(not tag_only)
        if tag_only:
            # A raised issue still needs a commit of its own to sit on.
            ok_btn.setEnabled(not err and not self._cuts_new_issue())
            return
        has_message = bool(self._message.toPlainText().strip())
        ok_btn.setEnabled(has_message and not err and self._has_changes())

    def message(self) -> str:
        """Final, trimmed commit message."""
        return self._message.toPlainText().strip()

    def issue(self) -> str:
        """The issue field as typed. The service cuts a tag only when it is
        non-empty and differs from the current issue."""
        return self._issue_edit.text().strip()

    def author(self) -> str:
        return self._author_edit.text().strip()

    def date(self) -> str:
        return self._date_edit.text().strip()

    def tag(self) -> str:
        """The Tag name as typed; empty means no tag."""
        return self._tag_edit.text().strip()

    def tag_message(self) -> str:
        return self._tag_message_edit.text().strip()
