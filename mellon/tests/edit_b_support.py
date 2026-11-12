"""Shared helpers for the MELLON-EDIT-SRS blind tests, requirements 176 and up
(docs/specification.md, step 7).

Builders for documents on disk (USAGE.md, On-disk format) with nested outlines,
tables, figures, filters and views; git helpers; the in-process MCP server's
emptied repository; and an offscreen driver for the Document Editor, its menus and
dialogs. Tests for requirements 1 to 175 use tests/edit_support.py, whose helpers
of the same names (write_doc, select, button, grid_ids and others) take other
arguments, so these are kept apart. Fixtures defined here are imported by name into
the test modules that use them.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pygit2
import pytest
from lxml import etree

from tests.store_support import DECL


# A minimal valid SVG, and a second one that differs from it.
SVG_A = (
    DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    '<rect width="20" height="10" fill="#0000ff"/></svg>\n'
).encode()


SVG_B = (
    DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    '<rect width="20" height="10" fill="#00ff00"/></svg>\n'
).encode()


# --------------------------------------------------------------------------
# Building documents on disk (the on-disk format of USAGE.md)
# --------------------------------------------------------------------------

STD_ATTRS = [
    {"name": "type", "kind": "enum", "required": True,
     "options": ["Heading", "Requirement", "Information"]},
    {"name": "status", "kind": "enum", "required": False,
     "options": ["Draft", "Approved"]},
    {"name": "rationale", "kind": "text", "required": False},
]


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _outline_xml(nodes, indent: str) -> list[str]:
    lines = []
    for node in nodes:
        if isinstance(node, str):
            lines.append(f'{indent}<ref id="{node}"/>')
        else:
            ref, children = node
            if children:
                lines.append(f'{indent}<ref id="{ref}">')
                lines.extend(_outline_xml(children, indent + "  "))
                lines.append(f"{indent}</ref>")
            else:
                lines.append(f'{indent}<ref id="{ref}"/>')
    return lines


def write_doc(root: Path, doc_id: str, *, objects: dict, outline=None,
              attrs=None, parents=(), links=None, next_number=None,
              tables=None) -> Path:
    """Write document `doc_id` under `root`.

    objects: {obj_id: (attr dict, description)}
    outline: nested list; a node is an id or (id, [children]). Default:
             every object at the top level, in dict order.
    links:   {source_id: [(target_doc, target_id, type)]}
    tables:  {obj_id: table XML (the <table> element, 2-space indented at 2)}
    """
    attrs = STD_ATTRS if attrs is None else attrs
    outline = list(objects) if outline is None else outline
    folder = root / doc_id
    (folder / "objects").mkdir(parents=True, exist_ok=True)
    head = f'<document id="{doc_id}"'
    if next_number is not None:
        head += f' next-object-number="{next_number}"'
    lines = [head + ">", "  <metadata>", "    <title>Test</title>",
             "    <author>tester</author>", "    <date>2026-01-01</date>",
             "  </metadata>"]
    if parents:
        lines.append("  <parents>")
        lines += [f'    <parent doc="{p}"/>' for p in parents]
        lines.append("  </parents>")
    else:
        lines.append("  <parents/>")
    lines.append("  <attributes>")
    for a in attrs:
        label = f' label="{_esc(a["label"])}"' if a.get("label") else ""
        req = "true" if a.get("required") else "false"
        opts = a.get("options") or []
        start = (f'    <attribute name="{a["name"]}"{label} kind="{a["kind"]}"'
                 f' required="{req}"')
        if opts:
            lines.append(start + ">")
            lines += [f"      <option>{_esc(o)}</option>" for o in opts]
            lines.append("    </attribute>")
        else:
            lines.append(start + "/>")
    lines.append("  </attributes>")
    if outline:
        lines.append("  <outline>")
        lines += _outline_xml(outline, "    ")
        lines.append("  </outline>")
    else:
        lines.append("  <outline/>")
    lines.append("</document>")
    (folder / f"{doc_id}.xml").write_text(DECL + "\n".join(lines) + "\n",
                                          encoding="utf-8", newline="\n")
    for obj_id, (values, description) in objects.items():
        body = [f'<object id="{obj_id}">']
        body += [f'  <attr name="{k}">{_esc(v)}</attr>' for k, v in values.items()]
        body.append(f"  <description>{_esc(description)}</description>")
        if tables and obj_id in tables:
            body.append(tables[obj_id].rstrip("\n"))
        body.append("</object>")
        (folder / "objects" / f"{obj_id}.xml").write_text(
            DECL + "\n".join(body) + "\n", encoding="utf-8", newline="\n")
    for source, items in (links or {}).items():
        (folder / "links").mkdir(exist_ok=True)
        body = [f'<links source="{source}">']
        body += [f'  <link target-doc="{d}" target-id="{t}" type="{k}"/>'
                 for d, t, k in items]
        body.append("</links>")
        (folder / "links" / f"{source}.xml").write_text(
            DECL + "\n".join(body) + "\n", encoding="utf-8", newline="\n")
    return folder


def git_init(root: Path) -> pygit2.Repository:
    repo = pygit2.init_repository(str(root))
    return repo


def git_commit_all(root: Path, message: str) -> str:
    """Commit every file under `root`, as a git client outside Mellon does."""
    repo = pygit2.Repository(str(root))
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    sig = repo.default_signature  # the test identity conftest sets up
    oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
    return str(oid)


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under `root`, `.git` included."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def outline_tree(manifest: Path) -> list:
    """The manifest's outline as nested [(id, [children])], read with lxml."""
    root = etree.parse(str(manifest)).getroot()
    outline = root.find("outline")

    def walk(el):
        return [(r.get("id"), walk(r)) for r in el.findall("ref")]

    return walk(outline) if outline is not None else []


def children_of(tree, ref_id):
    """Sub-object ids of `ref_id` in an outline_tree(); None if absent."""
    for rid, kids in tree:
        if rid == ref_id:
            return [k for k, _ in kids]
        found = children_of(kids, ref_id)
        if found is not None:
            return found
    return None


def top_level(tree) -> list[str]:
    return [rid for rid, _ in tree]


@pytest.fixture
def mcp_root(mcp_repo: Path) -> Path:
    """The in-process MCP server's repository root, emptied of the fixture's
    two documents so that each test builds exactly the documents it needs."""
    for name in ("PARENT", "CHILD"):
        shutil.rmtree(mcp_repo / name, ignore_errors=True)
    return mcp_repo


def _server():
    import mellon.mcp.server as server

    return server


# ==========================================================================
# Outline: moving objects
# ==========================================================================


def _nest_doc(root: Path) -> Path:
    """M: heading H (M-0) over A (M-1), A over B (M-2); X (M-3) on top."""
    return write_doc(root, "M", objects={
        "M-0": ({"type": "Heading"}, "H"),
        "M-1": ({"type": "Requirement"}, "A"),
        "M-2": ({"type": "Requirement"}, "B"),
        "M-3": ({"type": "Requirement"}, "X"),
    }, outline=[("M-0", [("M-1", ["M-2"])]), "M-3"], next_number=4)


_DESCENDANT_MOVES = {
    "under-itself": {"parent_id": "M-0"},
    "under-child": {"parent_id": "M-1"},
    "under-grandchild": {"parent_id": "M-2"},
    "under-child-before-grandchild": {"parent_id": "M-1", "before": "M-2"},
    "under-child-after-grandchild": {"parent_id": "M-1", "after": "M-2"},
    "before-grandchild": {"before": "M-2"},
    "after-grandchild": {"after": "M-2"},
    "before-child": {"before": "M-1"},
}


def _sections_doc(root: Path) -> Path:
    """S: H1 (S-0) over S-1, S-2; H2 (S-3) over S-4, S-5; S-6 on top."""
    return write_doc(root, "S", objects={
        "S-0": ({"type": "Heading"}, "H1"),
        "S-1": ({"type": "Requirement"}, "one"),
        "S-2": ({"type": "Requirement"}, "two"),
        "S-3": ({"type": "Heading"}, "H2"),
        "S-4": ({"type": "Requirement"}, "four"),
        "S-5": ({"type": "Requirement"}, "five"),
        "S-6": ({"type": "Requirement"}, "six"),
    }, outline=[("S-0", ["S-1", "S-2"]), ("S-3", ["S-4", "S-5"]), "S-6"],
        next_number=7)


def _ordering(tree) -> list[dict]:
    return [{"id": rid, "children": _ordering(kids)} for rid, kids in tree]


def _norm(nodes):
    return [(n, []) if isinstance(n, str) else (n[0], _norm(n[1])) for n in nodes]


_BAD_OUTLINES = {
    # every object once, plus one the document doesn't have
    "unknown-object": [("S-0", ["S-1", "S-2"]), ("S-3", ["S-4", "S-5"]), "S-6",
                       "S-99"],
    # S-1 named twice, the rest once each
    "named-twice": [("S-0", ["S-1", "S-2"]), ("S-3", ["S-4", "S-5", "S-1"]),
                    "S-6"],
    # S-6 left out
    "object-dropped": [("S-0", ["S-1", "S-2"]), ("S-3", ["S-4", "S-5"])],
}


# ==========================================================================
# Tables: what Mellon refuses to write
# ==========================================================================


def _table_doc(root: Path) -> Path:
    """T: T-0, a requirement without a table."""
    return write_doc(root, "T", objects={
        "T-0": ({"type": "Requirement"}, "Caption"),
    }, next_number=1)


def _table(cols: list[str], rows: list[list[tuple]]):
    """Build a Table: rows of cells (text, colspan, rowspan)."""
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    return Table(
        columns=tuple(TableColumn(align=a) for a in cols),
        rows=tuple(TableRow(cells=tuple(
            TableCell(text=t, colspan=c, rowspan=r) for t, c, r in row))
            for row in rows))


# Each case: column alignments and rows of (text, colspan, rowspan).
_BAD_TILINGS = {
    # (a) a cell spanning 3 columns in a two-column table
    "colspan-past-last-column": (["left", "left"], [[("a", 3, 1)]]),
    # (b) three cells in a two-column row
    "three-cells-in-two-columns": (["left", "left"],
                                   [[("a", 1, 1), ("b", 1, 1), ("c", 1, 1)]]),
    # (c) a row span of 3 in a two-row table
    "rowspan-past-last-row": (["left", "left"],
                              [[("a", 1, 3), ("b", 1, 1)], [("c", 1, 1)]]),
    # (d) row 1 gives two cells although row 0's rowspan-2 cell covers (1,0)
    "overlap-under-rowspan": (["left", "left"],
                              [[("a", 1, 2), ("b", 1, 1)],
                               [("c", 1, 1), ("d", 1, 1)]]),
    # a row that leaves a position uncovered
    "position-uncovered": (["left", "left"],
                           [[("a", 1, 1), ("b", 1, 1)], [("c", 1, 1)]]),
}


_BAD_SPANS = {"colspan-0": (0, 1), "rowspan-0": (1, 0), "colspan-minus-1": (-1, 1),
              "rowspan-minus-1": (1, -1)}


# ==========================================================================
# Figures: content, not name
# ==========================================================================

# The 8-byte PNG signature and an IHDR chunk: a PNG, whatever its name.
PNG_BYTES = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
             b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
             b"\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00"
             b"\x00\x00\x00IEND\xaeB`\x82")


# draw.io's own file format with a compressed diagram: XML whose root is
# mxfile, not svg.
DRAWIO_COMPRESSED = (
    b'<mxfile host="drawio" version="24.0.0"><diagram id="d" name="Page-1">'
    b"dZHBDoIwDIafhqMJMBQ9KqIePHnwPGFliZQs3Rjo0zvcJiHqkiXt/+9vW5aQqq/"
    b"P1kIc==</diagram></mxfile>\n")


def _figure_doc(root: Path) -> Path:
    folder = write_doc(root, "F", objects={
        "F-0": ({"type": "Requirement"}, "Has a figure"),
    }, next_number=1)
    (folder / "figures").mkdir()
    (folder / "figures" / "F-0.svg").write_bytes(SVG_A)
    return folder


# ==========================================================================
# draw.io desktop: where Mellon looks
# ==========================================================================


def _fake_program(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# ==========================================================================
# Filter conditions (the filter engine applied to a loaded document)
# ==========================================================================

_ALL_ATTR_OPS = ["eq", "ne", "contains", "not-contains", "starts-with", "lt",
                 "le", "gt", "ge", "between", "before", "after", "is-empty",
                 "is-not-empty", "is-true", "is-false"]


_KIND_OPS = {
    "enum": {"eq", "ne", "is-empty", "is-not-empty"},
    "text": {"contains", "not-contains", "starts-with", "eq", "ne", "is-empty",
             "is-not-empty"},
    "integer": {"eq", "ne", "lt", "le", "gt", "ge", "between", "is-empty",
                "is-not-empty"},
    "real": {"eq", "ne", "lt", "le", "gt", "ge", "between", "is-empty",
             "is-not-empty"},
    "boolean": {"is-true", "is-false", "is-empty"},
    "date": {"eq", "ne", "before", "after", "between", "is-empty",
             "is-not-empty"},
}


# ==========================================================================
# GUI helpers (offscreen Document Editor)
# ==========================================================================


def _plain(text: str) -> str:
    """An action's text without mnemonics or a trailing ellipsis."""
    return text.replace("&", "").replace("…", "").replace("...", "").strip()


def find_action(widget, text: str):
    """The QAction under `widget` whose plain text is `text`."""
    from PySide6.QtGui import QAction

    found = [a for a in widget.findChildren(QAction) if _plain(a.text()) == text]
    assert found, f"no action {text!r}"
    return found[0]


def toolbar_menu(window, title: str):
    """The toolbar menu (Figure, Table, Views, Filters) with its actions
    rebuilt for the current selection."""
    from PySide6.QtWidgets import QToolButton

    for b in window.findChildren(QToolButton):
        if b.text() == title and b.menu() is not None:
            menu = b.menu()
            menu.aboutToShow.emit()
            return menu
    raise AssertionError(f"no toolbar menu {title!r}")


def menu_action(window, title: str, text: str):
    menu = toolbar_menu(window, title)
    for a in menu.actions():
        if _plain(a.text()) == text:
            return a
        if a.menu() is not None:
            for sub in a.menu().actions():
                if _plain(sub.text()) == text:
                    return sub
    raise AssertionError(
        f"{title} menu has no {text!r}: {[_plain(a.text()) for a in menu.actions()]}")


def menu_texts(window, title: str) -> list[str]:
    return [_plain(a.text()) for a in toolbar_menu(window, title).actions()
            if a.text()]


def open_editor(qtbot, root: Path, doc_id: str, *, writable: bool = True,
                service=None):
    """Open a Document Editor on `doc_id`, as the GUI does, and leave
    read-only mode when `writable`."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    svc = service or DocumentService(root, cache=True)
    window = DocumentEditorWindow(doc_id, svc)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)
    ro = find_action(window, "Read-only")
    if writable and ro.isChecked():
        ro.trigger()
    assert ro.isChecked() is (not writable)
    return window


def activate(window, qtbot) -> None:
    """Deliver the window-activation event the GUI reacts to when the user
    returns to the window."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(window, QEvent(QEvent.WindowActivate))
    qtbot.wait(20)


def select(window, obj_id: str, qtbot=None) -> None:
    window.controller.select_object(window.controller.current_doc_id(), obj_id)
    if qtbot is not None:
        qtbot.wait(10)


def grid_ids(window) -> list[str]:
    """The object IDs of the grid's shown rows, top to bottom."""
    from PySide6.QtCore import Qt

    model = window.grid_pane.table.model()
    id_col = next(c for c in range(model.columnCount())
                  if model.headerData(c, Qt.Horizontal) == "ID")
    return [model.data(model.index(r, id_col)) for r in range(model.rowCount())]


def grid_headers(window, *, shown_only: bool = True) -> list[str]:
    """The grid's column headers left to right in visual order."""
    from PySide6.QtCore import Qt

    view = window.grid_pane.table
    model = view.model()
    header = view.horizontalHeader()
    out = []
    for visual in range(header.count()):
        logical = header.logicalIndex(visual)
        if shown_only and header.isSectionHidden(logical):
            continue
        out.append(model.headerData(logical, Qt.Horizontal))
    return out


def _flag(value) -> int:
    return int(value.value) if hasattr(value, "value") else int(value)


class MessageBoxes:
    """Stand-ins for QMessageBox, so that no modal box waits for input.
    Records each box; answers by accepting (Yes, OK, a Delete button...) or,
    with accept=False, by declining (No, Cancel)."""

    def __init__(self, monkeypatch, accept: bool = True):
        from PySide6.QtWidgets import QMessageBox

        self.calls: list[tuple[str, str]] = []
        self.accept = accept
        for name in ("question", "warning", "information", "critical"):
            monkeypatch.setattr(QMessageBox, name, self._make(name))

        def exec_(box, *args, **kwargs):
            return self._exec(box)

        monkeypatch.setattr(QMessageBox, "exec", exec_)

    def _standard_answer(self, buttons):
        from PySide6.QtWidgets import QMessageBox

        order = ((QMessageBox.Yes, QMessageBox.Ok, QMessageBox.Save,
                  QMessageBox.Discard, QMessageBox.Apply) if self.accept else
                 (QMessageBox.No, QMessageBox.Cancel, QMessageBox.Abort,
                  QMessageBox.Close))
        for b in order:
            if buttons is None or _flag(buttons) & _flag(b):
                return b
        return order[0]

    def _make(self, name):
        def fake(*args, **kwargs):
            from PySide6.QtWidgets import QMessageBox

            texts = [a for a in args if isinstance(a, str)]
            self.calls.append((name, " | ".join(texts)))
            buttons = kwargs.get("buttons")
            if buttons is None:
                flags = [a for a in args if isinstance(a, QMessageBox.StandardButton)]
                buttons = flags[0] if flags else None
            if buttons is None:
                return QMessageBox.Yes if name == "question" and self.accept \
                    else (QMessageBox.No if name == "question" else QMessageBox.Ok)
            return self._standard_answer(buttons)
        return staticmethod(fake)

    def _exec(self, box):
        from PySide6.QtWidgets import QMessageBox

        self.calls.append(("exec", box.text()))
        roles = ((QMessageBox.AcceptRole, QMessageBox.YesRole,
                  QMessageBox.DestructiveRole, QMessageBox.ApplyRole)
                 if self.accept else (QMessageBox.RejectRole, QMessageBox.NoRole))
        chosen = [b for b in box.buttons() if box.buttonRole(b) in roles]
        if not chosen:
            return QMessageBox.Ok
        chosen[0].click()
        standard = box.standardButton(chosen[0])
        if standard != QMessageBox.NoButton:
            return standard
        return box.buttons().index(chosen[0])

    def text(self) -> str:
        return "\n".join(t for _, t in self.calls)


# ==========================================================================
# Tables in the Document Editor (table editor and Table menu)
# ==========================================================================


def _gui_table_repo(tmp_path: Path, table) -> Path:
    """Repository with document T whose T-0 carries `table`, written by
    Mellon's own editor and committed."""
    from mellon.editor import set_table

    root = tmp_path / "repo"
    root.mkdir()
    folder = write_doc(root, "T", objects={
        "T-0": ({"type": "Requirement"}, "Caption"),
        "T-1": ({"type": "Requirement"}, "Other"),
    }, next_number=2)
    set_table(folder, "T-0", table)
    git_init(root)
    git_commit_all(root, "seed")
    return root


def _open_table_editor(qtbot, root: Path, monkeypatch):
    """Open the Document Editor on T, select T-0 and open its table editor."""
    from mellon.gui.widgets.inline_table import InlineTableWidget

    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "T")
    select(window, "T-0", qtbot)
    window.grid_pane.begin_table_edit("T-0")
    qtbot.wait(20)
    editors = window.findChildren(InlineTableWidget)
    assert len(editors) == 1, "the table editor did not open"
    return window, editors[0]


def _edit_cell(qtbot, editor, row: int, col: int, keys, *, finish="commit"):
    """Edit the cell's text as a user does: F2, select all, type, then end
    the cell edit with Ctrl+Enter (or cancel it with Escape)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    grid = editor.table
    grid.setCurrentCell(row, col)
    grid.setFocus()
    qtbot.keyClick(grid, Qt.Key_F2)
    field = QApplication.focusWidget()
    assert field is not grid, "no cell editor opened"
    field.selectAll()
    for k in keys:
        if isinstance(k, str):
            qtbot.keyClicks(field, k)
        else:
            qtbot.keyClick(field, *k)
    if finish == "escape":
        qtbot.keyClick(field, Qt.Key_Escape)
    else:
        qtbot.keyClick(field, Qt.Key_Return, Qt.ControlModifier)
    qtbot.wait(10)


def _save_table(qtbot, editor) -> None:
    """Save the table editor with Ctrl+Enter (USAGE.md), pressed where the
    focus is until the editor closes; the first press may only end a cell
    edit."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    window = editor.window()
    for _ in range(3):
        if not window.findChildren(type(editor)):
            break
        target = QApplication.focusWidget()
        if target is None or not editor.isAncestorOf(target):
            target = editor.table
        qtbot.keyClick(target, Qt.Key_Return, Qt.ControlModifier)
        qtbot.wait(20)
    assert not window.findChildren(type(editor)), "the table editor didn't close"


def _header_2x2():
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    return Table(columns=(TableColumn("left"), TableColumn("left")), rows=(
        TableRow(cells=(TableCell("h1"), TableCell("h2")), header=True),
        TableRow(cells=(TableCell("a"), TableCell("b")))))


# ==========================================================================
# Figures in the Document Editor
# ==========================================================================

SVG_RED = (DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
           '<rect width="40" height="20" fill="#ff0000"/></svg>\n').encode()


def _figure_repo(tmp_path: Path, figure: bytes | None = None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    folder = write_doc(root, "F", objects={
        "F-0": ({"type": "Requirement"}, "With figure"),
        "F-1": ({"type": "Requirement"}, "Without figure"),
    }, next_number=2)
    if figure is not None:
        (folder / "figures").mkdir()
        (folder / "figures" / "F-0.svg").write_bytes(figure)
    git_init(root)
    git_commit_all(root, "seed")
    return root


def _choose_file(monkeypatch, path: Path) -> list:
    """Answer the file chooser with `path`; returns the list of calls."""
    from PySide6.QtWidgets import QFileDialog

    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return (str(path), "SVG figures (*.svg)")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake))
    return calls


# ==========================================================================
# Filters and views in the Document Editor
# ==========================================================================


def drive_dialogs(monkeypatch, qtbot, handler) -> list:
    """Run each modal QDialog through `handler(dialog)` instead of waiting
    for input; the dialog's own result is returned to the caller."""
    from PySide6.QtWidgets import QDialog

    seen = []

    def fake_exec(dialog):
        seen.append(dialog)
        dialog.show()
        qtbot.wait(10)
        handler(dialog)
        qtbot.wait(10)
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    return seen


def answer_text(monkeypatch, text: str) -> list:
    """Answer each QInputDialog.getText with `text`."""
    from PySide6.QtWidgets import QInputDialog

    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return (text, True)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake))
    return calls


def button(dialog, text: str):
    from PySide6.QtWidgets import QAbstractButton

    found = [b for b in dialog.findChildren(QAbstractButton)
             if _plain(b.text()) == text]
    assert found, f"{type(dialog).__name__} has no {text!r} button"
    return found[0]


def _attr_combos(dialog) -> list:
    """The attribute choosers of the Advanced filter dialog's condition rows."""
    from PySide6.QtWidgets import QComboBox

    return [c for c in dialog.findChildren(QComboBox)
            if "__link_out__" in [c.itemData(i) for i in range(c.count())]]


def _row_combos(attr_combo):
    from PySide6.QtWidgets import QComboBox

    row = attr_combo.parentWidget()
    return [c for c in row.findChildren(QComboBox) if c is not attr_combo]


def _choose(combo, data) -> None:
    index = combo.findData(data)
    assert index >= 0, f"no choice {data!r}: {[combo.itemData(i) for i in range(combo.count())]}"
    combo.setCurrentIndex(index)


def _operator_combo(attr_combo):
    """The operator chooser beside `attr_combo`: the shown combo whose
    choices are operator codes."""
    known = set(_ALL_ATTR_OPS) | {"present", "absent"}
    for c in _row_combos(attr_combo):
        datas = [c.itemData(i) for i in range(c.count())]
        if datas and set(datas) <= known and c.isVisibleTo(attr_combo.window()):
            return c
    raise AssertionError("no operator chooser in the condition row")


def _name_field(dialog):
    from PySide6.QtWidgets import QLineEdit

    fields = [f for f in dialog.findChildren(QLineEdit)
              if f.isVisibleTo(dialog) and f.placeholderText() not in
              ("from", "to", "value")]
    assert fields, "no name field"
    return fields[0]


def _filters_repo(tmp_path: Path, *, filters=(), views=()) -> Path:
    """Document D of six objects with every attribute kind, plus a parent
    document P; `filters` are (name, rules), `views` (name, columns)."""
    from mellon.editor import create_filter, create_view

    root = tmp_path / "repo"
    root.mkdir()
    attrs = [
        {"name": "type", "kind": "enum", "required": True,
         "options": ["Heading", "Requirement"]},
        {"name": "status", "kind": "enum", "required": False,
         "options": ["Draft", "Approved"]},
        {"name": "priority", "kind": "enum", "required": False,
         "options": ["Must", "Should"]},
        {"name": "rationale", "kind": "text", "required": False},
        {"name": "count", "kind": "integer", "required": False},
        {"name": "weight", "kind": "real", "required": False},
        {"name": "flag", "kind": "boolean", "required": False},
        {"name": "due", "kind": "date", "required": False},
    ]
    write_doc(root, "P", objects={"P-0": ({"type": "Requirement"}, "parent req")},
              next_number=1)
    folder = write_doc(root, "D", attrs=attrs, objects={
        "D-0": ({"type": "Heading"}, "Section one"),
        "D-1": ({"type": "Requirement", "status": "Draft", "priority": "Must"},
                "alpha requirement"),
        "D-2": ({"type": "Requirement", "status": "Approved", "priority": "Should"},
                "beta requirement"),
        "D-3": ({"type": "Heading"}, "Section two"),
        "D-4": ({"type": "Requirement", "status": "Approved", "priority": "Must"},
                "gamma requirement"),
        "D-5": ({"type": "Requirement", "status": "Draft"}, "delta requirement"),
    }, outline=[("D-0", ["D-1", "D-2"]), ("D-3", ["D-4"]), "D-5"],
        parents=["P"], next_number=6)
    for name, rules in filters:
        create_filter(folder, name, rules=rules)
    for name, columns in views:
        create_view(folder, name, columns)
    git_init(root)
    git_commit_all(root, "seed")
    return root


def _eq(attr: str, value: str):
    from mellon.schema import FilterRule

    return FilterRule(kind="attr", attr=attr, op="eq", value=value)


def _filter_files(root: Path) -> dict[str, bytes]:
    d = root / "D" / "filters"
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.xml"))} if d.exists() else {}


def _inspector_field(window, label: str):
    """The Inspector's editor for the field labelled `label` (e.g. 'status')."""
    from PySide6.QtWidgets import QFormLayout, QGridLayout

    for lay in window.inspector.findChildren(QFormLayout):
        for r in range(lay.rowCount()):
            li = lay.itemAt(r, QFormLayout.LabelRole)
            fi = lay.itemAt(r, QFormLayout.FieldRole)
            if li and li.widget() and _plain(li.widget().text()).rstrip(":") == label:
                return fi.widget()
    for lay in window.inspector.findChildren(QGridLayout):
        for r in range(lay.rowCount()):
            li = lay.itemAtPosition(r, 0)
            fi = lay.itemAtPosition(r, 1)
            if li and li.widget() and hasattr(li.widget(), "text") and \
                    _plain(li.widget().text()).rstrip(":") == label:
                return fi.widget()
    raise AssertionError(f"no Inspector field {label!r}")


def _views_menu_choose(window, name: str) -> None:
    menu_action(window, "Views", name).trigger()


# The columns shown in each view (MELLON-EDIT-SRS-327): state, object ID,
# description and type (shown by its label, Type).
_FIXED_HEADERS = ("", "ID", "Description", "Type")


def _attr_headers(window) -> list[str]:
    """The shown attribute columns that a view decides, left to right."""
    return [h for h in grid_headers(window) if h not in _FIXED_HEADERS]


def _new_view_handler(qtbot, name: str, columns: list[str]):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit, QListWidget

    def handler(dialog):
        dialog.findChildren(QLineEdit)[0].setText(name)
        lst = dialog.findChildren(QListWidget)[0]
        for i in range(lst.count()):
            item = lst.item(i)
            item.setCheckState(Qt.Checked if item.text() in columns else Qt.Unchecked)
        button(dialog, "OK").click()

    return handler


def _broken_doc(root: Path) -> None:
    """Document BAD, whose object file isn't well-formed XML: a load error."""
    folder = write_doc(root, "BAD", objects={
        "BAD-0": ({"type": "Requirement"}, "fine")}, next_number=1)
    (folder / "objects" / "BAD-0.xml").write_text(
        DECL + '<object id="BAD-0">\n  <description>cut off\n',
        encoding="utf-8", newline="\n")
