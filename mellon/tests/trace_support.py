"""Shared helpers for the MELLON-TRACE-SRS blind tests (docs/specification.md, step 7).

Repositories built on disk under pytest's tmp_path in the on-disk format of USAGE.md,
with pygit2 for the history; readers for links files, acknowledged commits and link
states; the in-process MCP server and the server over stdio; a recorder of the paths
Mellon asks the operating system for; and an offscreen driver for the Document Editor,
the Inspector and the Project Explorer. Fixtures defined here are imported by name into
the test modules that use them.
"""

from __future__ import annotations

import builtins
import html
import io
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pygit2
import pytest
from lxml import etree

import mellon
from mellon.link_index import from_scratch
from mellon.repo import load_all
from mellon.service.document import DocumentService
from mellon.suspects import scan_document


NAME, EMAIL = "Blind Tester", "blind@example.invalid"


T0 = 1_700_000_000


# ---------------------------------------------------------------------------
# On-disk documents (format from USAGE.md, "On-disk format")
# ---------------------------------------------------------------------------

STATUS_OPTIONS = ("Draft", "Approved")


def attributes_xml(status_options=STATUS_OPTIONS) -> str:
    opts = "".join(f"      <option>{o}</option>\n" for o in status_options)
    return (
        "  <attributes>\n"
        '    <attribute name="type" kind="enum" required="true">\n'
        "      <option>Heading</option>\n"
        "      <option>Requirement</option>\n"
        "      <option>Test Case</option>\n"
        "    </attribute>\n"
        '    <attribute name="status" kind="enum" required="false">\n'
        f"{opts}"
        "    </attribute>\n"
        "  </attributes>\n"
    )


def outline_xml(items, depth=2) -> str:
    out = ""
    pad = "  " * depth
    for it in items:
        if isinstance(it, tuple):
            ref, kids = it
            out += f'{pad}<ref id="{ref}">\n{outline_xml(kids, depth + 1)}{pad}</ref>\n'
        else:
            out += f'{pad}<ref id="{it}"/>\n'
    return out


def manifest_xml(doc_id, outline, parents=(), title=None, status_options=STATUS_OPTIONS) -> str:
    par = "".join(f'    <parent doc="{p}"/>\n' for p in parents)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<document id="{doc_id}">\n'
        "  <metadata>\n"
        f"    <title>{title or doc_id + ' title'}</title>\n"
        "    <author>tester</author>\n"
        "    <date>2026-11-01</date>\n"
        "  </metadata>\n"
        f"  <parents>\n{par}  </parents>\n"
        f"{attributes_xml(status_options)}"
        f"  <outline>\n{outline_xml(outline)}  </outline>\n"
        "</document>\n"
    )


def object_xml(obj_id, description, type_="Requirement", status=None) -> str:
    st = f'  <attr name="status">{status}</attr>\n' if status else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<object id="{obj_id}">\n'
        f'  <attr name="type">{type_}</attr>\n'
        f"{st}"
        f"  <description>{description}</description>\n"
        "</object>\n"
    )


def links_xml(source, items) -> str:
    """items: (target_doc, target_id, type, ack or None)."""
    rows = ""
    for tdoc, tid, typ, ack in items:
        a = f' ack="{ack}"' if ack else ""
        rows += f'  <link target-doc="{tdoc}" target-id="{tid}" type="{typ}"{a}/>\n'
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<links source="{source}">\n{rows}</links>\n'


SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    '<rect width="20" height="10" fill="red"/></svg>\n'
)


class Repo:
    """A throwaway git repository holding documents, built file by file."""

    def __init__(self, root: Path, *, git: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.git = pygit2.init_repository(str(self.root)) if git else None
        self._t = T0

    # files ---------------------------------------------------------------
    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink()

    def add_doc(self, doc_id, objects: dict, parents=(), outline=None, title=None) -> None:
        """objects: obj_id -> description, or (description, type), or (description, type, status)."""
        self.write(f"{doc_id}/{doc_id}.xml", manifest_xml(doc_id, outline or list(objects), parents, title))
        for oid, spec in objects.items():
            self.set_object(doc_id, oid, spec)

    def set_object(self, doc_id, oid, spec) -> None:
        if isinstance(spec, str):
            spec = (spec,)
        self.write(f"{doc_id}/objects/{oid}.xml", object_xml(oid, *spec))

    def set_manifest(self, doc_id, outline, parents=(), title=None, status_options=STATUS_OPTIONS) -> None:
        self.write(f"{doc_id}/{doc_id}.xml", manifest_xml(doc_id, outline, parents, title, status_options))

    def set_links(self, doc_id, source_id, items) -> None:
        rel = f"{doc_id}/links/{source_id}.xml"
        if items:
            self.write(rel, links_xml(source_id, items))
        elif (self.root / rel).exists():
            self.delete(rel)

    # git -----------------------------------------------------------------
    def commit(self, msg, *, parents=None, branch=None, when=None, modes=None) -> str:
        """Commit the whole working copy; return the full hash.

        `parents` defaults to HEAD; `branch` (a ref name) defaults to the
        branch HEAD names; `modes` sets index file modes for this commit.
        """
        repo = self.git
        idx = repo.index
        idx.read()
        for e in list(idx):
            if not (self.root / e.path).exists():
                idx.remove(e.path)
        idx.add_all()
        for path, mode in (modes or {}).items():
            e = idx[path]
            idx.add(pygit2.IndexEntry(path, e.id, mode))
        idx.write()
        tree = idx.write_tree()
        if parents is None:
            parents = [] if repo.head_is_unborn else [str(repo.head.target)]
        if when is None:
            self._t += 60
            when = self._t
        sig = pygit2.Signature(NAME, EMAIL, when, 0)
        oid = repo.create_commit(None, sig, sig, msg, tree, [pygit2.Oid(hex=p) for p in parents])
        ref = branch or repo.references["HEAD"].target
        if ref in repo.references:
            repo.references[ref].set_target(oid)
        else:
            repo.references.create(ref, oid)
        return str(oid)

    def head(self) -> str:
        return str(self.git.head.target)


def basic_repo(tmp_path, name="repo") -> Repo:
    """PARENT (a heading and three requirements) and CHILD (three requirements, parent PARENT), committed as 'seed'."""
    r = Repo(tmp_path / name)
    r.add_doc("PARENT", {
        "PARENT-0": ("Parent heading", "Heading"),
        "PARENT-1": "parent one",
        "PARENT-2": "parent two",
        "PARENT-3": "parent three",
    })
    r.add_doc("CHILD", {"CHILD-1": "child one", "CHILD-2": "child two", "CHILD-3": "child three"}, parents=["PARENT"])
    r.c0 = r.commit("seed")
    return r


def remove_from_working_copy(r: Repo, doc_id: str, obj_id: str, outline: list) -> None:
    """Delete an object's file and outline entry on disk, as another program would, without committing."""
    r.delete(f"{doc_id}/objects/{obj_id}.xml")
    r.set_manifest(doc_id, outline)


# ---------------------------------------------------------------------------
# Reading results
# ---------------------------------------------------------------------------


def disk_links(root, doc_id, source_id) -> dict:
    """(target_doc, target_id) -> (type, ack) for one links file, parsed independently of Mellon."""
    p = Path(root) / doc_id / "links" / f"{source_id}.xml"
    if not p.exists():
        return {}
    t = etree.parse(str(p)).getroot()
    return {(e.get("target-doc"), e.get("target-id")): (e.get("type"), e.get("ack")) for e in t.iter("link")}


def all_acks(root) -> dict:
    """(doc, source, target_doc, target_id) -> ack for every links file in the repository."""
    out = {}
    for f in sorted(Path(root).glob("*/links/*.xml")):
        t = etree.parse(str(f)).getroot()
        for e in t.iter("link"):
            out[(f.parent.parent.name, t.get("source"), e.get("target-doc"), e.get("target-id"))] = e.get("ack")
    return out


def snapshot(root) -> dict:
    """Every working-copy file outside .git, as bytes."""
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def statuses(root, doc_id, *, scratch: bool) -> list:
    docs = load_all(root)
    ld = docs[doc_id]
    if scratch:
        with from_scratch():
            return scan_document(ld.document, ld.folder)
    return scan_document(ld.document, ld.folder)


def link_status(root, doc_id, source_id, target_doc, target_id, *, scratch: bool):
    hits = [
        s for s in statuses(root, doc_id, scratch=scratch)
        if s.source_id == source_id and s.link.target_doc == target_doc and s.link.target_id == target_id
    ]
    assert len(hits) == 1, f"expected one status for {source_id}->{target_doc}/{target_id}, got {hits}"
    return hits[0]


# ---------------------------------------------------------------------------
# MCP, in-process and over stdio
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp(monkeypatch):
    """Point the in-process MCP server at a repository, as the `mcp_repo` fixture does."""
    from mellon.mcp import server

    class Handle:
        def serve(self, root, read_only=False):
            monkeypatch.setattr(server._State, "repo_root", Path(root))
            monkeypatch.setattr(server._State, "service", None)
            monkeypatch.setattr(server._State, "read_only", read_only)
            return server

        def restart(self):
            """A new server process on the same root: nothing kept in memory."""
            monkeypatch.setattr(server._State, "service", None)

    return Handle()


def call(fn, *a, **k) -> object:
    """A tool call; an exception counts as a refusal, as an MCP client sees it."""
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001 - any raised error is the server refusing
        return {"ok": False, "raised": f"{type(e).__name__}: {e}"}


def refused(result) -> bool:
    return isinstance(result, dict) and result.get("ok") is False


# ---------------------------------------------------------------------------
# Recording which paths are opened (MELLON-TRACE-SRS-77, -79)
# ---------------------------------------------------------------------------


def _under(path: str, base: str) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(base))
    return p == b or p.startswith(b.rstrip("\\/") + os.sep)


@contextmanager
def recorded_paths(root):
    """Record each path given to Python's file APIs, lxml's parse and pygit2's
    Repository while the block runs. Paths outside `root` (other than the
    Python installation and Mellon's own package) are refused with
    FileNotFoundError, so nothing outside is read. Yields (all, outside).

    Not caught: files that a C library opens by itself without being given a
    path through these calls.
    """
    root_s = str(Path(root).resolve())
    allowed = {os.path.abspath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    allowed.add(str(Path(mellon.__file__).resolve().parent))
    # Resolving a path inside the repository looks up each folder above it
    # with lstat on Linux (os.path.realpath). That reads a folder's entry in
    # its parent and opens nothing, so lstat of the root's own ancestors is
    # allowed; opening, listing or stat-ing one is still refused.
    ancestors = {os.path.normcase(str(p)) for p in Path(root_s).parents}
    seen: list[str] = []
    outside: list[str] = []

    def note(p, *, lstat=False):
        if isinstance(p, int) or p is None:
            return
        try:
            s = os.fsdecode(p)
        except TypeError:
            return
        seen.append(s)
        if lstat and os.path.normcase(os.path.abspath(s)) in ancestors:
            return
        if not _under(s, root_s) and not any(_under(s, a) for a in allowed):
            outside.append(s)
            raise FileNotFoundError(2, "refused by the test: outside the repository", s)

    def wrap(fn, lstat=False):
        def inner(p, *a, **k):
            note(p, lstat=lstat)
            return fn(p, *a, **k)
        return inner

    patches = []

    def patch(obj, name, new):
        patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, new)

    for obj, name in [
        (builtins, "open"), (io, "open"), (os, "open"), (os, "stat"), (os, "lstat"),
        (os, "scandir"), (os, "listdir"), (os.path, "exists"), (os.path, "isfile"),
        (os.path, "isdir"), (os.path, "islink"), (os.path, "getsize"), (os.path, "getmtime"),
        (os.path, "realpath"), (etree, "parse"), (pygit2, "discover_repository"),
    ]:
        patch(obj, name, wrap(getattr(obj, name), lstat=(obj is os and name == "lstat")))
    orig_repo_init = pygit2.Repository.__init__

    def repo_init(self, path=None, *a, **k):
        note(path)
        return orig_repo_init(self, path, *a, **k)

    patch(pygit2.Repository, "__init__", repo_init)
    try:
        yield seen, outside
    finally:
        for obj, name, old in reversed(patches):
            setattr(obj, name, old)


# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------


def _qt():
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets
    return QtCore, QtGui, QtWidgets, QtTest


def open_editor(qtbot, root, doc_id, *, editable=True, register=True):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.main_window import DocumentEditorWindow

    w = DocumentEditorWindow(doc_id, DocumentService(root, cache=True))
    if register:
        qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(100)
    if editable:
        click_button(qtbot, w, "Enable editing")
    return w


def click_button(qtbot, w, text):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    buttons = [b for b in w.findChildren(QtWidgets.QAbstractButton) if b.text() == text and b.isVisible()]
    assert len(buttons) == 1, f"expected one visible {text!r} button, found {len(buttons)}"
    QtTest.QTest.mouseClick(buttons[0], QtCore.Qt.LeftButton)
    qtbot.wait(100)


def grid_view(w):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    views = [v for v in w.grid_pane.findChildren(QtWidgets.QTableView) if v.isVisible()]
    assert len(views) == 1, "expected one visible grid"
    return views[0]


def _column(view, header):
    QtCore = _qt()[0]
    m = view.model()
    for c in range(m.columnCount()):
        if m.headerData(c, QtCore.Qt.Horizontal, QtCore.Qt.DisplayRole) == header:
            return c
    raise AssertionError(f"no {header!r} column")


def grid_ids(w) -> list:
    v = grid_view(w)
    m = v.model()
    c = _column(v, "ID")
    return [m.data(m.index(r, c)) for r in range(m.rowCount())]


def grid_index(w, obj_id, column="ID"):
    v = grid_view(w)
    m = v.model()
    c = _column(v, column) if isinstance(column, str) else column
    idc = _column(v, "ID")
    for r in range(m.rowCount()):
        if m.data(m.index(r, idc)) == obj_id:
            return v, m.index(r, c)
    raise AssertionError(f"{obj_id} is not in the grid")


def click_row(qtbot, w, obj_id):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    v, idx = grid_index(w, obj_id)
    v.scrollTo(idx)
    QtTest.QTest.mouseClick(v.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, v.visualRect(idx).center())
    qtbot.wait(100)


def selected_ids(w) -> list:
    v = grid_view(w)
    c = _column(v, "ID")
    return sorted(v.model().data(i) for i in v.selectionModel().selectedRows(c))


def inspector_lists(w):
    """The Inspector's link lists, top to bottom: outgoing, then incoming."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    lists = list(w.inspector.findChildren(QtWidgets.QListWidget))
    lists.sort(key=lambda lw: lw.mapTo(w.inspector, QtCore.QPoint(0, 0)).y())
    assert len(lists) == 2, f"expected the outgoing and incoming lists, found {len(lists)}"
    return lists


def row_text(lw, i) -> str:
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    item = lw.item(i)
    parts = [item.text()]
    iw = lw.itemWidget(item)
    if iw is not None:
        if isinstance(iw, QtWidgets.QLabel):
            parts.append(iw.text())
        parts += [lab.text() for lab in iw.findChildren(QtWidgets.QLabel)]
    return html.unescape(re.sub(r"<[^>]+>", "", " ".join(parts)))


def find_row(lw, needle) -> int:
    rows = [i for i in range(lw.count()) if needle in row_text(lw, i)]
    assert len(rows) == 1, f"expected one row naming {needle}, rows: {[row_text(lw, i) for i in range(lw.count())]}"
    return rows[0]


def context_menu(qtbot, fire, choose=None):
    """Open a context menu with `fire`; return its action texts. With `choose`,
    activate the first action whose text starts with it, as a user does."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    seen = []
    tries = [0]

    def poll():
        pw = QtWidgets.QApplication.activePopupWidget()
        if isinstance(pw, QtWidgets.QMenu):
            acts = [a for a in pw.actions() if not a.isSeparator()]
            seen.append([a.text() for a in acts])
            target = next((a for a in acts if choose and a.text().startswith(choose)), None)
            if target is not None and target.isEnabled():
                pw.setActiveAction(target)
                QtTest.QTest.keyClick(pw, QtCore.Qt.Key_Return)
                if QtWidgets.QApplication.activePopupWidget() is pw:
                    pw.close()
            else:
                pw.close()
        elif tries[0] < 100:
            tries[0] += 1
            QtCore.QTimer.singleShot(20, poll)

    QtCore.QTimer.singleShot(0, poll)
    fire()
    qtbot.wait(100)
    return seen[0] if seen else None


def link_row_menu(qtbot, lw, i, choose=None):
    rect = lw.visualItemRect(lw.item(i))
    return context_menu(qtbot, lambda: lw.customContextMenuRequested.emit(rect.center()), choose)


class Modals:
    """Answer each modal dialog while active: message boxes with Yes/OK,
    other dialogs with accept() or reject(). Records what it saw."""

    def __init__(self, accept_dialogs=False):
        QtCore, QtGui, QtWidgets, QtTest = _qt()
        self.seen = []
        self._accept = accept_dialogs
        self._tried = {}
        self.timer = QtCore.QTimer()
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self):
        QtCore, QtGui, QtWidgets, QtTest = _qt()
        mw = QtWidgets.QApplication.activeModalWidget()
        if mw is None:
            return
        key = id(mw)
        n = self._tried.get(key, 0)
        self._tried[key] = n + 1
        if n == 0:
            self.seen.append(f"{type(mw).__name__}: {mw.windowTitle()}")
        if isinstance(mw, QtWidgets.QMessageBox) and n == 0:
            for sb in (QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.Ok, QtWidgets.QMessageBox.Save):
                btn = mw.button(sb)
                if btn is not None:
                    btn.click()
                    return
            mw.accept()
        elif isinstance(mw, QtWidgets.QFileDialog) or not self._accept or n > 0:
            mw.reject()
        else:
            mw.accept()

    def stop(self):
        self.timer.stop()


# ===========================================================================
# Creating links (MELLON-TRACE-SRS-4, -6, -7, -14, -19, -24)
# ===========================================================================


def _uncommitted_parent_5(r: Repo) -> None:
    r.set_object("PARENT", "PARENT-5", "parent five, never committed")
    r.set_manifest("PARENT", ["PARENT-0", "PARENT-1", "PARENT-2", "PARENT-3", "PARENT-5"])


MISSING_TARGETS = {
    # target id -> how it is missing from the working copy
    "PARENT-99": "never existed",
    "PARENT-2": "committed, deleted from the working copy",
}


def _make_missing(r: Repo, target: str) -> None:
    if target == "PARENT-2":
        remove_from_working_copy(r, "PARENT", "PARENT-2", ["PARENT-0", "PARENT-1", "PARENT-3"])


# ===========================================================================
# Incoming links (MELLON-TRACE-SRS-33)
# ===========================================================================


def _three_document_repo(tmp_path) -> Repo:
    r = Repo(tmp_path / "repo")
    r.add_doc("PARENT", {"PARENT-1": "one", "PARENT-2": "two", "PARENT-3": "three"})
    r.add_doc("CHILD", {"CHILD-1": "c1", "CHILD-2": "c2"}, parents=["PARENT"])
    r.add_doc("SIB", {"SIB-1": "s1"}, parents=["PARENT"])
    r.set_links("PARENT", "PARENT-2", [("PARENT", "PARENT-1", "refines", None)])
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", None), ("PARENT", "PARENT-3", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-3", "verifies", None)])
    r.set_links("SIB", "SIB-1", [("PARENT", "PARENT-1", "verifies", None)])
    r.commit("seed")
    return r


EXPECTED_INCOMING_PARENT_1 = sorted([
    ("PARENT", "PARENT-2", "refines"), ("CHILD", "CHILD-1", "satisfies"), ("SIB", "SIB-1", "verifies")])


# ===========================================================================
# Link states (MELLON-TRACE-SRS-45)
# ===========================================================================


def _repo_with_never_committed_target(tmp_path) -> Repo:
    r = basic_repo(tmp_path)
    r.set_object("PARENT", "PARENT-43", "written, never committed")
    r.set_manifest("PARENT", ["PARENT-0", "PARENT-1", "PARENT-2", "PARENT-3", "PARENT-43"])
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-43", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-1", "satisfies", None)])
    r.set_links("CHILD", "CHILD-3", [("PARENT", "PARENT-44", "satisfies", None)])
    return r


def _outside_repo(tmp_path) -> Repo:
    base = tmp_path / "w"
    (base / "OUT" / "objects").mkdir(parents=True)
    (base / "OUT" / "OUT.xml").write_text(manifest_xml("OUT", ["OUT-1"]), encoding="utf-8")
    (base / "OUT" / "objects" / "OUT-1.xml").write_text(object_xml("OUT-1", "outside"), encoding="utf-8")
    (base / "OUT-1.xml").write_text(object_xml("OUT-1", "outside"), encoding="utf-8")
    r = Repo(base / "repo")
    r.add_doc("PARENT", {"PARENT-1": "parent one", "PARENT-42": "parent 42"})
    r.add_doc("CHILD", {"CHILD-1": "c1", "CHILD-2": "c2", "CHILD-3": "c3"}, parents=["PARENT"])
    r.set_links("CHILD", "CHILD-1", [("../OUT", "OUT-1", "satisfies", None), ("PARENT", "PARENT-1", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-42/../../../../OUT-1", "satisfies", None)])
    r.set_links("CHILD", "CHILD-3", [("//127.0.0.1/mellon-test-share", "SHARE-1", "satisfies", None)])
    r.set_links("PARENT", "PARENT-42", [("../OUT", "OUT-1", "refines", None)])
    r.commit("links that reach outside")
    return r


OPERATIONS_79 = {
    "incoming links": lambda s: [
        s.incoming_links("PARENT", "PARENT-1"), s.incoming_links("../OUT", "OUT-1")],
    "trace": lambda s: [
        s.trace("CHILD", "CHILD-1", "both", None), s.trace("CHILD", "CHILD-2", "both", None),
        s.trace("CHILD", "CHILD-3", "both", None), s.trace("PARENT", "PARENT-42", "both", None),
        s.trace("../OUT", "OUT-1", "both", None)],
    "coverage report": lambda s: [
        s.coverage_report("CHILD", "PARENT", "outgoing", transitive=True),
        s.coverage_report("CHILD", "../OUT", "outgoing"), s.coverage_report("../OUT", "CHILD")],
    "review of a suspect link": lambda s: [
        s.review_suspect_link("CHILD", "CHILD-1", "../OUT", "OUT-1"),
        s.review_suspect_link("CHILD", "CHILD-2", "PARENT", "PARENT-42/../../../../OUT-1"),
        s.review_suspect_link("CHILD", "CHILD-3", "//127.0.0.1/mellon-test-share", "SHARE-1")],
    "link states": lambda s: [s.suspects("CHILD"), s.suspects("PARENT"), s.suspects()],
}


def _suspects_set(rows) -> set:
    return {(e.get("doc_id"), e["source_id"], e["link"]["target_id"], e["state"]) for e in rows}


def _v_model_repo(tmp_path) -> Repo:
    r = Repo(tmp_path / "repo")
    r.add_doc("SYS", {"SYS-1": "system requirement"})
    r.add_doc("SRS", {"SRS-1": "software requirement"}, parents=["SYS"])
    r.add_doc("STS", {"STS-1": ("test case", "Test Case")}, parents=["SRS"])
    r.set_links("SRS", "SRS-1", [("SYS", "SYS-1", "satisfies", None)])
    r.set_links("STS", "STS-1", [("SRS", "SRS-1", "verifies", None)])
    r.commit("seed")
    return r
