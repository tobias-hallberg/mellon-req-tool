"""Blind tests, round 2, for Compare: MELLON-VER-SRS-9, -22, -56, -67 and -73.

Written from the requirements, `docs/definitions.md` and the public signatures
alone, without the code under test or the bodies of other tests. Every expected
value comes from a requirement or a definition. Repositories are built in
`tmp_path` with fixed commit signatures; no real clock, no network.

Rungs: -22 runs on rung 2 (the comparison over real git commits) and once more
through the Compare dialog and the MCP server; -9, -56, -67 and -73 run on
rung 3 (the GUI offscreen, the MCP server over stdio).
"""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path
from typing import Callable

import pygit2
import pytest
import shiboken6
from lxml import etree
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTextEdit,
    QWidget,
)

from mellon import diff, editor, git_ops
from mellon.gui.main_window import DocumentEditorWindow
from mellon.gui.panels.grid_pane import GridPane, ReadOnlyBanner
from mellon.service import DocumentService
from tests.test_blind_ver_engine import _is_error, _payload, mcp_session

DOC = "SPEC"
WORKING_COPY = diff.WORKING_COPY

# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def _sig(n: int) -> pygit2.Signature:
    """A fixed signature per commit number, so hashes repeat from run to run."""
    return pygit2.Signature("Blind Tester", "blind@example.invalid", 1_700_000_000 + 60 * n, 0)


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pygit2.init_repository(str(root), initial_head="main")


def _commit(root: Path, message: str) -> str:
    """Commit every file under `root` on the current branch; return the full hash."""
    repo = pygit2.Repository(str(root))
    index = repo.index
    index.add_all()
    index.write()
    tree = index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    n = 0 if repo.head_is_unborn else sum(1 for _ in repo.walk(repo.head.target))
    oid = repo.create_commit("HEAD", _sig(n), _sig(n), message, tree, parents)
    return str(oid)


def _object_file(root: Path, obj_id: str) -> Path:
    return root / DOC / "objects" / f"{obj_id}.xml"


def _disk_description(root: Path, obj_id: str) -> str:
    """The description an object's file holds in the working copy."""
    tree = etree.parse(str(_object_file(root, obj_id)))
    node = tree.getroot().find("description")
    return "" if node is None or node.text is None else node.text


def _all_object_ids(repo: pygit2.Repository) -> set[str]:
    """Every object in the repository: commits, trees, blobs and tag objects."""
    seen: set[str] = set()

    def tree_walk(tree: pygit2.Tree) -> None:
        seen.add(str(tree.id))
        for entry in tree:
            if entry.type_str == "tree":
                tree_walk(repo[entry.id])
            else:
                seen.add(str(entry.id))

    for name in repo.references:
        ref = repo.references[name].resolve()
        obj = repo[ref.target]
        if obj.type_str == "tag":
            seen.add(str(obj.id))
            obj = obj.peel(pygit2.Commit)
        for commit in repo.walk(obj.id):
            seen.add(str(commit.id))
            tree_walk(commit.tree)
    return seen


def _build_rev_repo(root: Path) -> dict:
    """SPEC on `main`: commit Ck adds SPEC-k ("Add object k"), k = 0..5; HEAD = main = C5.

    Names: annotated tag `v1` and issue tag `SPEC-issue-1` on C1; lightweight
    tag `rel-2` and local branch `feature` on C2; remote-tracking branch
    `origin/review` on C3. Comparing From Ck with To C5 lists SPEC-(k+1)..SPEC-5
    as Added and nothing else, so the rows tell which commit From named.
    """
    _init(root)
    folder = editor.create_document(root, DOC, title="Revisions")
    ids, commits = [], []
    for k in range(6):
        ids.append(editor.create_object(folder, None, f"Object {k}."))
        commits.append(_commit(root, f"Add object {k}"))
    repo = pygit2.Repository(str(root))
    oid = [pygit2.Oid(hex=h) for h in commits]
    git_ops.create_tag(repo, "v1", oid[1], "Version 1")
    git_ops.create_issue_tag(repo, DOC, "1", oid[1])
    repo.references.create("refs/tags/rel-2", oid[2])
    repo.references.create("refs/heads/feature", oid[2])
    repo.references.create("refs/remotes/origin/review", oid[3])
    everything = _all_object_ids(repo)
    prefix = None
    for k in (1, 2, 3, 4):
        p = commits[k][:4]
        if sum(1 for h in everything if h.startswith(p)) == 1:
            prefix = (p, k)
            break
    assert prefix, "test setup: no commit C1..C4 has a 4-digit prefix unique in the repository"
    v1_tag_object = str(repo.references["refs/tags/v1"].target)
    return {"ids": ids, "commits": commits, "prefix": prefix, "v1_tag_object": v1_tag_object}


@pytest.fixture(scope="module")
def rev_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("rev") / "repo"
    return root, _build_rev_repo(root)


@pytest.fixture
def rev_repo(rev_template, tmp_path):
    src, info = rev_template
    dst = tmp_path / "repo"
    shutil.copytree(src, dst)
    return dst, info


def _added(info: dict, after: range) -> dict[str, str]:
    return {info["ids"][j]: "Added" for j in after}


# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------


def _open_editor(qtbot, root: Path, *, editable: bool = False) -> DocumentEditorWindow:
    win = DocumentEditorWindow(DOC, DocumentService(root, cache=True))
    qtbot.addWidget(win)
    win.show()
    QApplication.processEvents()
    if editable:
        banner = win.findChild(ReadOnlyBanner)
        button = next(b for b in banner.findChildren(QAbstractButton) if b.text() == "Enable editing")
        button.click()
        qtbot.waitUntil(lambda: not banner.isVisible(), timeout=5000)
    return win


def _settle(qtbot, dlg) -> None:
    qtbot.waitUntil(lambda: not dlg.is_busy(), timeout=20_000)
    QApplication.processEvents()


def _open_compare(qtbot, win, from_ref=None, to_ref=None):
    dlg = win.open_compare(from_ref, to_ref)
    _settle(qtbot, dlg)
    return dlg


def _press_compare(qtbot, dlg) -> None:
    """Press Compare without moving the keyboard focus, as a shortcut does."""
    dlg.compare_button.click()
    _settle(qtbot, dlg)


def _rows(dlg) -> dict[str, tuple[str, str]]:
    """The dialog's rows as {object ID: (change, text)}."""
    tree = dlg.list
    head = {tree.headerItem().text(c): c for c in range(tree.columnCount())}
    out: dict[str, tuple[str, str]] = {}

    def walk(item) -> None:
        if not item.isHidden():
            out[item.text(head["ID"])] = (item.text(head["Change"]), item.text(head["Text"]))
        for i in range(item.childCount()):
            walk(item.child(i))

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    return out


def _changes(dlg) -> dict[str, str]:
    return {obj_id: change for obj_id, (change, _) in _rows(dlg).items()}


def _result_text(dlg) -> str:
    """What the dialog shows besides its rows, with no row selected: the title and
    each visible label, line edit and text pane, apart from the version pickers
    and the selected object's details."""
    excluded = (dlg.from_combo, dlg.to_combo, dlg.details)
    parts = [dlg.windowTitle()]
    for w in dlg.findChildren(QWidget):
        if not w.isVisible() or any(x is w or x.isAncestorOf(w) for x in excluded):
            continue
        if isinstance(w, QLabel):
            parts.append(w.text())
        elif isinstance(w, QLineEdit):
            parts.append(w.text())
        elif isinstance(w, QTextEdit):
            parts.append(w.toPlainText())
    return html.unescape(re.sub(r"<[^>]+>", " ", "\n".join(parts)))


def _grid_view(win) -> QAbstractItemView:
    grid = win.findChild(GridPane)
    return next(v for v in grid.findChildren(QAbstractItemView) if not isinstance(v, QHeaderView))


def _type_without_leaving(qtbot, win, obj_id: str, text: str):
    """Open `obj_id`'s description cell in the grid and type `text` at its end,
    leaving the cell editor open: a pending edit. Returns (view, editor widget)."""
    view = _grid_view(win)
    model = view.model()
    cols = {model.headerData(c, Qt.Horizontal, Qt.DisplayRole): c for c in range(model.columnCount())}
    row = next(r for r in range(model.rowCount()) if model.data(model.index(r, cols["ID"])) == obj_id)
    index = model.index(row, cols["Description"])
    view.setCurrentIndex(index)
    view.edit(index)
    qtbot.waitUntil(lambda: view.state() == QAbstractItemView.State.EditingState, timeout=5000)
    widget = view.indexWidget(index) or QApplication.focusWidget()
    QTest.keyClick(widget, Qt.Key_End, Qt.ControlModifier)
    QTest.keyClicks(widget, text)
    QApplication.processEvents()
    return view, widget


def _editor_text(widget) -> str | None:
    """The text in a cell editor, or None once the editor is gone."""
    if widget is None or not shiboken6.isValid(widget):
        return None
    if hasattr(widget, "toPlainText"):
        return widget.toPlainText()
    return widget.text()


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-9: typed text that git's revision syntax resolves to one commit
# ---------------------------------------------------------------------------

# (case, typed text or a function of the repository info, the k of the commit Ck it names)
TYPED_FROM: list[tuple[str, object, object]] = [
    ("full hash", lambda i: i["commits"][2], 2),
    ("4-digit unique prefix", lambda i: i["prefix"][0], lambda i: i["prefix"][1]),
    ("annotated tag", "v1", 1),
    ("lightweight tag", "rel-2", 2),
    ("local branch", "feature", 2),
    ("local branch, full ref name", "refs/heads/feature", 2),
    ("remote-tracking branch", "origin/review", 3),
    ("remote-tracking branch, full ref name", "refs/remotes/origin/review", 3),
    ("HEAD~1", "HEAD~1", 4),
    ("HEAD^", "HEAD^", 4),
    ("HEAD^^", "HEAD^^", 3),
    ("HEAD~5, the first commit", "HEAD~5", 0),
    ("@~2", "@~2", 3),
    ("<branch>~2", "main~2", 3),
    ("<branch>~2 on another branch", "feature~2", 0),
    ("<tag>^{commit}", "v1^{commit}", 1),
    ("<tag>^{}", "v1^{}", 1),
    ("<tag>~1", "rel-2~1", 1),
    ("HEAD^{/message}", "HEAD^{/object 3}", 3),
    (":/message", ":/object 1", 1),
]


def _resolve(value, info):
    return value(info) if callable(value) else value


def _from_latest(qtbot, root: Path, info: dict):
    """The dialog comparing C5 with the latest commit: no rows. Each typed version
    below names an older commit, so rows can only come from the typed text."""
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win, info["commits"][5], diff.LATEST)
    assert _changes(dlg) == {}, f"precondition: C5 to the latest commit lists {_rows(dlg)}"
    return dlg


@pytest.mark.parametrize("case, typed, k", TYPED_FROM, ids=[c[0] for c in TYPED_FROM])
def test_9_typed_from_compares_the_commit_it_names(qtbot, rev_repo, case, typed, k):
    """MELLON-VER-SRS-9: text typed as From that git's revision syntax resolves to
    one commit Ck is compared as Ck. To is the latest commit (C5), so the rows
    must be SPEC-(k+1)..SPEC-5, each Added, and nothing else. Each expected
    commit was checked with `git rev-parse`."""
    root, info = rev_repo
    text, k = _resolve(typed, info), _resolve(k, info)
    dlg = _from_latest(qtbot, root, info)
    dlg.from_combo.setEditText(text)
    dlg.to_combo.setCurrentIndex(dlg.to_combo.findData(diff.LATEST))
    _press_compare(qtbot, dlg)
    expected = _added(info, range(k + 1, 6))
    shown = _changes(dlg)
    assert shown == expected, (
        f"From typed as {text!r} ({case}) names C{k}; comparing it with the latest commit should "
        f"list {expected}, the dialog lists {shown}. Message: {dlg.message_label.text()!r}"
    )


TYPED_TO: list[tuple[str, object, object]] = [
    ("HEAD~2", "HEAD~2", 3),
    ("local branch", "feature", 2),
    ("remote-tracking branch", "origin/review", 3),
    ("<tag>^{commit}", "v1^{commit}", 1),
    ("4-digit unique prefix", lambda i: i["prefix"][0], lambda i: i["prefix"][1]),
]


@pytest.mark.parametrize("case, typed, k", TYPED_TO, ids=[c[0] for c in TYPED_TO])
def test_9_typed_to_compares_the_commit_it_names(qtbot, rev_repo, case, typed, k):
    """MELLON-VER-SRS-9, the To side: From is C0's full hash, To is typed text that
    resolves to Ck, so the rows must be SPEC-1..SPEC-k, each Added."""
    root, info = rev_repo
    text, k = _resolve(typed, info), _resolve(k, info)
    dlg = _from_latest(qtbot, root, info)
    dlg.from_combo.setEditText(info["commits"][0])
    dlg.to_combo.setEditText(text)
    _press_compare(qtbot, dlg)
    expected = _added(info, range(1, k + 1))
    shown = _changes(dlg)
    assert shown == expected, (
        f"To typed as {text!r} ({case}) names C{k}; From C0 to it should list {expected}, "
        f"the dialog lists {shown}. Message: {dlg.message_label.text()!r}"
    )


NOT_A_COMMIT = [
    ("no such name", "no-such-branch"),
    ("past the first commit", "HEAD~6"),
    ("a tree", "HEAD^{tree}"),
    ("the tree of a commit", "HEAD:"),
    ("a file at a commit", "HEAD:SPEC/SPEC.xml"),
]


def _bad_after_good(qtbot, rev_repo, bad: str):
    """Compare From C2 (typed full hash) with the latest commit, then type `bad` as
    From and press Compare. Returns (dialog, rows of the good result)."""
    root, info = rev_repo
    dlg = _from_latest(qtbot, root, info)
    dlg.from_combo.setEditText(info["commits"][2])
    _press_compare(qtbot, dlg)
    good = _rows(dlg)
    assert _changes(dlg) == _added(info, range(3, 6)), "precondition: the first comparison failed"
    dlg.from_combo.setEditText(bad)
    _press_compare(qtbot, dlg)
    return dlg, good


@pytest.mark.parametrize("case, bad", NOT_A_COMMIT, ids=[c[0] for c in NOT_A_COMMIT])
def test_9_text_naming_no_commit_is_reported_by_name(qtbot, rev_repo, case, bad):
    """MELLON-VER-SRS-9's boundary, with -10: text that resolves to no commit
    (nothing, or a tree or file rather than a commit) isn't accepted; the dialog
    shows an error message naming the typed text."""
    dlg, _ = _bad_after_good(qtbot, rev_repo, bad)
    shown = _result_text(dlg)
    assert bad in shown, f"typed {bad!r} ({case}) names no commit; no message names it. The dialog shows: {shown!r}"


@pytest.mark.parametrize("case, bad", NOT_A_COMMIT, ids=[c[0] for c in NOT_A_COMMIT])
def test_9_text_naming_no_commit_is_not_compared(qtbot, rev_repo, case, bad):
    """MELLON-VER-SRS-9's boundary, with -10 and -11: text that resolves to no
    commit isn't compared as something else, so the previous rows stay."""
    dlg, good = _bad_after_good(qtbot, rev_repo, bad)
    assert _rows(dlg) == good, (
        f"typed {bad!r} ({case}) names no commit, yet the rows changed from {good} to {_rows(dlg)}"
    )


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-22: differences gone in the canonical form are no change
# ---------------------------------------------------------------------------

FIGURE_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">\n'
    b'  <g id="layer">\n'
    b'    <rect x="0" y="0" width="40" height="20" fill="#ff0000"/>\n'
    b"  </g>\n"
    b"</svg>\n"
)


def _build_canon_repo(root: Path) -> dict:
    """SPEC at commit "seed", written by the editor: SPEC-0 (Heading) over SPEC-1
    (two-line description, rationale set but empty, a link to SPEC-0 with an
    acknowledged commit), SPEC-2 (text with < and &, a figure) and SPEC-3 (empty
    description). Files with empty elements written both as <x/> and <x></x>."""
    _init(root)
    folder = editor.create_document(root, DOC, title="Canonical forms")
    top = editor.create_object(folder, None, "Top heading.")
    editor.set_attribute(folder, top, "type", "Heading")
    one = editor.create_object(folder, top, "Line one.\nLine two.")
    editor.set_attribute(folder, one, "type", "Information")
    editor.set_attribute(folder, one, "rationale", "")
    two = editor.create_object(folder, top, "Speed < 10 & stop.")
    editor.set_attribute(folder, two, "type", "Information")
    three = editor.create_object(folder, top, "")
    editor.set_attribute(folder, three, "type", "Note")
    editor.add_link(folder, one, DOC, top, "refines")
    src = root.parent / "figure-src.svg"
    src.write_bytes(FIGURE_SVG)
    editor.set_figure(folder, two, src)
    c0 = _commit(root, "objects")
    links = folder / "links" / f"{one}.xml"
    data = links.read_bytes()
    marker = b'type="refines"/>'
    assert marker in data, f"test setup: unexpected links file {data!r}"
    links.write_bytes(data.replace(marker, f'type="refines" ack="{c0}"/>'.encode()))
    c1 = _commit(root, "seed")
    return {"ids": [top, one, two, three], "c0": c0, "c1": c1}


@pytest.fixture(scope="module")
def canon_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("canon") / "repo"
    return root, _build_canon_repo(root)


@pytest.fixture
def canon_repo(canon_template, tmp_path):
    src, info = canon_template
    dst = tmp_path / "repo"
    shutil.copytree(src, dst)
    return dst, info


def _canonical(data: bytes) -> bytes:
    """The definition's canonical form, worked out independently: Canonical XML
    (lxml's C14N) with whitespace-only text between elements removed."""
    root = etree.fromstring(data, etree.XMLParser(strip_cdata=True, resolve_entities=False))
    for el in root.iter():
        if len(el) and el.text is not None and not el.text.strip(" \t\r\n"):
            el.text = None
        if el.tail is not None and not el.tail.strip(" \t\r\n"):
            el.tail = None
    return etree.tostring(root, method="c14n")


_TAG = re.compile(r"<([A-Za-z_][\w.:-]*)((?:\s+[\w.:-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))+)(\s*/?)>")
_ATTR = re.compile(r"([\w.:-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def _reverse_attributes(s: str) -> str:
    def one(m):
        attrs = _ATTR.findall(m.group(2))[::-1]
        return "<" + m.group(1) + "".join(f" {n}={v}" for n, v in attrs) + m.group(3) + ">"

    return _TAG.sub(one, s)


def _single_quotes(s: str) -> str:
    return re.sub(r'="([^"<>\']*)"', r"='\1'", s)


def _space_inside_tags(s: str) -> str:
    def one(m):
        attrs = _ATTR.findall(m.group(2))
        return "<" + m.group(1) + "".join(f"\n      {n} = {v}" for n, v in attrs) + " " + m.group(3).strip() + ">"

    s = _TAG.sub(one, s)
    s = re.sub(r"<([A-Za-z_][\w.:-]*)>", r"<\1 >", s)
    return re.sub(r"</([\w.:-]+)>", r"</\1  >", s)


def _expand_empty(s: str) -> str:
    return re.sub(r"<([A-Za-z_][\w.:-]*)([^<>]*?)\s*/>", r"<\1\2></\1>", s)


def _collapse_empty(s: str) -> str:
    return re.sub(r"<([A-Za-z_][\w.:-]*)([^<>]*)></\1>", r"<\1\2/>", s)


def _tabs(s: str) -> str:
    return re.sub(r"(?m)^( +)(?=<)", lambda m: "\t" * (len(m.group(1)) // 2), s)


def _one_line(s: str) -> str:
    return re.sub(r">\s+<", "><", s)


def _blank_lines(s: str) -> str:
    return re.sub(r">\n([ \t]*)<", r">\n\n\1<", s)


def _no_declaration(s: str) -> str:
    return re.sub(r"^<\?xml[^>]*\?>\s*", "", s)


def _no_final_newline(s: str) -> str:
    return s.rstrip("\n")


def _crlf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "\r\n")


def _char_refs(s: str) -> str:
    s = s.replace('"SPEC', '"&#83;PEC')
    s = s.replace(">Line one.", ">&#x4C;ine one.")
    return s.replace(">Top heading.", ">&#84;op heading.")


def _cdata(s: str) -> str:
    def one(m):
        text = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return f"<description><![CDATA[{text}]]></description>"

    return re.sub(r"<description>([^<]+)</description>", one, s)


def _everything(s: str) -> str:
    for step in (_cdata, _char_refs, _expand_empty, _reverse_attributes, _single_quotes,
                 _space_inside_tags, _tabs, _no_declaration, _blank_lines, _crlf):
        s = step(s)
    return s


XML_REWRITES: list[tuple[str, Callable[[str], str]]] = [
    ("CRLF line endings", _crlf),
    ("tab indentation", _tabs),
    ("no indentation, one line", _one_line),
    ("blank lines between elements", _blank_lines),
    ("no final newline", _no_final_newline),
    ("XML attributes in reverse order", _reverse_attributes),
    ("single quotes", _single_quotes),
    ("empty elements as <x></x>", _expand_empty),
    ("empty elements as <x/>", _collapse_empty),
    ("whitespace inside tags", _space_inside_tags),
    ("no XML declaration", _no_declaration),
    ("character references", _char_refs),
    ("CDATA sections", _cdata),
    ("all of these at once", _everything),
]

FIGURE_REWRITES: list[tuple[str, Callable[[str], str]]] = [
    ("CRLF line endings", _crlf),
    ("tab indentation", _tabs),
    ("XML attributes in reverse order", _reverse_attributes),
    ("single quotes", _single_quotes),
    ("empty elements as <x></x>", _expand_empty),
    ("whitespace inside tags", _space_inside_tags),
]


def _rewrite(root: Path, pattern: str, rewrite: Callable[[str], str]) -> list[str]:
    """Rewrite each file matching `pattern` under SPEC/. Each rewrite must leave the
    file's canonical form as it was (checked here, so a mistake in this test can't
    pass for a finding). Returns the files changed; at least one must be."""
    changed = []
    for path in sorted((root / DOC).glob(pattern)):
        old = path.read_bytes()
        new = rewrite(old.decode("utf-8")).encode("utf-8")
        if new == old:
            continue
        assert _canonical(new) == _canonical(old), f"test error: rewrite changed the canonical form of {path}"
        path.write_bytes(new)
        changed.append(path.relative_to(root).as_posix())
    assert changed, f"test error: the rewrite changed no file matching {pattern}"
    return changed


def _reported(d: diff.DocumentDiff) -> list[str]:
    """Everything a comparison reports: each object change, and outline problems."""
    out = [f"{c.obj_id}: {', '.join(str(k) for k in c.kinds)} {c.errors}" for c in d.changes]
    for side, err in (("From", d.outline_error_a), ("To", d.outline_error_b)):
        if err:
            out.append(f"manifest at {side}: {err}")
    return out


def _kinds(d: diff.DocumentDiff) -> dict[str, tuple[str, ...]]:
    return {c.obj_id: tuple(str(k) for k in c.kinds) for c in d.changes}


@pytest.mark.parametrize("case, rewrite", XML_REWRITES, ids=[c[0] for c in XML_REWRITES])
def test_22_canonical_only_rewrite_of_document_files_is_no_change(canon_repo, case, rewrite):
    """MELLON-VER-SRS-22: every manifest, object, links and rules file rewritten in a
    way its canonical form doesn't show (Canonical XML 1.1 also drops the XML
    declaration and resolves character references and CDATA). Comparing the two
    commits reports no object and no problem."""
    root, info = canon_repo
    files = _rewrite(root, "**/*.xml", rewrite)
    c2 = _commit(root, f"rewrite: {case}")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    assert _reported(d) == [], f"{case} in {files} is gone in the canonical form, but Compare reports {_reported(d)}"


@pytest.mark.parametrize("case, rewrite", FIGURE_REWRITES, ids=[c[0] for c in FIGURE_REWRITES])
def test_22_canonical_only_rewrite_of_figure_is_no_change(canon_repo, case, rewrite):
    """MELLON-VER-SRS-22: the figure is a file too (an SVG, so XML). A rewrite its
    canonical form doesn't show marks no object Modified."""
    root, info = canon_repo
    files = _rewrite(root, "figures/*.svg", rewrite)
    c2 = _commit(root, f"figure rewrite: {case}")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    assert _reported(d) == [], f"{case} in {files} is gone in the canonical form, but Compare reports {_reported(d)}"


def _desc_word(root, folder, ids):
    editor.set_description(folder, ids[0], "Top title.")
    return {ids[0]: ("Modified",)}


def _desc_inner_space(root, folder, ids):
    editor.set_description(folder, ids[1], "Line one.\nLine  two.")
    return {ids[1]: ("Modified",)}


def _ack(root, folder, ids):
    path = folder / "links" / f"{ids[1]}.xml"
    seed = str(pygit2.Repository(str(root)).head.target)
    data = path.read_bytes()
    new = re.sub(rb'ack="[0-9a-f]+"', f'ack="{seed}"'.encode(), data)
    assert new != data, "test setup: no ack in the links file"
    path.write_bytes(new)
    return {ids[1]: ("Link reviewed",)}


def _swap(root, folder, ids):
    # From: SPEC-1, SPEC-2, SPEC-3 under SPEC-0; To: SPEC-1, SPEC-3, SPEC-2. Two
    # largest same-order sets ({1,2}, {1,3}); the later at From (SPEC-3) is Moved.
    editor.move_object(folder, ids[3], before=ids[2])
    return {ids[3]: ("Moved",)}


REAL_CHANGES = [
    ("a word in a description", _desc_word),
    ("whitespace inside a description", _desc_inner_space),
    ("an acknowledged commit", _ack),
    ("the outline order", _swap),
]


@pytest.mark.parametrize("case, change", REAL_CHANGES, ids=[c[0] for c in REAL_CHANGES])
def test_22_real_change_in_rewritten_files_is_still_reported(canon_repo, case, change):
    """MELLON-VER-SRS-22's control (with -21, -23, -24): a change the canonical form
    does show is reported although every file was also rewritten in each way that
    doesn't show. Whitespace inside a description's text isn't between elements,
    so it stays in the canonical form and is a change."""
    root, info = canon_repo
    expected = change(root, root / DOC, info["ids"])
    _rewrite(root, "**/*.xml", _everything)
    c2 = _commit(root, f"real change: {case}")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    assert _kinds(d) == expected, f"{case}: expected exactly {expected}, Compare reports {_reported(d)}"


def test_22_real_figure_change_in_rewritten_figure_is_still_reported(canon_repo):
    """MELLON-VER-SRS-22's control for the figure (with -21): a figure whose colour
    changed is Modified although its file was also rewritten in ways that don't show."""
    root, info = canon_repo
    path = root / DOC / "figures" / f"{info['ids'][2]}.svg"
    path.write_bytes(path.read_bytes().replace(b"#ff0000", b"#0000ff"))
    for _, rewrite in FIGURE_REWRITES:
        _rewrite(root, "figures/*.svg", rewrite)
    c2 = _commit(root, "real figure change")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    expected = {info["ids"][2]: ("Modified",)}
    assert _kinds(d) == expected, f"expected exactly {expected}, Compare reports {_reported(d)}"


def test_22_compare_dialog_lists_no_row_for_canonical_only_rewrite(qtbot, canon_repo):
    """MELLON-VER-SRS-22 through the Compare dialog: every document file except the
    figure rewritten in all the ways at once; the dialog lists no row."""
    root, info = canon_repo
    _rewrite(root, "**/*.xml", _everything)
    c2 = _commit(root, "rewrite everything")
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win, info["c1"], c2)
    assert dlg.diff is not None, f"precondition: no result. Message: {dlg.message_label.text()!r}"
    assert _rows(dlg) == {}, f"a canonical-only rewrite is no change, the dialog lists {_rows(dlg)}"


def test_22_mcp_compare_reports_no_change_for_canonical_only_rewrite(canon_repo, tmp_path):
    """MELLON-VER-SRS-22 through the MCP server's compare tool, over stdio: the same
    rewrite as the dialog test; the tool returns no change."""
    root, info = canon_repo
    _rewrite(root, "**/*.xml", _everything)
    c2 = _commit(root, "rewrite everything")
    [result] = mcp_session(root, [{"doc_id": DOC, "from_ref": info["c1"], "to_ref": c2}], tmp_path / "mcp.log")
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    payload = _payload(result)
    assert payload["changes"] == [], f"a canonical-only rewrite is no change, the tool returns {payload['changes']}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-56: comparing changes nothing but by saving pending edits
# ---------------------------------------------------------------------------


def _build_dirty_repo(root: Path) -> dict:
    """SPEC-0..SPEC-2 at "seed" (C1); "revise" (C2 = HEAD) changes SPEC-1. A branch
    `side` and an annotated tag `v1` on C1. Then a working copy that differs from
    the index, which differs from HEAD: SPEC-0 changed and not staged; SPEC-1
    changed, staged, and changed again; two untracked files. SPEC-2 is clean, for
    the pending edit."""
    _init(root)
    folder = editor.create_document(root, DOC, title="Dirty")
    ids = [editor.create_object(folder, None, f"Object {k}.") for k in range(3)]
    c1 = _commit(root, "seed")
    editor.set_description(folder, ids[1], "Object 1, revised.")
    c2 = _commit(root, "revise")
    repo = pygit2.Repository(str(root))
    repo.references.create("refs/heads/side", pygit2.Oid(hex=c1))
    git_ops.create_tag(repo, "v1", pygit2.Oid(hex=c1), "Version 1")
    editor.set_description(folder, ids[0], "Object 0, not committed.")
    editor.set_description(folder, ids[1], "Object 1, staged.")
    index = repo.index
    index.read()
    index.add(f"{DOC}/objects/{ids[1]}.xml")
    index.write()
    editor.set_description(folder, ids[1], "Object 1, staged, then edited again.")
    (root / "notes.txt").write_text("untracked at the root\n", encoding="utf-8")
    (folder / "scratch.txt").write_text("untracked in the document folder\n", encoding="utf-8")
    return {"ids": ids, "c1": c1, "c2": c2}


def _snapshot(root: Path) -> dict[str, dict]:
    """The three things -56 protects. The working copy: every file outside .git,
    tracked or not, by bytes and modified time. The git index: its entries (path,
    blob, mode); a rewrite of git's stat cache alone isn't a change to the index.
    The git references: HEAD, packed-refs, every file under refs/ and each other
    *_HEAD file, by bytes."""
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] == ".git" or not p.is_file():
            continue
        files[rel] = (p.read_bytes(), p.stat().st_mtime_ns)
    repo = pygit2.Repository(str(root))
    index = {e.path: (str(e.id), e.mode) for e in repo.index}
    git = root / ".git"
    refs = {}
    for p in [git / "packed-refs", *git.glob("*HEAD"), *(git / "refs").rglob("*")]:
        if p.is_file():
            refs[p.relative_to(git).as_posix()] = p.read_bytes()
    return {"working copy": files, "git index": index, "git references": refs}


def _changed(before: dict, after: dict, part: str) -> set[str]:
    b, a = before[part], after[part]
    return {k for k in set(b) | set(a) if b.get(k) != a.get(k)}


PENDING = " Typed, not saved."
PARTS = ["working copy", "git index", "git references"]


def _scenario_two_commits(qtbot, win, info):
    dlg = _open_compare(qtbot, win, "side", diff.LATEST)
    dlg.from_combo.setEditText("v1^{commit}")
    _press_compare(qtbot, dlg)


def _scenario_working_copy_pending(qtbot, win, info):
    dlg = _open_compare(qtbot, win, diff.LATEST, WORKING_COPY)
    _type_without_leaving(qtbot, win, info["ids"][2], PENDING)
    _press_compare(qtbot, dlg)


def _scenario_two_commits_pending(qtbot, win, info):
    dlg = _open_compare(qtbot, win, "side", diff.LATEST)
    _type_without_leaving(qtbot, win, info["ids"][2], PENDING)
    _press_compare(qtbot, dlg)


SCENARIOS = [
    ("two commits", _scenario_two_commits, False),
    ("working copy, pending edit", _scenario_working_copy_pending, True),
    ("two commits, pending edit", _scenario_two_commits_pending, True),
]


@pytest.mark.parametrize("part", PARTS)
@pytest.mark.parametrize("case, run, pending", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_56_compare_dialog_changes_only_what_a_pending_edit_saves(qtbot, tmp_path, case, run, pending, part):
    """MELLON-VER-SRS-56 through the Compare dialog, in a working copy that differs
    from the index, which differs from HEAD. Comparing changes nothing in `part`,
    except that saving a pending edit may write the edited object's file. Assumed:
    a description edit saves only the object's own file."""
    root = tmp_path / "repo"
    info = _build_dirty_repo(root)
    win = _open_editor(qtbot, root, editable=True)
    before = _snapshot(root)
    run(qtbot, win, info)
    after = _snapshot(root)
    allowed = {f"{DOC}/objects/{info['ids'][2]}.xml"} if pending and part == "working copy" else set()
    extra = _changed(before, after, part) - allowed
    assert not extra, f"comparing ({case}) changed {sorted(extra)} in the {part}"


@pytest.fixture(scope="module")
def mcp_dirty(tmp_path_factory):
    """One MCP server session over stdio on the dirty repository, comparing two
    commits (named and typed), HEAD with the working copy and the working copy
    with a branch. Snapshots taken once the server is up and before it stops."""
    base = tmp_path_factory.mktemp("mcp56")
    root = base / "repo"
    info = _build_dirty_repo(root)
    snaps: dict[str, dict] = {}
    calls = [
        {"doc_id": DOC, "from_ref": info["c1"], "to_ref": info["c2"]},
        {"doc_id": DOC, "from_ref": "side", "to_ref": "HEAD"},
        {"doc_id": DOC, "from_ref": "HEAD~1", "to_ref": "v1"},
        {"doc_id": DOC, "from_ref": "HEAD", "to_ref": WORKING_COPY},
        {"doc_id": DOC, "from_ref": WORKING_COPY, "to_ref": "side"},
    ]
    results = mcp_session(
        root, calls, base / "mcp.log",
        on_ready=lambda: snaps.setdefault("before", _snapshot(root)),
        on_done=lambda: snaps.setdefault("after", _snapshot(root)),
    )
    return results, snaps


@pytest.mark.parametrize("part", PARTS)
def test_56_mcp_compare_changes_nothing(mcp_dirty, part):
    """MELLON-VER-SRS-56 through the MCP server's compare tool: five comparisons,
    two of them with the working copy, change nothing in `part`."""
    results, snaps = mcp_dirty
    failed = [r[1][:200] for r in results if _is_error(r)]
    assert not failed, f"precondition: comparisons failed: {failed}"
    changed = _changed(snaps["before"], snaps["after"], part)
    assert not changed, f"the MCP compare tool changed {sorted(changed)} in the {part}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-67: each result reports the commit each version resolved to,
# "by at least the first 7 digits of its hash" (settled with the person on
# 2026-11-07). The tests accept any run of 7 to 40 hex digits that begins the
# right commit's hash. They don't tie a hash to its side, but each side's
# commit differs, so the other side's hash can't stand in for it.
#
# MELLON-VER-SRS-87: the MCP server's compare tool returns the full hash.
# ---------------------------------------------------------------------------

_HEX_RUN = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])")


def _shows_hash_of(text: str, full: str) -> bool:
    """True when `text` holds at least the first 7 digits of the hash `full`."""
    return any(full.startswith(run) for run in _HEX_RUN.findall(text.lower()))


def _typed_result(qtbot, root: Path, from_text: str, to_text: str | None):
    """Open the Compare dialog on its defaults, then type From and To (None picks
    the working copy for To) and press Compare."""
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win)
    dlg.from_combo.setEditText(from_text)
    if to_text is None:
        dlg.to_combo.setCurrentIndex(dlg.to_combo.findData(WORKING_COPY))
    else:
        dlg.to_combo.setEditText(to_text)
    _press_compare(qtbot, dlg)
    return dlg


# (case, From typed, To typed, k of From's commit, k of To's commit)
TYPED_PAIRS = [
    ("branch to HEAD~1", "feature", "HEAD~1", 2, 4),
    ("annotated tag to remote-tracking branch", "v1", "origin/review", 1, 3),
]


@pytest.mark.parametrize("side", ["From", "To"])
@pytest.mark.parametrize("case, from_text, to_text, ka, kb", TYPED_PAIRS, ids=[p[0] for p in TYPED_PAIRS])
def test_67_dialog_shows_hash_of_typed_version(qtbot, rev_repo, case, from_text, to_text, ka, kb, side):
    """MELLON-VER-SRS-67: the dialog's result, with no row selected, reports at
    least the first 7 digits of the hash of the commit each typed version
    resolved to. The names typed don't hold the hash. An annotated tag resolves
    to its commit, so the commit's hash is wanted, not the tag object's. The
    dialog first showed its default result, for other commits, so a stale report
    fails too."""
    root, info = rev_repo
    dlg = _typed_result(qtbot, root, from_text, to_text)
    assert _changes(dlg) == _added(info, range(ka + 1, kb + 1)), (
        f"precondition: comparing {from_text!r} with {to_text!r} failed: {dlg.message_label.text()!r}"
    )
    k = ka if side == "From" else kb
    full = info["commits"][k]
    shown = _result_text(dlg)
    assert _shows_hash_of(shown, full), (
        f"{side} {from_text if side == 'From' else to_text!r} resolved to {full}; the result doesn't "
        f"report 7 or more of its first digits. It shows: {shown!r}"
    )


@pytest.mark.parametrize("side, k", [("From", 1), ("To", 5)])
def test_67_dialog_shows_hash_of_default_versions(qtbot, rev_repo, side, k):
    """MELLON-VER-SRS-67 for versions the dialog chose itself: opened by the
    Compare… path with no versions given, From is the current issue (C1) and To
    the latest commit (C5), per -12. The result reports at least the first 7
    digits of each one's hash."""
    root, info = rev_repo
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win)
    assert _changes(dlg) == _added(info, range(2, 6)), (
        f"precondition: the default comparison isn't issue 1 to the latest commit: {_rows(dlg)}"
    )
    full = info["commits"][k]
    shown = _result_text(dlg)
    assert _shows_hash_of(shown, full), (
        f"{side} resolved to {full}; the result doesn't report 7 or more of its first digits. It shows: {shown!r}"
    )


def test_67_dialog_shows_hash_of_from_when_to_is_working_copy(qtbot, rev_repo):
    """MELLON-VER-SRS-67: From typed as HEAD~1 (C4), To the working copy. The
    result reports at least the first 7 digits of C4's hash."""
    root, info = rev_repo
    dlg = _typed_result(qtbot, root, "HEAD~1", None)
    # SPEC-5 exists at To (the working copy, as C5) and not at From (C4): Added.
    assert _changes(dlg) == {info["ids"][5]: "Added"}, f"precondition: unexpected rows {_rows(dlg)}"
    shown = _result_text(dlg)
    full = info["commits"][4]
    assert _shows_hash_of(shown, full), (
        f"From resolved to {full}; the result doesn't report 7 or more of its first digits. It shows: {shown!r}"
    )


def test_67_dialog_says_to_was_the_working_copy(qtbot, rev_repo):
    """MELLON-VER-SRS-67: with To the working copy, the result says so. Weak: the
    words "working copy" can come from naming the versions (-41) alone."""
    root, info = rev_repo
    dlg = _typed_result(qtbot, root, "HEAD~1", None)
    shown = _result_text(dlg)
    assert "working copy" in shown.lower(), f"To was the working copy; the result doesn't say so. It shows: {shown!r}"


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _strings(k) + _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


MCP_67_CALLS = {
    "branch to HEAD~1": ({"from_ref": "feature", "to_ref": "HEAD~1"}, 2, 4),
    "defaults": ({}, 1, 5),
    "annotated tag to remote-tracking branch": ({"from_ref": "v1", "to_ref": "origin/review"}, 1, 3),
    "HEAD~1 to working copy": ({"from_ref": "HEAD~1", "to_ref": WORKING_COPY}, 4, None),
}


@pytest.fixture(scope="module")
def mcp_rev(rev_template, tmp_path_factory):
    """One MCP server session over stdio on a copy of the revisions repository."""
    src, info = rev_template
    base = tmp_path_factory.mktemp("mcp67")
    root = base / "repo"
    shutil.copytree(src, root)
    names = list(MCP_67_CALLS)
    calls = [{"doc_id": DOC, **MCP_67_CALLS[n][0]} for n in names]
    results = mcp_session(root, calls, base / "mcp.log")
    return info, dict(zip(names, results))


MCP_67_HASH_CASES = [
    (name, side) for name, (_, ka, kb) in MCP_67_CALLS.items() for side, k in (("From", ka), ("To", kb)) if k is not None
]


@pytest.mark.parametrize("name, side", MCP_67_HASH_CASES, ids=[f"{n}-{s}" for n, s in MCP_67_HASH_CASES])
def test_67_mcp_result_reports_hash_of_each_version(mcp_rev, name, side):
    """MELLON-VER-SRS-67 through the MCP server's compare tool: the result holds at
    least the first 7 digits of the hash of the commit each version resolved to,
    for typed names, an annotated tag (its commit, not the tag object) and the
    tool's defaults (latest issue, latest commit)."""
    info, results = mcp_rev
    result = results[name]
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    _, ka, kb = MCP_67_CALLS[name]
    full = info["commits"][ka if side == "From" else kb]
    strings = _strings(_payload(result))
    assert any(_shows_hash_of(s, full) for s in strings), (
        f"{name}: {side} resolved to {full}; the result doesn't hold 7 or more of its first digits. "
        f"Top-level values: { {k: v for k, v in _payload(result).items() if k != 'changes'} }"
    )


@pytest.mark.parametrize("name, side", MCP_67_HASH_CASES, ids=[f"{n}-{s}" for n, s in MCP_67_HASH_CASES])
def test_87_mcp_result_reports_full_hash_of_each_version(mcp_rev, name, side):
    """MELLON-VER-SRS-87: the MCP server's compare tool returns the full 40-digit
    hash of the commit each version resolved to, for typed names, an annotated
    tag (its commit, not the tag object) and the tool's defaults (latest issue,
    latest commit). With To the working copy, only From has a commit."""
    info, results = mcp_rev
    result = results[name]
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    _, ka, kb = MCP_67_CALLS[name]
    full = info["commits"][ka if side == "From" else kb]
    strings = _strings(_payload(result))
    assert any(full in s.lower() for s in strings), (
        f"{name}: {side} resolved to {full}; the result doesn't hold that full hash. "
        f"Top-level values: { {k: v for k, v in _payload(result).items() if k != 'changes'} }"
    )


def test_67_mcp_result_says_to_was_the_working_copy(mcp_rev):
    """MELLON-VER-SRS-67 through the MCP server: with To the working copy, the
    result says so. Weak: echoing the version's name is enough to pass."""
    info, results = mcp_rev
    result = results["HEAD~1 to working copy"]
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    strings = _strings(_payload(result))
    assert any("working copy" in s.lower() for s in strings), "the result doesn't say To was the working copy"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-73: pending edits are saved before comparing with the working copy
# ---------------------------------------------------------------------------


def _build_pending_repo(root: Path) -> dict:
    """SPEC-0..SPEC-2 ("Object k.") at "seed" (C1); "revise" (C2 = HEAD) changes
    SPEC-1 only. The working copy is clean."""
    _init(root)
    folder = editor.create_document(root, DOC, title="Pending")
    ids = [editor.create_object(folder, None, f"Object {k}.") for k in range(3)]
    c1 = _commit(root, "seed")
    editor.set_description(folder, ids[1], "Object 1, revised.")
    c2 = _commit(root, "revise")
    return {"ids": ids, "c1": c1, "c2": c2}


def _pending_compare(qtbot, tmp_path, from_ref, to_ref):
    """Open the dialog comparing `from_ref` with `to_ref`, type into SPEC-2's
    description cell without leaving it, then press Compare."""
    root = tmp_path / "repo"
    info = _build_pending_repo(root)
    win = _open_editor(qtbot, root, editable=True)
    dlg = _open_compare(qtbot, win, from_ref(info), to_ref(info))
    target = info["ids"][2]
    view, widget = _type_without_leaving(qtbot, win, target, PENDING)
    assert view.state() == QAbstractItemView.State.EditingState, "precondition: the cell editor closed"
    assert _disk_description(root, target) == "Object 2.", "precondition: the typed text was saved before Compare"
    _press_compare(qtbot, dlg)
    return root, info, dlg, widget


TYPED_LINE = "Object 2." + PENDING


@pytest.mark.parametrize(
    "side, from_ref, to_ref",
    [("To", lambda i: diff.LATEST, lambda i: WORKING_COPY), ("From", lambda i: WORKING_COPY, lambda i: diff.LATEST)],
    ids=["working copy as To", "working copy as From"],
)
def test_73_pending_edit_is_part_of_working_copy_comparison(qtbot, tmp_path, side, from_ref, to_ref):
    """MELLON-VER-SRS-73: the working copy equals the latest commit except for text
    typed in a cell that is still open. Comparing the two must list that object,
    and only it, as Modified, with the working copy on either side."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, from_ref, to_ref)
    expected = {info["ids"][2]: "Modified"}
    assert _changes(dlg) == expected, (
        f"working copy as {side}: the pending edit wasn't compared; expected {expected}, "
        f"the dialog lists {_rows(dlg)}"
    )


def test_73_row_shows_the_typed_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73 with -32: the row of the edited object shows the first line
    of its description at To, the working copy, which is the typed text."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, lambda i: diff.LATEST, lambda i: WORKING_COPY)
    row = _rows(dlg).get(info["ids"][2])
    assert row is not None and row[1] == TYPED_LINE, f"expected the row to show {TYPED_LINE!r}, got {row}"


def test_73_file_holds_the_typed_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73: comparing with the working copy first saves the pending
    edit, so afterwards the object's file holds the typed text."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, lambda i: diff.LATEST, lambda i: WORKING_COPY)
    on_disk = _disk_description(root, info["ids"][2])
    assert on_disk == TYPED_LINE, f"expected the file to hold {TYPED_LINE!r}, it holds {on_disk!r}"


def test_73_two_commit_comparison_leaves_out_pending_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73 doesn't apply when neither version is the working copy.
    Assumed: such a comparison is of the two commits alone (-19 to -21), whether or
    not the pending edit is saved (-56 allows saving it). Seed to the latest commit
    changed SPEC-1 only, so SPEC-1 is the only row."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, lambda i: i["c1"], lambda i: diff.LATEST)
    expected = {info["ids"][1]: "Modified"}
    assert _changes(dlg) == expected, f"expected exactly {expected}, the dialog lists {_rows(dlg)}"


def test_73_two_commit_comparison_keeps_pending_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73, two commits. Assumed: -73 requires no save here, and -56
    allows one, so either the typed text is still in the open cell editor or the
    file holds it. Losing it is neither."""
    root, info, dlg, widget = _pending_compare(qtbot, tmp_path, lambda i: i["c1"], lambda i: diff.LATEST)
    in_editor = _editor_text(widget)
    on_disk = _disk_description(root, info["ids"][2])
    assert in_editor == TYPED_LINE or on_disk == TYPED_LINE, (
        f"the typed text is gone: editor holds {in_editor!r}, the file holds {on_disk!r}"
    )
