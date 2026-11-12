"""Shared helpers for the MELLON-EDIT-SRS blind tests, requirements 1 to 175
(docs/specification.md, step 7).

Builders for documents on disk (USAGE.md, On-disk format) with nested outlines,
labelled attributes and a next object number; the repositories those tests use;
readers that take values straight from the XML; and an offscreen driver for the
Document Editor, the Properties dialog and the Project Explorer that answers every
modal dialog. It builds on tests/store_support.py's file builders, snapshots, git
helpers and MCP fixtures rather than repeating them. Fixtures defined here are
imported by name into the test modules that use them.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pygit2
import pytest
from lxml import etree
from PySide6.QtCore import QLocale, QModelIndex, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QTreeView,
    QWidget,
)

import mellon.mcp.server as mcp_server
from mellon import editor, git_ops
from mellon.loader import load_document
from mellon.schema import Table, TableCell, TableColumn, TableRow
from mellon.service.document import DocumentService
from tests.store_support import (
    git_commit_all,
    git_init,
    links_xml,
    manifest_xml,
    object_xml,
    write,
)

# --------------------------------------------------------------------------------------
# Building documents on disk
# --------------------------------------------------------------------------------------

STD_ATTRS = (
    ("type", "enum", True, ("Heading", "Requirement", "Information")),
    ("status", "enum", False, ("Draft", "Approved")),
    ("rationale", "text", False, ()),
)

SVG_BLUE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    b'<rect width="20" height="10" fill="blue"/></svg>\n'
)


def attr_def(name, kind, required=False, options=(), label=None):
    """One manifest <attribute> element, with its options and an optional label."""
    lab = f" label={quoteattr(label)}" if label is not None else ""
    req = "true" if required else "false"
    if options:
        opts = "".join(f"      <option>{escape(o)}</option>\n" for o in options)
        return f'    <attribute name="{name}"{lab} kind="{kind}" required="{req}">\n{opts}    </attribute>\n'
    return f'    <attribute name="{name}"{lab} kind="{kind}" required="{req}"/>\n'


def outline_xml(tree, indent=4):
    """Nested <ref> elements for a list of IDs or (ID, [sub-outline]) pairs."""
    out = ""
    for node in tree:
        nid, kids = (node, ()) if isinstance(node, str) else node
        pad = " " * indent
        if kids:
            out += f'{pad}<ref id="{nid}">\n' + outline_xml(kids, indent + 2) + f"{pad}</ref>\n"
        else:
            out += f'{pad}<ref id="{nid}"/>\n'
    return out


def write_doc(root, doc_id, *, objects, outline, attrs=STD_ATTRS, parents=(), links=None,
              next_number=None, title="T", author="A", date="2026-01-01"):
    """Write a document folder: manifest, object files and links files.

    `objects` maps object ID -> (attribute values, description); `outline` is a list of
    IDs or (ID, [sub-outline]) pairs; `links` maps a source ID -> [(doc, id, type)].
    """
    folder = Path(root) / doc_id
    (folder / "objects").mkdir(parents=True, exist_ok=True)
    nn = f' next-object-number="{next_number}"' if next_number is not None else ""
    write(folder / f"{doc_id}.xml", manifest_xml(
        doc_id,
        metadata=f"<title>{escape(title)}</title><author>{escape(author)}</author><date>{date}</date>",
        parents=tuple(parents),
        attributes="".join(attr_def(*a) for a in attrs),
        outline=("\n" + outline_xml(outline) + "  ") if outline else None,
        root_attrs=nn,
    ))
    for oid, (avals, desc) in objects.items():
        write(folder / "objects" / f"{oid}.xml", object_xml(
            oid, attrs=tuple((k, escape(v)) for k, v in avals.items()), description=escape(desc)))
    for src, lst in (links or {}).items():
        write(folder / "links" / f"{src}.xml", links_xml(src, lst))
    return folder


@pytest.fixture
def commit_all(git_env):
    """Commit everything under a root, making it a repository first; returns the hash."""
    def commit(root, message="seed"):
        root = Path(root)
        if not (root / ".git").exists():
            git_init(git_env, root)
        return git_commit_all(git_env, root, message)
    return commit


@pytest.fixture
def no_git_user(tmp_path_factory):
    """No git `user.name` anywhere libgit2 looks."""
    empty = tmp_path_factory.mktemp("empty-git-config")
    levels = [pygit2.enums.ConfigLevel.GLOBAL, pygit2.enums.ConfigLevel.XDG,
              pygit2.enums.ConfigLevel.SYSTEM, pygit2.enums.ConfigLevel.PROGRAMDATA]
    old = {lv: pygit2.settings.search_path[lv] for lv in levels}
    for lv in levels:
        pygit2.settings.search_path[lv] = str(empty)
    yield
    for lv, v in old.items():
        pygit2.settings.search_path[lv] = v


def tools(mcp_at, root):
    """Point the in-process MCP tools at `root` with store_support's mcp_at; return them."""
    mcp_at(root)
    return mcp_server


def mcp_refused(fn, *args, **kwargs):
    """Call a tool expecting a refusal; a raised error counts as one too."""
    try:
        res = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - any raised error is a refusal of the call
        return {"ok": False, "raised": repr(exc)}
    return res


# --------------------------------------------------------------------------------------
# Reading results straight from the files
# --------------------------------------------------------------------------------------


def obj_file(root, doc_id, obj_id):
    return Path(root) / doc_id / "objects" / f"{obj_id}.xml"


def read_obj(root, doc_id, obj_id):
    """(attribute values as the file gives them, description) read straight from the XML."""
    el = etree.parse(str(obj_file(root, doc_id, obj_id))).getroot()
    attrs = {a.get("name"): (a.text or "") for a in el.findall("attr")}
    return attrs, (el.findtext("description") or "")


def manifest_root(root, doc_id):
    return etree.parse(str(Path(root) / doc_id / f"{doc_id}.xml")).getroot()


def manifest_attr(root, doc_id, name):
    for a in manifest_root(root, doc_id).iter("attribute"):
        if a.get("name") == name:
            return a
    return None


def manifest_options(root, doc_id, name):
    a = manifest_attr(root, doc_id, name)
    return tuple(o.text for o in a.findall("option"))


def _find_node(nodes, ref_id):
    for n in nodes:
        if n.ref_id == ref_id:
            return n
        hit = _find_node(n.children, ref_id)
        if hit is not None:
            return hit
    return None


def sub_objects(root, doc_id, parent_id=None):
    """Sub-object IDs of `parent_id` in outline order; the top level for None."""
    doc = load_document(Path(root) / doc_id)
    if parent_id is None:
        return [n.ref_id for n in doc.outline]
    node = _find_node(doc.outline, parent_id)
    assert node is not None, f"{parent_id} is not in the outline"
    return [c.ref_id for c in node.children]


def all_object_ids(root, doc_id):
    return {p.stem for p in (Path(root) / doc_id / "objects").glob("*.xml")}


# --------------------------------------------------------------------------------------
# The repositories the tests use
# --------------------------------------------------------------------------------------


def basic_repo(root):
    """SRS: SRS-0 heading over SRS-1 (Draft) and SRS-2; SYS: SYS-0, SYS-1. SRS declares SYS."""
    write_doc(root, "SYS", objects={"SYS-0": ({"type": "Requirement"}, "System one"),
                                    "SYS-1": ({"type": "Requirement"}, "System two")},
              outline=["SYS-0", "SYS-1"], next_number=2)
    write_doc(root, "SRS", parents=("SYS",),
              objects={"SRS-0": ({"type": "Heading"}, "Heading"),
                       "SRS-1": ({"type": "Requirement", "status": "Draft"}, "First"),
                       "SRS-2": ({"type": "Requirement", "status": "Approved"}, "Second")},
              outline=[("SRS-0", ["SRS-1", "SRS-2"])], next_number=3)
    return Path(root)


def child_parent_repo(root, commit):
    """CHILD declares PARENT, and CHILD-0 links to PARENT-1."""
    write_doc(root, "PARENT", objects={"PARENT-0": ({"type": "Heading"}, "P"),
                                       "PARENT-1": ({"type": "Requirement"}, "Parent req")},
              outline=[("PARENT-0", ["PARENT-1"])], next_number=2)
    write_doc(root, "CHILD", parents=("PARENT",),
              objects={"CHILD-0": ({"type": "Requirement", "status": "Draft"}, "Child req")},
              outline=["CHILD-0"], links={"CHILD-0": [("PARENT", "PARENT-1", "satisfies")]},
              next_number=1)
    commit(root)
    return Path(root)


def counted_repo(root, commit, *, tag):
    """SYS: 2 objects, no links. SRS: 3 objects, 3 outgoing links in 2 links files; with
    `tag`, SRS issue 1 on the first commit and issue 2 on the second."""
    write_doc(root, "SYS", objects={"SYS-0": ({"type": "Requirement"}, "a"),
                                    "SYS-1": ({"type": "Requirement"}, "b")},
              outline=["SYS-0", "SYS-1"], next_number=2)
    write_doc(root, "SRS", parents=("SYS",),
              objects={"SRS-0": ({"type": "Heading"}, "H"),
                       "SRS-1": ({"type": "Requirement"}, "one"),
                       "SRS-2": ({"type": "Requirement"}, "two")},
              outline=[("SRS-0", ["SRS-1", "SRS-2"])],
              links={"SRS-1": [("SYS", "SYS-0", "satisfies"), ("SYS", "SYS-1", "satisfies")],
                     "SRS-2": [("SYS", "SYS-1", "satisfies")]},
              next_number=3)
    c1 = commit(root)
    if tag:
        repo = pygit2.Repository(str(root))
        git_ops.create_issue_tag(repo, "SRS", "1", pygit2.Oid(hex=c1))
        write(obj_file(root, "SRS", "SRS-2"), object_xml("SRS-2", description="two, revised"))
        c2 = commit(root, "revise")
        git_ops.create_issue_tag(repo, "SRS", "2", pygit2.Oid(hex=c2))
    return Path(root)


def parts_repo(root, commit):
    """counted_repo without tags, plus a view Dev and a filter Only headings."""
    from mellon.schema import FilterRule
    counted_repo(root, commit, tag=False)
    editor.create_view(Path(root) / "SRS", "Dev", ["type"])
    editor.create_filter(Path(root) / "SRS", "Only headings",
                         rules=[FilterRule(kind="attr", attr="type", op="eq", value="Heading")])
    return Path(root)


def labelled_repo(root, commit):
    """`type` without a label, `req-type` labelled Requirement Type, `rationale` without one."""
    attrs = (
        ("type", "enum", True, ("Heading", "Requirement")),
        ("req-type", "enum", False, ("Functional", "Safety"), "Requirement Type"),
        ("rationale", "text", False, ()),
    )
    write_doc(root, "SRS", attrs=attrs,
              objects={"SRS-0": ({"type": "Requirement", "req-type": "Safety", "rationale": "why"}, "R")},
              outline=["SRS-0"], next_number=1)
    commit(root)
    return Path(root)


V = "VCU3-SRS"


def vcu_repo(root, commit):
    """VCU3-SRS-0 heading with the sub-objects 1, 2, 11, 12, 13; next object number 14."""
    ids = [f"{V}-{n}" for n in (1, 2, 11, 12, 13)]
    objects = {f"{V}-0": ({"type": "Heading"}, "Section")}
    objects.update({i: ({"type": "Requirement"}, f"Text of {i}") for i in ids})
    write_doc(root, V, objects=objects, outline=[(f"{V}-0", ids)], next_number=14)
    commit(root)
    return Path(root)


def order_repo(root, commit):
    """Top level SRS-4 (sub-objects SRS-5, SRS-2), SRS-1, SRS-3: out of numeric order."""
    write_doc(root, "SRS", objects={i: ({"type": "Requirement"}, i) for i in
                                    ("SRS-1", "SRS-2", "SRS-3", "SRS-4", "SRS-5")},
              outline=[("SRS-4", ["SRS-5", "SRS-2"]), "SRS-1", "SRS-3"], next_number=6)
    commit(root)
    return Path(root)


def edit_repo(root, commit, extra_attrs=(), values=None):
    """SRS-0 heading over SRS-1 (Requirement, Draft, `Old text`), with extra attributes."""
    attrs = STD_ATTRS + tuple(extra_attrs)
    write_doc(root, "SRS", attrs=attrs, objects={
        "SRS-0": ({"type": "Heading"}, "H"),
        "SRS-1": ({"type": "Requirement", "status": "Draft", **(values or {})}, "Old text"),
    }, outline=[("SRS-0", ["SRS-1"])], next_number=2)
    commit(root)
    return Path(root)


def long_repo(root, commit, n=150):
    """L with `n` three-line objects; L-80 has a table."""
    ids = [f"L-{i}" for i in range(n)]
    objects = {i: ({"type": "Requirement", "status": "Draft"},
                   f"Requirement {i} line one\nline two\nline three") for i in ids}
    write_doc(root, "L", objects=objects, outline=ids, next_number=n)
    editor.set_table(Path(root) / "L", "L-80", Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("a"), TableCell("b"))), TableRow(cells=(TableCell("c"), TableCell("d"))))))
    commit(root)
    return Path(root)


TABLE = Table(
    columns=(TableColumn("left"), TableColumn("right")),
    rows=(TableRow(cells=(TableCell("Signal"), TableCell("Limit")), header=True),
          TableRow(cells=(TableCell("Spans both", colspan=2),))),
)


def copy_repo(root, commit):
    """SRS-0 heading over SRS-1 (TABLE, link to SYS-0) and SRS-2; SRS-3 top level; next
    object number 7, so SRS-3 to SRS-6 may have been used before."""
    write_doc(root, "SYS", objects={"SYS-0": ({"type": "Requirement"}, "S")}, outline=["SYS-0"], next_number=1)
    write_doc(root, "SRS", parents=("SYS",), objects={
        "SRS-0": ({"type": "Heading"}, "Heading"),
        "SRS-1": ({"type": "Requirement", "status": "Draft", "rationale": "why"}, "Limits"),
        "SRS-2": ({"type": "Requirement"}, "Other"),
        "SRS-3": ({"type": "Requirement", "status": "Approved"}, "Top"),
    }, outline=[("SRS-0", ["SRS-1", "SRS-2"]), "SRS-3"],
        links={"SRS-1": [("SYS", "SYS-0", "satisfies")]}, next_number=7)
    editor.set_table(Path(root) / "SRS", "SRS-1", TABLE)
    commit(root)
    return Path(root)


def copy_of(root, mcp_at, path_kind, source="SRS-1", **kwargs):
    """Copy `source` once through the service or the MCP tool; return the copy's ID."""
    if path_kind == "mcp":
        res = tools(mcp_at, root).copy_object("SRS", source, **kwargs)
        assert res.get("ok") is True, res
        (new_id,) = res["new_ids"]
    else:
        (new_id,) = DocumentService(root).copy_object("SRS", source, **kwargs).value
    return new_id


def with_figure(root, commit, obj="SRS-1"):
    """Attach SVG_BLUE to `obj` and commit; return the figure file's bytes."""
    src = Path(root).parent / "blue.svg"
    src.write_bytes(SVG_BLUE)
    editor.set_figure(Path(root) / "SRS", obj, src)
    commit(root, "figure")
    return editor.figure_path(Path(root) / "SRS", obj).read_bytes()


def delete_repo(root, commit):
    """SRS-0 heading over SRS-1, then SRS-2 at the top level."""
    write_doc(root, "SRS", objects={"SRS-0": ({"type": "Heading"}, "H"),
                                    "SRS-1": ({"type": "Requirement"}, "R1"),
                                    "SRS-2": ({"type": "Requirement"}, "R2")},
              outline=[("SRS-0", ["SRS-1"]), "SRS-2"], next_number=3)
    commit(root)
    return Path(root)


def commit_new_repo(root, commit):
    """Make a folder a repository with one commit (git commits no empty tree)."""
    if not any(Path(root).iterdir()):
        write(Path(root) / "README.md", "repository\n")
    commit(root)
    return Path(root)


def create_default_doc(path_kind, root, mcp_at, commit, doc_id="D"):
    """Create `doc_id` without attribute definitions, through the library or the MCP tool."""
    if path_kind == "mcp":
        commit_new_repo(root, commit)
        res = tools(mcp_at, root).create_document(doc_id)
        assert res.get("ok") is True, res
    else:
        editor.create_document(root, doc_id)
    return Path(root) / doc_id


def assert_link_kept(root, links_before, mcp_at):
    """CHILD no longer declares PARENT, yet CHILD-0's links file keeps its bytes and
    show_object still lists the link to PARENT-1."""
    path = Path(root) / "CHILD" / "links" / "CHILD-0.xml"
    assert path.read_bytes() == links_before, "the links file changed"
    assert load_document(Path(root) / "CHILD").parents == ()
    shown = tools(mcp_at, root).show_object("CHILD", "CHILD-0")
    assert "PARENT-1" in json.dumps(shown), f"show_object no longer lists the link: {shown}"


# --------------------------------------------------------------------------------------
# The GUI driver
# --------------------------------------------------------------------------------------


class Modals:
    """Answers every modal entry point a GUI test can reach, and records each call."""

    def __init__(self):
        self.calls = []
        self.question = QMessageBox.StandardButton.No
        self.box_button = "cancel"     # "accept" or "cancel": what a QMessageBox.exec answers
        self.inputs = []               # answers for QInputDialog's static functions, in order
        self.handlers = {}             # QDialog subclass -> handler(dialog) -> int
        self.on_call = None            # callback(kind) run the moment a modal opens


@pytest.fixture
def modals(monkeypatch):
    """Patch QMessageBox, QInputDialog, QDialog.exec and QMenu.exec so that no GUI test
    waits for input; tests set the answers on the returned Modals."""
    m = Modals()

    def static(kind):
        def f(*args, **kwargs):
            m.calls.append((kind, " | ".join(a for a in args if isinstance(a, str))))
            if m.on_call:
                m.on_call(kind)
            return m.question if kind == "question" else QMessageBox.StandardButton.Ok
        return staticmethod(f)

    for kind in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, static(kind))

    def box_exec(self):
        m.calls.append(("box", self.text()))
        if m.on_call:
            m.on_call("box")
        accept_roles = (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole)
        reject_roles = (QMessageBox.ButtonRole.RejectRole, QMessageBox.ButtonRole.NoRole)
        roles = accept_roles if m.box_button == "accept" else reject_roles
        for b in self.buttons():
            if self.buttonRole(b) in roles:
                b.click()
                sb = self.standardButton(b)
                return sb if sb != QMessageBox.StandardButton.NoButton else self.result()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", box_exec)

    def get_input(kind):
        def f(*args, **kwargs):
            m.calls.append((kind, " | ".join(a for a in args if isinstance(a, str))))
            return m.inputs.pop(0) if m.inputs else ("", False)
        return staticmethod(f)

    for kind in ("getText", "getItem", "getMultiLineText"):
        monkeypatch.setattr(QInputDialog, kind, get_input(kind))

    def dialog_exec(self):
        m.calls.append(("dialog", type(self).__name__))
        for cls, handler in m.handlers.items():
            if isinstance(self, cls):
                return handler(self)
        return 0

    monkeypatch.setattr(QDialog, "exec", dialog_exec)
    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: None)
    return m


@pytest.fixture
def us_locale():
    """Number and date editors read the locale; fix it so the typed keys mean one thing."""
    old = QLocale()
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
    yield
    QLocale.setDefault(old)


def plain(text):
    return text.replace("&", "").replace("…", "...").strip()


def find_action(widget, text):
    hits = [a for a in widget.findChildren(QAction) if plain(a.text()) == text]
    assert hits, f"no action named {text!r}"
    return hits[0]


def open_editor_window(qtbot, root, doc_id, *, editable=True):
    """A shown Document Editor on `doc_id`, with read-only mode off (or on)."""
    from mellon.gui.main_window import DocumentEditorWindow
    w = DocumentEditorWindow(doc_id, DocumentService(root))
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    qtbot.waitExposed(w)
    ro = find_action(w, "Read-only")
    if ro.isChecked() == editable:
        ro.trigger()
    qtbot.wait(20)
    assert ro.isChecked() != editable
    return w


def grid_col(w, header):
    m = w.grid_pane.table.model()
    for c in range(m.columnCount()):
        if m.headerData(c, Qt.Orientation.Horizontal) == header:
            return c
    raise AssertionError(f"no grid column {header!r}")


def grid_headers(w):
    m = w.grid_pane.table.model()
    return [m.headerData(c, Qt.Orientation.Horizontal) for c in range(m.columnCount())]


def walk_model(m, parent=QModelIndex()):
    """(row, parent) of each row of a model, top to bottom, sub-rows after their row."""
    for r in range(m.rowCount(parent)):
        yield r, parent
        yield from walk_model(m, m.index(r, 0, parent))


def grid_ids(w):
    """Object IDs of the grid's rows, top to bottom."""
    m = w.grid_pane.table.model()
    c = grid_col(w, "ID")
    return [m.index(r, c, p).data() for r, p in walk_model(m)]


def grid_index(w, obj_id, header):
    m = w.grid_pane.table.model()
    idc, c = grid_col(w, "ID"), grid_col(w, header)
    for r, p in walk_model(m):
        if m.index(r, idc, p).data() == obj_id:
            return m.index(r, c, p)
    raise AssertionError(f"{obj_id} has no grid row")


def current_id(w):
    t = w.grid_pane.table
    idx = t.currentIndex()
    return idx.siblingAtColumn(grid_col(w, "ID")).data() if idx.isValid() else None


def select(qtbot, w, obj_id):
    """Click the object's ID cell in the grid."""
    t = w.grid_pane.table
    idx = grid_index(w, obj_id, "ID")
    t.scrollTo(idx)
    qtbot.wait(10)
    QTest.mouseClick(t.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     t.visualRect(idx).center())
    qtbot.wait(30)
    assert current_id(w) == obj_id


INPUTS = (QComboBox, QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)


def open_cell_editor(qtbot, w, obj_id, header):
    """Open the grid's editor on one cell and return it."""
    t = w.grid_pane.table
    idx = grid_index(w, obj_id, header)
    t.scrollTo(idx)
    t.setCurrentIndex(idx)
    t.edit(idx)
    qtbot.wait(30)
    eds = [c for c in t.viewport().findChildren(QWidget) if c.isVisible() and isinstance(c, INPUTS)]
    outer = [e for e in eds if not any(o is not e and o.isAncestorOf(e) for o in eds)]
    assert outer, f"no editor opened on {obj_id} / {header}"
    return outer[0]


def type_into(qtbot, widget, text):
    """Focus a text-taking widget, select its text and type `text` over it with keys.

    A date editor takes a date section by section: select its year and type the digits,
    as a user does (the editor moves to the next section once one is full)."""
    widget.setFocus()
    qtbot.wait(5)
    if isinstance(widget, QDateTimeEdit) and text:
        widget.setSelectedSection(QDateTimeEdit.Section.YearSection)
        QTest.keyClicks(widget, text.replace("-", ""))
        qtbot.wait(5)
        return
    QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    if text:
        QTest.keyClicks(widget, text)
    else:
        QTest.keyClick(widget, Qt.Key.Key_Delete)
    qtbot.wait(5)


def choose_in_combo(qtbot, combo, text):
    """Open the combo's list and click the entry `text`, as a user picks from it."""
    rows = [i for i in range(combo.count()) if combo.itemText(i) == text]
    assert rows, f"{text!r} is not offered; offered {[combo.itemText(i) for i in range(combo.count())]}"
    combo.showPopup()
    qtbot.wait(20)
    view = combo.view()
    idx = view.model().index(rows[0], combo.modelColumn())
    view.scrollTo(idx)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     view.visualRect(idx).center())
    qtbot.wait(30)


def combo_items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def field_for_label(container, label_text):
    """The input widget a form pairs with the label reading `label_text`."""
    for form in container.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            li = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            fi = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if li and fi and li.widget() and isinstance(li.widget(), QLabel) \
                    and plain(li.widget().text()).rstrip(":") == label_text and fi.widget():
                return fi.widget()
    labels = [lb for lb in container.findChildren(QLabel) if plain(lb.text()).rstrip(":") == label_text]
    assert labels, f"no label {label_text!r}"
    lb = labels[0]
    if lb.buddy() is not None:
        return lb.buddy()
    y = lb.mapTo(container, lb.rect().center()).y()
    fields = [f for f in container.findChildren(QWidget) if isinstance(f, INPUTS)
              and not isinstance(f.parentWidget(), (QAbstractSpinBox, QComboBox))]
    return min(fields, key=lambda f: abs(f.mapTo(container, f.rect().center()).y() - y))


def button(container, text):
    hits = [b for b in container.findChildren(QAbstractButton) if plain(b.text()).rstrip(".") == text]
    assert hits, f"no button {text!r} in {type(container).__name__}"
    return hits[0]


def inspector_fields(w):
    """Inspector's field name (label text without ':') -> the widget beside or below it."""
    insp = w.inspector
    labels = [lb for lb in insp.findChildren(QLabel) if lb.isVisible() and lb.text().endswith(":")]
    out = {}
    for lb in labels:
        name = lb.text()[:-1]
        y = lb.mapTo(insp, lb.rect().center()).y()
        x = lb.mapTo(insp, lb.rect().center()).x()
        cands = [c for c in insp.findChildren(QWidget) if c.isVisible() and c is not lb
                 and isinstance(c, INPUTS + (QLabel,))
                 and not isinstance(c.parentWidget(), (QAbstractSpinBox, QComboBox))
                 and not (isinstance(c, QLabel) and c.text().endswith(":"))]
        same_row = [c for c in cands if abs(c.mapTo(insp, c.rect().center()).y() - y) < 12
                    and c.mapTo(insp, c.rect().center()).x() > x]
        below = [c for c in cands if c.mapTo(insp, c.rect().topLeft()).y() > y]
        pick = same_row or sorted(below, key=lambda c: c.mapTo(insp, c.rect().topLeft()).y())[:1]
        if pick:
            out[name] = min(pick, key=lambda c: c.mapTo(insp, c.rect().center()).x())
    return out


def widget_text(c):
    if isinstance(c, QComboBox):
        return c.currentText()
    if isinstance(c, (QTextEdit, QPlainTextEdit)):
        return c.toPlainText()
    if isinstance(c, (QLineEdit, QLabel, QAbstractSpinBox)):
        return c.text()
    return None


def open_properties(qtbot, w, doc_id):
    """The Properties dialog on `doc_id`, shown without a modal loop."""
    from mellon.gui.dialogs.document_properties import DocumentPropertiesDialog
    d = DocumentPropertiesDialog(w.controller, doc_id, w)
    qtbot.addWidget(d)
    d.show()
    qtbot.waitExposed(d)
    return d


def props_attributes(qtbot, root):
    """(window, Properties dialog, its attributes tab, the tab's tree) on SRS."""
    from PySide6.QtWidgets import QTreeWidget

    from mellon.gui.dialogs.document_properties import AttributesTab
    w = open_editor_window(qtbot, root, "SRS")
    d = open_properties(qtbot, w, "SRS")
    tab = d.findChild(AttributesTab)
    return w, d, tab, tab.findChild(QTreeWidget)


def attr_tree_item(tree, name):
    for i in range(tree.topLevelItemCount()):
        it = tree.topLevelItem(i)
        if it.text(0) == name:
            return it
    listed = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    raise AssertionError(f"attribute {name!r} not listed; listed {listed}")


def required_control(tab, tree, item):
    """A control of the attributes tab that changes whether the selected attribute is
    required: a check box in the item, or an enabled button or action naming required."""
    for c in range(tree.columnCount()):
        if item.data(c, Qt.ItemDataRole.CheckStateRole) is not None:
            col = c

            def toggle_check():
                new = (Qt.CheckState.Unchecked if item.checkState(col) == Qt.CheckState.Checked
                       else Qt.CheckState.Checked)
                item.setCheckState(col, new)
            return toggle_check
    for b in tab.findChildren(QAbstractButton):
        if "required" in plain(b.text()).lower() and b.isEnabled() and b.isVisible():
            return b.click
    for a in tab.findChildren(QAction):
        if "required" in plain(a.text()).lower() and a.isEnabled():
            return a.trigger
    return None


def new_document_handler(doc_id):
    """Fill the New document dialog's Document ID field with keys and press OK."""
    def handler(dlg):
        dlg.show()
        QApplication.processEvents()
        field = field_for_label(dlg, "Document ID")
        field.setFocus()
        QTest.keyClicks(field, doc_id)
        ok = [b for b in dlg.findChildren(QPushButton) if plain(b.text()) == "OK"][0]
        if ok.isEnabled():
            ok.click()
        QApplication.processEvents()
        result = dlg.result()
        dlg.hide()
        handler.ran = True
        return result
    handler.ran = False
    return handler


def add_attribute_handler(modals, name, kind, options=(), label=""):
    """Fill the Add attribute form with keys and clicks, then press OK."""
    def handler(dlg):
        dlg.show()
        QApplication.processEvents()
        name_field = field_for_label(dlg, "Name")
        name_field.setFocus()
        QTest.keyClicks(name_field, name)
        if label:
            lf = field_for_label(dlg, "Label")
            lf.setFocus()
            QTest.keyClicks(lf, label)
        kind_box = field_for_label(dlg, "Kind")
        kind_box.setCurrentIndex(kind_box.findText(kind))
        QApplication.processEvents()
        for opt in options:
            modals.inputs.append((opt, True))
            button(dlg, "Add option").click()
            QApplication.processEvents()
        [b for b in dlg.findChildren(QPushButton) if plain(b.text()) == "OK"][0].click()
        QApplication.processEvents()
        result = dlg.result()
        dlg.hide()
        return result
    return handler


def explorer(qtbot, root):
    from mellon.gui.project_explorer_window import ProjectExplorerWindow
    pe = ProjectExplorerWindow()
    qtbot.addWidget(pe)
    pe.show()
    qtbot.waitExposed(pe)
    assert pe.open_repo(root)
    return pe


def explorer_doc_ids(pe):
    """The first word of each row the Project Explorer's visible tree shows."""
    ids = []
    for v in pe.findChildren(QTreeView):
        if not v.isVisible():
            continue
        m = v.model()
        for r, p in walk_model(m):
            text = m.index(r, 0, p).data()
            if text:
                ids.append(str(text).split()[0])
    return ids


# Oracles for what fits each kind, written from USAGE.md's schema kinds, apart from the
# validator: integer = base-10 signed digits; real = a parseable float; date = YYYY-MM-DD
# naming a real day from 0001-01-01 to 9999-12-31.

def fits_integer(v):
    return re.fullmatch(r"[+-]?[0-9]+", v) is not None


def fits_real(v):
    try:
        float(v)
    except ValueError:
        return False
    return v.strip() == v and v.lower() not in ("nan", "inf", "-inf", "+inf", "infinity", "-infinity")


def fits_date(v):
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v) is None:
        return False
    try:
        datetime.date(int(v[:4]), int(v[5:7]), int(v[8:]))
    except ValueError:
        return False
    return True


KIND_CASES = {
    "integer": (("count", "integer", False, ()), "3", fits_integer),
    "real": (("ratio", "real", False, ()), "1.5", fits_real),
    "date": (("due", "date", False, ()), "2026-05-01", fits_date),
}

BOOL = (("flag", "boolean", False, ()),)


def kind_window(qtbot, root, commit, kind):
    """A Document Editor on SRS whose SRS-1 has a value for an attribute of `kind`."""
    attr, start, _ = KIND_CASES[kind]
    edit_repo(root, commit, (attr,), {attr[0]: start})
    return open_editor_window(qtbot, root, "SRS"), attr[0]


def grid_enter(qtbot, w, name, text):
    """Type `text` into SRS-1's grid cell for `name` and leave the cell."""
    ed = open_cell_editor(qtbot, w, "SRS-1", name)
    type_into(qtbot, ed, text)
    w.grid_pane.commit_open_editor()
    qtbot.wait(30)


def inspector_enter(qtbot, w, name, text):
    """Type `text` into the Inspector's field for `name` on SRS-1 and save it."""
    select(qtbot, w, "SRS-1")
    field = inspector_fields(w)[name]
    type_into(qtbot, field, text)
    w.inspector.commit_pending()
    qtbot.wait(30)


def middle_row(qtbot, w):
    """Scroll the grid halfway and return the ID of the row at the viewport's centre."""
    t = w.grid_pane.table
    bar = t.verticalScrollBar()
    assert bar.maximum() > 0, "precondition: the grid scrolls"
    bar.setValue(bar.maximum() // 2)
    qtbot.wait(50)
    return t.indexAt(t.viewport().rect().center()).siblingAtColumn(grid_col(w, "ID")).data()


def outline_delete(w):
    """The outline pane's own Delete action, as a callable."""
    return [a for a in w.outline_pane.edit_toolbar.actions() if plain(a.text()) == "Delete"][0].trigger


def grid_delete_key(w):
    """Pressing Delete in the grid, as a callable."""
    def press():
        t = w.grid_pane.table
        t.setFocus()
        QTest.keyClick(t, Qt.Key.Key_Delete)
    return press
