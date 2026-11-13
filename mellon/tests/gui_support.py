"""Shared helpers for the MELLON-GUI-SRS blind tests (docs/specification.md, step 7).

Copies of the req-test fixture, files changed as another program would, git and
snapshot helpers, and an offscreen driver that uses the GUI as a user does: key
presses go to the focused widget of the active window, actions are triggered through
their QAction or the widget the user clicks, and modal dialogs never wait for input
(their exec and the QMessageBox, QFileDialog and QInputDialog calls answer for the
user). Fixtures defined here are imported by name into the test modules that use
them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pygit2
import pytest
from PySide6.QtCore import QCoreApplication, QStandardPaths, QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
    QTreeView,
    QWidget,
)


REQ_TEST = Path(__file__).resolve().parents[2] / "req-test"


SRS = "VCU3-SRS"


STS = "VCU3-STS"


SYRS = "VCU3-SyRS"


DOC_IDS = (SRS, STS, SYRS)


# --------------------------------------------------------------------------- repositories


def _copy_fixture(dest: Path) -> Path:
    from tests.conftest import _clean_worktree, _ensure_req_test_fixture

    _ensure_req_test_fixture()
    shutil.copytree(REQ_TEST, dest)
    _clean_worktree(dest)
    return dest


def _obj_file(repo: Path, doc: str, oid: str) -> Path:
    return repo / doc / "objects" / f"{oid}.xml"


def _manifest(repo: Path, doc: str) -> Path:
    return repo / doc / f"{doc}.xml"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _external_edit(path: Path, old: str, new: str) -> None:
    """Change a file as another program would, and make sure its size and time change."""
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path.name}"
    _write(path, text.replace(old, new))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))


def _head(repo: Path) -> str:
    return str(pygit2.Repository(str(repo)).head.target)


def _refs(repo: Path) -> dict[str, str]:
    r = pygit2.Repository(str(repo))
    out = {name: str(r.references[name].target) for name in r.references}
    out["HEAD"] = (repo / ".git" / "HEAD").read_text()
    return out


def _git_commit_all(repo: Path, message: str) -> None:
    r = pygit2.Repository(str(repo))
    r.index.add_all()
    r.index.write()
    tree = r.index.write_tree()
    sig = pygit2.Signature("Blind Tester", "blind@example.invalid")
    r.create_commit("HEAD", sig, sig, message, tree, [r.head.target])


def _snapshot(repo: Path) -> dict:
    """Bytes of every work-tree file outside .git, the folders, .git/index and every reference."""
    files, dirs = {}, []
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if rel.parts[0] == ".git":
            continue
        if p.is_dir():
            dirs.append(rel.as_posix())
        else:
            files[rel.as_posix()] = p.read_bytes()
    return {"files": files, "dirs": dirs, "index": (repo / ".git" / "index").read_bytes(), "refs": _refs(repo)}


def _snapshot_diff(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a["files"]) | set(b["files"])):
        if a["files"].get(k) != b["files"].get(k):
            out.append(f"file {k}")
    if a["dirs"] != b["dirs"]:
        out.append(f"folders {sorted(set(a['dirs']) ^ set(b['dirs']))}")
    if a["index"] != b["index"]:
        out.append(".git/index")
    if a["refs"] != b["refs"]:
        out.append(f"refs {a['refs']} -> {b['refs']}")
    return out


SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="#{c}"/></svg>\n'
)


def _shown(w) -> bool:
    """True while the window exists and is shown (a closed window may already be deleted)."""
    import shiboken6

    return shiboken6.isValid(w) and w.isVisible()


def _plain(text: str) -> str:
    return text.replace("&", "").replace("…", "").replace("...", "").strip()


# --------------------------------------------------------------------------- the GUI driver


class Gui:
    """Drives the GUI as a user does and answers every modal dialog."""

    def __init__(self, qapp, qtbot, monkeypatch, tmp_path):
        self.app = qapp
        self.bot = qtbot
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.log: list[tuple[str, str, str, str]] = []
        self.handlers: dict[str, object] = {}
        self.answers: dict[str, object] = {}
        self._patch_dialogs()

    # ---- dialogs ---------------------------------------------------------------------
    def _patch_dialogs(self):
        gui_gui = self
        real_dialog_exec = QDialog.exec
        real_box_exec = QMessageBox.exec
        self.real_dialog_exec = real_dialog_exec
        self.real_box_exec = real_box_exec

        def _text(d):
            t = d.text() if isinstance(d, QMessageBox) else ""
            if isinstance(d, QMessageBox):
                t += "\n" + d.informativeText()
            else:
                t += "\n".join(lab.text() for lab in d.findChildren(QLabel))
            return t

        def fake_exec(d):
            name = type(d).__name__
            gui_gui.log.append(("exec", name, d.windowTitle(), _text(d)))
            h = gui_gui.handlers.get(name)
            if h is not None:
                return h(d)
            if isinstance(d, QMessageBox):
                return gui_gui.answer_box(d, yes=False)
            return int(QDialog.DialogCode.Rejected)

        self.mp.setattr(QDialog, "exec", fake_exec)
        self.mp.setattr(QMessageBox, "exec", fake_exec)
        self.mp.setattr(QFileDialog, "exec", fake_exec)
        self.mp.setattr(QInputDialog, "exec", fake_exec)

        def box_static(kind, default):
            def f(*args, **kwargs):
                title = str(args[1]) if len(args) > 1 else ""
                text = str(args[2]) if len(args) > 2 else str(kwargs.get("text", ""))
                gui_gui.log.append((kind, "QMessageBox", title, text))
                a = gui_gui.answers.get(kind, default)
                return a(*args) if callable(a) else a
            return staticmethod(f)

        S = QMessageBox.StandardButton
        for kind, default in (("question", S.No), ("warning", S.Ok), ("information", S.Ok),
                              ("critical", S.Ok), ("about", None)):
            self.mp.setattr(QMessageBox, kind, box_static(kind, default))

        def file_static(kind, default):
            def f(*args, **kwargs):
                gui_gui.log.append((kind, "QFileDialog", str(args[1]) if len(args) > 1 else "", ""))
                a = gui_gui.answers.get(kind, default)
                return a(*args) if callable(a) else a
            return staticmethod(f)

        for kind, default in (("getExistingDirectory", ""), ("getOpenFileName", ("", "")),
                              ("getOpenFileNames", ([], "")), ("getSaveFileName", ("", ""))):
            self.mp.setattr(QFileDialog, kind, file_static(kind, default))
        for kind, default in (("getText", ("", False)), ("getItem", ("", False)), ("getInt", (0, False))):
            self.mp.setattr(QInputDialog, kind, file_static(kind, default))

    @staticmethod
    def answer_box(box: QMessageBox, *, yes: bool) -> int:
        S = QMessageBox.StandardButton
        wanted = (S.Yes, S.Ok, S.Save, S.Apply) if yes else (S.No, S.Cancel, S.Abort, S.Close)
        for std in wanted:
            b = box.button(std)
            if b is not None:
                b.click()
                return box.result()
        roles = (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole) if yes else (
            QMessageBox.ButtonRole.RejectRole, QMessageBox.ButtonRole.NoRole)
        for b in box.buttons():
            if box.buttonRole(b) in roles:
                b.click()
                return box.result()
        return int(S.Cancel)

    def opened(self, name: str | None = None) -> list[tuple[str, str, str, str]]:
        return [e for e in self.log if name is None or e[1] == name or e[0] == name]

    # ---- windows ---------------------------------------------------------------------
    def explorer(self, repo: Path | None = None):
        from mellon.gui.project_explorer_window import ProjectExplorerWindow

        pe = ProjectExplorerWindow()
        pe.show()
        if repo is not None:
            assert pe.open_repo(repo)
        self.bot.waitExposed(pe)
        self.activate(pe)
        return pe

    def activate(self, w):
        w.show()
        w.raise_()
        w.activateWindow()
        self.bot.waitUntil(lambda: w.isActiveWindow(), timeout=3000)
        self.app.processEvents()

    def reactivate(self, ed, other):
        """The user switches to another window and back, so `ed` is activated."""
        self.activate(other)
        self.activate(ed)

    @staticmethod
    def doc_tree(pe) -> QTreeView:
        return pe.findChild(QTreeView)

    @staticmethod
    def open_list(pe) -> list[str]:
        lw = pe.findChild(QListWidget)
        return [lw.item(i).text() for i in range(lw.count())]

    def listed_docs(self, pe) -> list[str]:
        m = self.doc_tree(pe).model()
        out = []
        for r in range(m.rowCount()):
            out.append(str(m.data(m.index(r, 0))).split(" ")[0])
        return out

    def doc_index(self, pe, doc_id):
        m = self.doc_tree(pe).model()
        for r in range(m.rowCount()):
            i = m.index(r, 0)
            if str(m.data(i)).split(" ")[0] == doc_id:
                return i
        raise AssertionError(f"{doc_id} not listed in the Project Explorer")

    @staticmethod
    def editors(doc_id: str | None = None):
        from mellon.gui.main_window import DocumentEditorWindow

        eds = [w for w in QApplication.topLevelWidgets() if isinstance(w, DocumentEditorWindow) and w.isVisible()]
        if doc_id is not None:
            eds = [e for e in eds if e.controller.current_doc_id() == doc_id]
        return eds

    def dclick(self, view, index):
        rect = view.visualRect(index)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        self.app.processEvents()

    def open_doc(self, pe, doc_id, *, editing=False):
        before = set(map(id, self.editors(doc_id)))
        tree = self.doc_tree(pe)
        self.activate(pe)
        self.dclick(tree, self.doc_index(pe, doc_id))
        self.bot.waitUntil(lambda: len(self.editors(doc_id)) >= 1, timeout=5000)
        eds = [e for e in self.editors(doc_id) if id(e) not in before] or self.editors(doc_id)
        ed = eds[0]
        self.bot.waitExposed(ed)
        self.activate(ed)
        if editing:
            self.enable_editing(ed)
        return ed

    def enable_editing(self, ed):
        btn = [b for b in ed.grid_pane.read_only_banner.findChildren(QPushButton) if "Enable editing" in b.text()]
        assert btn, "no Enable editing button on the read-only banner"
        self.bot.mouseClick(btn[0], Qt.MouseButton.LeftButton)
        self.app.processEvents()
        assert not self.is_read_only(ed), "Enable editing left the window read-only"

    @staticmethod
    def action(win, text: str) -> QAction:
        want = _plain(text)
        found = [a for a in win.findChildren(QAction) if _plain(a.text()) == want]
        assert found, f"no action {text!r} in {type(win).__name__}"
        return found[0]

    def is_read_only(self, ed) -> bool:
        a = self.action(ed, "Read-only")
        return a.isChecked()

    def toolbar_button(self, win, text: str) -> QToolButton:
        found = [b for b in win.findChildren(QToolButton) if _plain(b.text()) == _plain(text)]
        assert found, f"no toolbar button {text!r}"
        return found[0]

    def key(self, win, key, mods=Qt.KeyboardModifier.NoModifier, target=None):
        """Press a key as the user does: to the focused widget of the active window."""
        if not win.isActiveWindow():
            self.activate(win)
        w = target or QApplication.focusWidget() or win
        QTest.keyClick(w, key, mods)
        self.app.processEvents()

    # ---- grid ------------------------------------------------------------------------
    @staticmethod
    def table(ed):
        return ed.grid_pane.table

    def headers(self, ed) -> list[str]:
        m = self.table(ed).model()
        return [str(m.headerData(c, Qt.Orientation.Horizontal) or "") for c in range(m.columnCount())]

    def col(self, ed, header: str) -> int:
        h = self.headers(ed)
        assert header in h, f"no grid column {header!r} in {h}"
        return h.index(header)

    def grid_ids(self, ed) -> list[str]:
        tv = self.table(ed)
        m = tv.model()
        c = self.col(ed, "ID")
        return [m.data(m.index(r, c)) for r in range(m.rowCount()) if not tv.isRowHidden(r)]

    def row(self, ed, oid: str) -> int:
        m = self.table(ed).model()
        c = self.col(ed, "ID")
        for r in range(m.rowCount()):
            if m.data(m.index(r, c)) == oid:
                return r
        raise AssertionError(f"{oid} is not a grid row")

    def cell_rect(self, ed, oid, header="ID"):
        tv = self.table(ed)
        m = tv.model()
        idx = m.index(self.row(ed, oid), self.col(ed, header))
        tv.scrollTo(idx)
        self.app.processEvents()
        return tv.visualRect(idx)

    def click_cell(self, ed, oid, header="ID", mods=Qt.KeyboardModifier.NoModifier):
        tv = self.table(ed)
        rect = self.cell_rect(ed, oid, header)
        QTest.mouseClick(tv.viewport(), Qt.MouseButton.LeftButton, mods, rect.center())
        self.app.processEvents()

    def select(self, ed, oid):
        self.click_cell(ed, oid, "ID")
        assert ed.controller.current_obj_id() == oid, f"clicking {oid} selected {ed.controller.current_obj_id()}"

    def pause(self):
        """Wait longer than the double-click interval, so two clicks don't merge."""
        self.bot.wait(QApplication.doubleClickInterval() + 30)

    def open_cell_editor(self, ed, oid, header):
        """Select the row, then double-click the cell, as a user does."""
        tv = self.table(ed)
        if ed.controller.current_obj_id() != oid:
            self.select(ed, oid)
        rect = self.cell_rect(ed, oid, header)
        self.pause()
        QTest.mouseClick(tv.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        self.pause()
        QTest.mouseDClick(tv.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        self.app.processEvents()
        return QApplication.focusWidget()

    def selected_grid_ids(self, ed) -> list[str]:
        tv = self.table(ed)
        m = tv.model()
        c = self.col(ed, "ID")
        rows = sorted({i.row() for i in tv.selectionModel().selectedIndexes()})
        return [m.data(m.index(r, c)) for r in rows]

    # ---- inspector -------------------------------------------------------------------
    @staticmethod
    def insp_description(ed) -> QTextEdit:
        eds = [w for w in ed.inspector.findChildren(QTextEdit) if w.isVisible()]
        assert eds, "the Inspector shows no description field"
        return eds[0]

    @staticmethod
    def insp_field(ed, label: str) -> QWidget:
        """The field the Inspector shows next to `label:`."""
        for lab in ed.inspector.findChildren(QLabel):
            if lab.isVisible() and lab.text().strip() == f"{label}:":
                lay = lab.parentWidget().layout()
                if isinstance(lay, QFormLayout):
                    r, _ = lay.getWidgetPosition(lab)
                    item = lay.itemAt(r, QFormLayout.ItemRole.FieldRole)
                    return item.widget()
                if isinstance(lay, QGridLayout):
                    r, c, _, _ = lay.getItemPosition(lay.indexOf(lab))
                    return lay.itemAtPosition(r, c + 1).widget()
                kids = [lay.itemAt(i).widget() for i in range(lay.count())]
                if lab in kids:
                    return kids[kids.index(lab) + 1]
        raise AssertionError(f"the Inspector shows no field labelled {label!r}")

    @staticmethod
    def insp_texts(ed) -> list[str]:
        out = []
        for w in ed.inspector.findChildren(QWidget):
            if not w.isVisible():
                continue
            if isinstance(w, QLabel):
                out.append(w.text())
            elif isinstance(w, QLineEdit):
                out.append(w.text())
            elif isinstance(w, (QTextEdit, QPlainTextEdit)):
                out.append(w.toPlainText())
            elif isinstance(w, QComboBox):
                out.append(w.currentText())
            elif isinstance(w, QListWidget):
                for i in range(w.count()):
                    it = w.item(i)
                    out.append(it.text())
                    iw = w.itemWidget(it)
                    if iw is not None:
                        out.extend(lab.text() for lab in iw.findChildren(QLabel))
        return out

    # ---- outline ---------------------------------------------------------------------
    @staticmethod
    def outline_items(ed):
        """(display text, depth, index) of each outline-pane item, top to bottom."""
        view = ed.outline_pane.outline_view
        m = view.model()
        out = []

        def walk(parent, depth):
            for r in range(m.rowCount(parent)):
                i = m.index(r, 0, parent)
                out.append((str(m.data(i)), depth, i))
                walk(i, depth + 1)

        walk(view.rootIndex(), 0)
        return out

    def outline_index(self, ed, repo, doc, oid):
        first_line = _description(repo, doc, oid).splitlines()[0]
        for text, _, idx in self.outline_items(ed):
            if text.startswith(f"{oid}:") or text.rstrip().endswith(first_line):
                return idx
        raise AssertionError(f"{oid} not in the outline pane")

    @staticmethod
    def outline_selected(ed):
        view = ed.outline_pane.outline_view
        return view.selectionModel().selectedIndexes()

    # ---- status bar ------------------------------------------------------------------
    @staticmethod
    def status_texts(win) -> str:
        parts = [win.statusBar().currentMessage()]
        for lab in win.statusBar().findChildren(QLabel):
            parts.append(lab.text())
            parts.append(lab.toolTip())
        return "\n".join(parts)

    def close_all(self):
        for w in list(QApplication.topLevelWidgets()):
            if _shown(w):
                w.close()
        self.app.processEvents()
        from PySide6.QtCore import QThreadPool

        QThreadPool.globalInstance().waitForDone(10000)
        self.app.processEvents()


def _description(repo: Path, doc: str, oid: str) -> str:
    text = _obj_file(repo, doc, oid).read_text(encoding="utf-8")
    m = re.search(r"<description>(.*?)</description>", text, re.S)
    return (m.group(1) if m else "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _outline(repo: Path, doc: str) -> list[tuple[str, str | None]]:
    """(object ID, parent object ID) for each object, in outline order, read from the manifest."""
    from lxml import etree

    root = etree.parse(str(_manifest(repo, doc))).getroot()
    out = []

    def walk(el, parent):
        for ref in el.findall("ref"):
            out.append((ref.get("id"), parent))
            walk(ref, ref.get("id"))

    walk(root.find("outline"), None)
    return out


@pytest.fixture
def repo(tmp_path):
    return _copy_fixture(tmp_path / "repo")


@pytest.fixture
def gui_gui(qapp, qtbot, monkeypatch, tmp_path):
    g = Gui(qapp, qtbot, monkeypatch, tmp_path)
    yield g
    g.close_all()


# --------------------------------------------------------------------------- the entry point


@pytest.fixture
def run_main(qapp, monkeypatch, tmp_path):
    """Run the GUI's entry point in-process.

    `scenario(result)` runs inside the GUI's event loop; without one the loop returns at once and
    leaves the windows open. The organisation name is made unique so that an error log written by
    a test lands in a throwaway folder, which is removed afterwards.
    """
    from mellon.gui.theme import ThemeManager

    org, app_name = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    unique = f"mellon-blindtest-{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(ThemeManager, "SETTINGS_ORG", unique)
    real_exec = QApplication.exec
    state = {"exec_calls": 0}

    def run(argv, scenario=None, timeout_ms=20000):
        result: dict = {}

        def fake_exec(*_):
            state["exec_calls"] += 1
            if scenario is None:
                return 0

            def guard():
                result["timed_out"] = True
                for w in QApplication.topLevelWidgets():
                    w.close()
                QApplication.exit(99)

            QTimer.singleShot(0, lambda: scenario(result))
            t = QTimer()
            t.setSingleShot(True)
            t.timeout.connect(guard)
            t.start(timeout_ms)
            rc = real_exec()
            t.stop()
            return rc

        monkeypatch.setattr(QApplication, "exec", fake_exec)
        from mellon.gui.app import main

        try:
            rc = main(argv)
        except SystemExit as e:  # an argparse-style exit is an exit status too
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            monkeypatch.setattr(QApplication, "exec", real_exec)
        return rc, result

    run.state = state
    run.org = unique
    yield run
    for w in list(QApplication.topLevelWidgets()):
        w.close()
    qapp.processEvents()
    QCoreApplication.setOrganizationName(org)
    QCoreApplication.setApplicationName(app_name)
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
    shutil.rmtree(base / unique, ignore_errors=True)


def _visible_explorers():
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    return [w for w in QApplication.topLevelWidgets() if isinstance(w, ProjectExplorerWindow) and w.isVisible()]


def _gui_script() -> list[str]:
    found = shutil.which("mellon-gui", path=str(Path(sys.executable).parent))
    assert found, "the mellon-gui command is not installed in the environment"
    return [found]


def _mcp_script() -> list[str]:
    found = shutil.which("mellon-mcp", path=str(Path(sys.executable).parent))
    assert found, "the mellon-mcp command is not installed in the environment"
    return [found]


def _offscreen_env(**extra) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.update(extra)
    return env


@pytest.fixture(scope="module")
def gui_version_run():
    t0 = time.monotonic()
    p = subprocess.run(_gui_script() + ["--version"], capture_output=True, timeout=60, env=_offscreen_env())
    return p, time.monotonic() - t0


_RELEASE_NUMBER = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")


def _explorer_of_main():
    pes = _visible_explorers()
    assert len(pes) == 1
    return pes[0]


# --------------------------------------------------------------------------- draw.io absent


def _commit_accepting(gui_gui):
    gui_gui.handlers["CommitDialog"] = lambda d: int(QDialog.DialogCode.Accepted)


def _doc_dirty(repo: Path, doc: str) -> list[str]:
    st = pygit2.Repository(str(repo)).status()
    return [k for k, v in st.items() if k.startswith(doc + "/") and v != pygit2.GIT_STATUS_IGNORED]


def _add_table(repo: Path, oid: str):
    from mellon.schema import Table, TableCell, TableColumn, TableRow
    from mellon.service.document import DocumentService

    DocumentService(repo).set_table(SRS, oid, Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("Signal"), TableCell("Limit")), header=True),
              TableRow(cells=(TableCell("a"), TableCell("b"))))))


def _edit_attribute(gui_gui, ed, oid, text):
    editor = gui_gui.open_cell_editor(ed, oid, "Rationale")
    QTest.keyClicks(editor, text)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    gui_gui.app.processEvents()


def _edit_description(gui_gui, ed, oid, text):
    editor = gui_gui.open_cell_editor(ed, oid, "Description")
    QTest.keyClick(editor, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(editor, text)
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    gui_gui.app.processEvents()


def _system_columns_ok(gui_gui, ed) -> list[str]:
    tv = gui_gui.table(ed)
    hh = tv.horizontalHeader()
    bad = []
    headers = gui_gui.headers(ed)
    for name in ("", "ID", "Description"):  # the state gutter has no heading
        c = headers.index(name)
        if tv.isColumnHidden(c) or hh.isSectionHidden(c) or hh.sectionSize(c) <= 0:
            bad.append(name or "state gutter")
    return bad


def _choose_view(gui_gui, ed, name):
    menu = gui_gui.toolbar_button(ed, "Views").menu()
    menu.aboutToShow.emit()
    pick = [a for a in menu.actions() if _plain(a.text()) == name]
    assert pick, [a.text() for a in menu.actions()]
    pick[0].trigger()
    gui_gui.app.processEvents()


def _make_filter(repo, name, value):
    from mellon.schema import FilterRule
    from mellon.service.document import DocumentService

    DocumentService(repo).create_filter(SRS, name, rules=[FilterRule(kind="attr", attr="rationale", op="contains",
                                                                     value=value)])


def _choose_filter(gui_gui, ed, name):
    menu = gui_gui.toolbar_button(ed, "Filters").menu()
    menu.aboutToShow.emit()
    pick = [a for a in menu.actions() if _plain(a.text()) == name]
    assert pick, [a.text() for a in menu.actions()]
    pick[0].trigger()
    gui_gui.app.processEvents()


def _open_menu(gui_gui, ed):
    """Open the Document Editor's Menu, as the user does before picking one of its actions: the menu
    sets which of its actions are enabled as it opens."""
    gui_gui.toolbar_button(ed, "Menu").menu().aboutToShow.emit()
    gui_gui.app.processEvents()


# --------------------------------------------------------------------------- changes on disk


def _change_elsewhere(path: Path, words: str = " Changed elsewhere.") -> None:
    """Another program adds words to the end of an object's description."""
    _external_edit(path, "</description>", f"{words}</description>")


def _stale_shown(ed) -> bool:
    return ed.grid_pane.stale_banner.isVisible()


def _reactivated(gui_gui, ed, other):
    """The user switches to another window and back; give the activation check time to finish."""
    gui_gui.reactivate(ed, other)
    gui_gui.bot.wait(150)
    gui_gui.app.processEvents()


def _read_only_banner_shown(ed) -> bool:
    return ed.grid_pane.read_only_banner.isVisible()


def _uncommitted_change(repo: Path) -> None:
    """Something for a commit of VCU3-SRS to record, made before the Document Editor reads the document."""
    _external_edit(_obj_file(repo, SRS, "VCU3-SRS-11"), "3 degC", "4 degC")


def _commit_from(gui_gui, ed, change=None, in_dialog=False):
    """Run Commit and accept the commit dialog. `change`, if given, runs as another program's change: just
    before Commit, or while the commit dialog is open. The window isn't activated in between."""

    def accept(d):
        if change is not None and in_dialog:
            change()
        return int(QDialog.DialogCode.Accepted)

    gui_gui.handlers["CommitDialog"] = accept
    if change is not None and not in_dialog:
        change()
    gui_gui.action(ed, "Commit").trigger()
    gui_gui.bot.wait(300)


def _attach_figure_and_commit(repo: Path, oid: str, colour: str = "0000ff") -> Path:
    from mellon.service.document import DocumentService

    src = repo.parent / "figure-source.svg"
    _write(src, SVG.format(c=colour))
    DocumentService(repo).set_figure(SRS, oid, src)
    _git_commit_all(repo, f"Attach a figure to {oid}")
    fig = repo / SRS / "figures" / f"{oid}.svg"
    assert fig.is_file(), "precondition: the figure is attached"
    return fig


def _grid_text(gui_gui, ed, oid, header="Description") -> str:
    m = gui_gui.table(ed).model()
    return str(m.data(m.index(gui_gui.row(ed, oid), gui_gui.col(ed, header))) or "")


def _press_in_grid(gui_gui, ed, oid, key, mods=Qt.KeyboardModifier.NoModifier):
    if oid is not None:
        gui_gui.select(ed, oid)
    gui_gui.table(ed).setFocus()
    gui_gui.app.processEvents()
    gui_gui.key(ed, key, mods)


_CTRL = Qt.KeyboardModifier.ControlModifier


_SHIFT = Qt.KeyboardModifier.ShiftModifier


_ALT = Qt.KeyboardModifier.AltModifier


def _rationale(repo, oid) -> str:
    m = re.search(r'<attr name="rationale">(.*?)</attr>', _obj_file(repo, SRS, oid).read_text(encoding="utf-8"))
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- input paths: banner and read-only mode


def _svg_file(repo: Path) -> Path:
    path = repo.parent / "picked.svg"
    _write(path, SVG.format(c="00aa00"))
    return path


def _answer_yes(gui_gui):
    """Confirm every question, so that an input that isn't stopped goes all the way."""
    gui_gui.answers["question"] = QMessageBox.StandardButton.Yes
    gui_gui.handlers["QMessageBox"] = lambda b: gui_gui.answer_box(b, yes=True)


def _paste_in_cell_editor(gui_gui, ed, oid="VCU3-SRS-2", text=" Pasted text."):
    """Paste at the end of the description in the grid's description editor, and save it with Ctrl+Enter.
    (Ctrl+V on a grid cell with no editor open does nothing in 0.3.0.)"""
    QApplication.clipboard().setText(text)
    w = gui_gui.open_cell_editor(ed, oid, "Description")
    target = w if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit)) else gui_gui.table(ed)
    QTest.keyClick(target, Qt.Key.Key_End, _CTRL)
    QTest.keyClick(target, Qt.Key.Key_V, _CTRL)
    QTest.keyClick(target, Qt.Key.Key_Return, _CTRL)
    gui_gui.app.processEvents()


def _drag_column(gui_gui, ed):
    """Drag the Status column of the active view in front of Priority."""
    hh = gui_gui.table(ed).horizontalHeader()
    h = gui_gui.headers(ed)
    hh.moveSection(hh.visualIndex(h.index("Status")), hh.visualIndex(h.index("Priority")))
    gui_gui.app.processEvents()


def _attach_figure_by_key(gui_gui, ed, svg, oid="VCU3-SRS-2"):
    gui_gui.answers["getOpenFileName"] = (str(svg), "SVG files (*.svg)")
    _press_in_grid(gui_gui, ed, oid, Qt.Key.Key_G, _CTRL)


def _add_link_by_key(gui_gui, ed, oid="VCU3-SRS-2"):
    def pick(d):
        boxes = [w for w in d.findChildren(QLineEdit) if w.text() == ""]
        if boxes:
            boxes[0].setText("VCU3-SyRS-1")
        return int(QDialog.DialogCode.Accepted)

    gui_gui.handlers["LinkPicker"] = pick
    _press_in_grid(gui_gui, ed, oid, Qt.Key.Key_L, _CTRL)


def _edit_properties(gui_gui, ed):
    def change_title(d):
        title = [w for w in d.findChildren(QLineEdit) if w.text() == "VCU3 Software Requirements Specification"]
        apply = [b for b in d.findChildren(QPushButton) if _plain(b.text()) == "Apply"]
        if title and apply:
            title[0].setText("Retitled elsewhere")
            apply[0].click()
            gui_gui.app.processEvents()
        return int(QDialog.DialogCode.Rejected)

    gui_gui.handlers["DocumentPropertiesDialog"] = change_title
    gui_gui.action(ed, "Properties").trigger()
    gui_gui.app.processEvents()


# --------------------------------------------------------------------------- export running


def _fake_tectonic(folder: Path, delay_s: int) -> Path:
    """A stand-in for the typesetter: waits `delay_s` seconds, then writes a PDF beside each .tex it is given.
    Export finds it through MELLON_TECTONIC."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = folder / "tectonic.bat"
        _write(path, f'@echo off\nping -n {delay_s + 1} 127.0.0.1 >nul\n'
                     'for %%a in (%*) do if /i "%%~xa"==".tex" copy /y "%%~fa" "%%~dpna.pdf" >nul\nexit /b 0\n')
    else:
        path = folder / "tectonic"
        _write(path, f'#!/bin/sh\nsleep {delay_s}\nfor a in "$@"; do case "$a" in *.tex) cp "$a" "${{a%.tex}}.pdf";; '
                     'esac; done\nexit 0\n')
        path.chmod(0o755)
    return path


def _start_export(gui_gui, ed, out: Path):
    """Ctrl+E, Export anyway past the suspect-link warning, and save to `out`."""
    gui_gui.handlers["QMessageBox"] = lambda b: gui_gui.answer_box(b, yes=True)
    gui_gui.answers["getSaveFileName"] = (str(out), "PDF files (*.pdf)")
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_E, _CTRL)
