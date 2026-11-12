"""Shared helpers for the MELLON-VER-SRS blind tests (docs/specification.md, step 7).

Repositories built on disk in the on-disk format of USAGE.md and committed with the
git command line under an empty home and global config (the module fixture
_git_cli_home, imported by each test module that uses these helpers); readers for
commits, the index, references and the working copy; the MCP server in process and
over stdio; and an offscreen driver for the Document Editor, the Commit dialog, the
History tab, the Project Explorer and the Merge dialog. Fixtures defined here are
imported by name into the test modules that use them.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pygit2
import pytest
from lxml import etree
from pygit2.enums import ConfigLevel
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

from mellon import merge
from mellon.gui.dialogs.commit_dialog import CommitDialog
from mellon.gui.dialogs.merge_dialog import MergeDialog
from mellon.gui.dialogs.object_at_commit import ObjectAtCommitDialog
from mellon.gui.main_window import DocumentEditorWindow
from mellon.gui.project_explorer_window import ProjectExplorerWindow
from mellon.service.document import DocumentService
from tests.edit_b_support import _plain
from tests.store_support import DECL


ATTRIBUTES = (
    '    <attribute name="type" kind="enum" required="true">\n'
    "      <option>Heading</option>\n"
    "      <option>Requirement</option>\n"
    "    </attribute>\n"
    '    <attribute name="status" kind="enum" required="false">\n'
    "      <option>Draft</option>\n"
    "      <option>Approved</option>\n"
    "    </attribute>\n"
    '    <attribute name="priority" kind="enum" required="false">\n'
    "      <option>High</option>\n"
    "      <option>Low</option>\n"
    "    </attribute>\n"
    '    <attribute name="rationale" kind="text" required="false"/>\n'
)


SVG_RED = (
    DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    '<rect width="20" height="10" fill="red"/></svg>\n'
)


SVG_BLUE = SVG_RED.replace("red", "blue")


IDENTITY = ("Test Person", "test@example.invalid")


_CLI_HOME: Path | None = None


_TICK = [0]


@pytest.fixture(autouse=True, scope="module")
def _git_cli_home(tmp_path_factory):
    """An empty home and global config for the git command line, so the
    tests never read or change the user's own git settings."""
    global _CLI_HOME
    _CLI_HOME = tmp_path_factory.mktemp("git-cli-home")
    (_CLI_HOME / "gitconfig").write_text("", newline="\n")
    yield


def _cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=str(_CLI_HOME / "gitconfig"),
        XDG_CONFIG_HOME=str(_CLI_HOME),
        HOME=str(_CLI_HOME),
        GIT_TERMINAL_PROMPT="0",
        GIT_EDITOR="true",
        GIT_SEQUENCE_EDITOR="true",
        GIT_MERGE_AUTOEDIT="no",
    )
    if extra:
        env.update(extra)
    return env


def _git(root: Path, *args: str, check: bool = True, env: dict | None = None):
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=_cli_env(env),
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def _dates(tick: int | None) -> dict[str, str]:
    if tick is None:
        _TICK[0] += 1
        tick = 1000 + _TICK[0]
    seconds = 1767225600 + tick * 60
    stamp = f"@{seconds} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def _commit(root: Path, message: str, tick: int | None = None) -> str:
    """Commit every change in the work tree from the git command line."""
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-verify", "-m", message, env=_dates(tick))
    return _head(root)


def _write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")


def _manifest(
    doc: str,
    outline: list[str],
    *,
    parents: tuple[str, ...] = (),
    title: str = "Title",
    author: str = "Ann Author",
    date: str = "2026-01-01",
    next_number: int | None = None,
    attributes: str = ATTRIBUTES,
) -> str:
    nxt = f' next-object-number="{next_number}"' if next_number is not None else ""
    par = "".join(f'    <parent doc="{p}"/>\n' for p in parents)
    out = "".join(f'    <ref id="{i}"/>\n' for i in outline)
    return (
        DECL
        + f'<document id="{doc}"{nxt}>\n'
        + "  <metadata>\n"
        + f"    <title>{title}</title>\n"
        + f"    <author>{author}</author>\n"
        + f"    <date>{date}</date>\n"
        + "  </metadata>\n"
        + f"  <parents>\n{par}  </parents>\n"
        + f"  <attributes>\n{attributes}  </attributes>\n"
        + f"  <outline>\n{out}  </outline>\n"
        + "</document>\n"
    )


def _obj(
    oid: str,
    desc: str,
    *,
    type_: str = "Requirement",
    status: str | None = "Draft",
    priority: str | None = None,
    table: str = "",
) -> str:
    lines = [DECL + f'<object id="{oid}">\n', f'  <attr name="type">{type_}</attr>\n']
    if status is not None:
        lines.append(f'  <attr name="status">{status}</attr>\n')
    if priority is not None:
        lines.append(f'  <attr name="priority">{priority}</attr>\n')
    lines.append(f"  <description>{desc}</description>\n")
    lines.append(table)
    lines.append("</object>\n")
    return "".join(lines)


def _links(source: str, links: list[tuple[str, str, str, str | None]]) -> str:
    body = ""
    for tdoc, tid, ltype, ack in links:
        a = f' ack="{ack}"' if ack else ""
        body += f'  <link target-doc="{tdoc}" target-id="{tid}" type="{ltype}"{a}/>\n'
    return DECL + f'<links source="{source}">\n{body}</links>\n'


def _init(root: Path, identity: tuple[str, str] = IDENTITY) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", identity[0])
    _git(root, "config", "user.email", identity[1])
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "commit.gpgsign", "false")


def _clear(root: Path) -> None:
    for entry in root.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _write_base_docs(root: Path) -> None:
    """PARENT (PARENT-0 heading, PARENT-1, PARENT-2) and CHILD (CHILD-0,
    CHILD-1), CHILD declaring PARENT; CHILD-0 satisfies PARENT-1 (no ack)."""
    p = root / "PARENT"
    _write(p / "PARENT.xml", _manifest("PARENT", ["PARENT-0", "PARENT-1", "PARENT-2"], title="Parent"))
    _write(p / "objects" / "PARENT-0.xml", _obj("PARENT-0", "Parent heading", type_="Heading", status=None))
    _write(p / "objects" / "PARENT-1.xml", _obj("PARENT-1", "The system shall start."))
    _write(p / "objects" / "PARENT-2.xml", _obj("PARENT-2", "The system shall stop."))
    c = root / "CHILD"
    _write(c / "CHILD.xml", _manifest("CHILD", ["CHILD-0", "CHILD-1"], parents=("PARENT",), title="Child"))
    _write(c / "objects" / "CHILD-0.xml", _obj("CHILD-0", "Text of CHILD-0."))
    _write(c / "objects" / "CHILD-1.xml", _obj("CHILD-1", "Text of CHILD-1."))
    _write(c / "links" / "CHILD-0.xml", _links("CHILD-0", [("PARENT", "PARENT-1", "satisfies", None)]))


def _make_repo(root: Path, identity: tuple[str, str] = IDENTITY) -> Path:
    _init(root, identity)
    _write_base_docs(root)
    _commit(root, "seed", tick=1)
    return root


def _mcp_root(mcp_repo, identity: tuple[str, str] = IDENTITY) -> Path:
    """The MCP server's root, rebuilt as a committed repository of PARENT and CHILD."""
    root = Path(mcp_repo)
    _clear(root)
    return _make_repo(root, identity)


# --------------------------------------------------------------------------
# Reading git state
# --------------------------------------------------------------------------


def _head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return proc.stdout.strip() or None


def _rev(root: Path, rev: str) -> str:
    return _git(root, "rev-parse", rev).stdout.strip()


def _parents(root: Path, rev: str = "HEAD") -> list[str]:
    line = _git(root, "rev-list", "--parents", "-n", "1", rev).stdout.split()
    return line[1:]


def _tree(root: Path, rev: str = "HEAD") -> dict[str, bytes]:
    repo = pygit2.Repository(str(root))
    commit = repo.revparse_single(rev).peel(pygit2.Commit)
    out: dict[str, bytes] = {}

    def walk(tree, prefix):
        for entry in tree:
            path = f"{prefix}{entry.name}"
            if entry.type_str == "tree":
                walk(repo[entry.id], path + "/")
            elif entry.type_str == "blob":
                out[path] = repo[entry.id].data
            else:
                out[path] = b"<" + entry.type_str.encode() + b">"

    walk(commit.tree, "")
    return out


def _status(root: Path, *paths: str) -> list[str]:
    out = _git(root, "status", "--porcelain=v1", "-uall", "--", *paths).stdout
    return [line for line in out.splitlines() if line]


def _unmerged(root: Path) -> list[str]:
    out = _git(root, "diff", "--name-only", "--diff-filter=U").stdout
    return [line for line in out.splitlines() if line]


def _work_files(root: Path, sub: str = "", *, include_git: bool = False) -> dict[str, str]:
    """sha256 of every file under root/sub, keyed by posix path."""
    base = root / sub if sub else root
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not include_git and (rel == ".git" or rel.startswith(".git/")):
            continue
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {p for p in set(before) | set(after) if before.get(p) != after.get(p)}


def _index_entries(root: Path) -> list[tuple[str, str, str, str]]:
    """(path, mode, blob id, stage) of every git index entry."""
    out = _git(root, "ls-files", "-s").stdout
    entries = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode, oid, stage = meta.split()
        entries.append((path, mode, oid, stage))
    return sorted(entries)


def _refs(root: Path) -> dict[str, str]:
    out = _git(root, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    refs = dict(line.split(" ", 1) for line in out.splitlines() if line)
    head = _git(root, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    refs["HEAD -> "] = head or _rev(root, "HEAD")
    return refs


def _xml(data: bytes | str):
    if isinstance(data, str):
        data = data.encode()
    return etree.fromstring(data)


def _description(data: bytes) -> str:
    return (_xml(data).findtext("description") or "").strip()


def _attr(data: bytes, name: str) -> str | None:
    for a in _xml(data).findall("attr"):
        if a.get("name") == name:
            return a.text or ""
    return None


def _assert_identity(root: Path, name: str, email: str, rev: str = "HEAD") -> None:
    repo = pygit2.Repository(str(root))
    c = repo.revparse_single(rev).peel(pygit2.Commit)
    assert (c.author.name, c.author.email) == (name, email), (
        f"commit author is {c.author.name} <{c.author.email}>, expected {name} <{email}>"
    )
    assert (c.committer.name, c.committer.email) == (name, email), (
        f"commit committer is {c.committer.name} <{c.committer.email}>, expected {name} <{email}>"
    )


@pytest.fixture
def only_repo_git_config(tmp_path_factory):
    """Point libgit2's global, XDG, system and program-data config at an empty
    folder, so the repository's own config is the only git setting."""
    empty = tmp_path_factory.mktemp("no-outer-git-config")
    levels = [ConfigLevel.GLOBAL, ConfigLevel.XDG, ConfigLevel.SYSTEM, ConfigLevel.PROGRAMDATA]
    saved = {lvl: pygit2.settings.search_path[lvl] for lvl in levels}
    for lvl in levels:
        pygit2.settings.search_path[lvl] = str(empty)
    yield
    for lvl, value in saved.items():
        pygit2.settings.search_path[lvl] = value


# --------------------------------------------------------------------------
# GUI helpers
# --------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _model_texts(model) -> list[str]:
    out: list[str] = []
    if model is None:
        return out

    def walk(parent):
        for r in range(model.rowCount(parent)):
            for c in range(model.columnCount(parent)):
                idx = model.index(r, c, parent)
                v = model.data(idx)
                if v is not None:
                    out.append(str(v))
                tip = model.data(idx, Qt.ItemDataRole.ToolTipRole)
                if tip:
                    out.append(str(tip))
            walk(model.index(r, 0, parent))

    from PySide6.QtCore import QModelIndex

    walk(QModelIndex())
    return out


def _texts(widget: QWidget) -> list[str]:
    """Every piece of text a widget shows: labels, buttons, fields, lists."""
    out = [widget.windowTitle()]
    for c in widget.findChildren(QWidget):
        if isinstance(c, QLabel):
            out.append(_strip_html(c.text()))
        elif isinstance(c, QAbstractButton):
            out.append(c.text())
        elif isinstance(c, QLineEdit):
            out.append(c.text())
        elif isinstance(c, (QPlainTextEdit, QTextEdit)):
            out.append(c.toPlainText())
        elif isinstance(c, QComboBox):
            out.extend(c.itemText(i) for i in range(c.count()))
        elif isinstance(c, QGroupBox):
            out.append(c.title())
        elif isinstance(c, QAbstractItemView) and not isinstance(c, QListWidget):
            out.extend(_model_texts(c.model()))
        if isinstance(c, QListWidget):
            out.extend(c.item(i).text() for i in range(c.count()))
        if c.toolTip():
            out.append(_strip_html(c.toolTip()))
    return [t for t in out if t]


def _list_items(dialog: QWidget) -> list[str]:
    items: list[str] = []
    for lw in dialog.findChildren(QListWidget):
        items.extend(lw.item(i).text() for i in range(lw.count()))
    return items


def _action(window: QWidget, name: str) -> QAction:
    found = [a for a in window.findChildren(QAction) if _plain(a.text()) == name]
    if not found:
        found = [a for a in window.findChildren(QAction) if _plain(a.text()).startswith(name)]
    assert found, f"no action named {name!r} in {type(window).__name__}"
    return found[0]


def _activate(qtbot, window: QWidget) -> None:
    """Deliver a window activation, as switching back to the window does."""
    window.event(QEvent(QEvent.Type.WindowActivate))
    qtbot.wait(20)


def _status_labels(window) -> list[str]:
    sb = window.statusBar()
    out = []
    for lab in sb.findChildren(QLabel):
        out.append(_strip_html(lab.text()))
        if lab.toolTip():
            out.append(_strip_html(lab.toolTip()))
    if sb.toolTip():
        out.append(sb.toolTip())
    return out


def _button(dialog: QWidget, text: str) -> QPushButton:
    found = [b for b in dialog.findChildren(QPushButton) if _plain(b.text()) == text]
    assert found, f"no button {text!r}; buttons: {[b.text() for b in dialog.findChildren(QPushButton)]}"
    return found[0]


class _Ui:
    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []
        self.answer = QMessageBox.StandardButton.Cancel
        self.commit_dialogs: list[CommitDialog] = []
        self.commit_dialog_texts: list[list[str]] = []
        self.commit_dialog_items: list[list[str]] = []
        self.on_commit_dialog = lambda dlg: QDialog.DialogCode.Rejected
        self.object_dialogs: list[ObjectAtCommitDialog] = []

    def message_text(self) -> str:
        return "\n".join(f"{k}: {t}: {x}" for k, t, x in self.messages)


@pytest.fixture
def ui(monkeypatch):
    """Patch every modal the tests reach: message boxes record their text and
    answer `ui.answer`; the commit dialog runs `ui.on_commit_dialog`."""
    rec = _Ui()

    def static(kind):
        def box(parent=None, title="", text="", *args, **kwargs):
            rec.messages.append((kind, str(title), str(text)))
            return rec.answer

        return staticmethod(box)

    for kind in ("question", "warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, kind, static(kind))

    def mb_exec(self):
        rec.messages.append(
            ("exec", self.windowTitle(), "\n".join([self.text(), self.informativeText(), self.detailedText()]))
        )
        return rec.answer

    monkeypatch.setattr(QMessageBox, "exec", mb_exec)

    def commit_exec(self):
        rec.commit_dialogs.append(self)
        rec.commit_dialog_texts.append(_texts(self))
        rec.commit_dialog_items.append(_list_items(self))
        return rec.on_commit_dialog(self)

    monkeypatch.setattr(CommitDialog, "exec", commit_exec)

    def object_exec(self):
        rec.object_dialogs.append(self)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ObjectAtCommitDialog, "exec", object_exec)
    monkeypatch.setattr(ObjectAtCommitDialog, "open", lambda self: rec.object_dialogs.append(self))
    return rec


def _open_editor(qtbot, root: Path, doc: str = "CHILD", *, writable: bool = True) -> DocumentEditorWindow:
    w = DocumentEditorWindow(doc, DocumentService(root))
    qtbot.addWidget(w)
    w.show()
    ro = _action(w, "Read-only")
    if writable and ro.isChecked():
        ro.trigger()
    if writable:
        assert not ro.isChecked(), "could not leave read-only mode"
    return w


def _open_explorer(qtbot, root: Path) -> ProjectExplorerWindow:
    pe = ProjectExplorerWindow()
    qtbot.addWidget(pe)
    pe.show()
    assert pe.open_repo(root), "the Project Explorer refused the repository"
    return pe


def _grid_index(w: DocumentEditorWindow, obj_id: str, column_title: str):
    t = w.grid_pane.table
    m = t.model()
    cols = [str(m.headerData(c, Qt.Orientation.Horizontal) or "") for c in range(m.columnCount())]
    col = cols.index(column_title)
    id_col = cols.index("ID")
    from PySide6.QtCore import QModelIndex

    def find(parent):
        for r in range(m.rowCount(parent)):
            if m.data(m.index(r, id_col, parent)) == obj_id:
                return m.index(r, col, parent)
            sub = find(m.index(r, 0, parent))
            if sub is not None:
                return sub
        return None

    idx = find(QModelIndex())
    assert idx is not None, f"{obj_id} is not in the grid"
    return idx


def _inspector_description(w: DocumentEditorWindow):
    edits = [e for e in w.inspector.findChildren(QTextEdit) if not e.isReadOnly()]
    edits += [e for e in w.inspector.findChildren(QPlainTextEdit) if not e.isReadOnly()]
    assert edits, "no editable description field in the Inspector"
    return edits[0]


# --------------------------------------------------------------------------
# MCP over stdio
# --------------------------------------------------------------------------


def _stdio(root: Path, work, tmp: Path):
    """Run `await work(session)` against `mellon-mcp --repo-root root` over stdio."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = tmp / "stdio-home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys; from mellon.mcp.server import main; sys.exit(main())", "--repo-root", str(root)],
        env=env,
        cwd=str(root),
    )
    errlog = open(tmp / "stdio-stderr.txt", "w", encoding="utf-8")

    async def run():
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await work(session)

    try:
        return anyio.run(run)
    finally:
        errlog.close()


def _unborn_doc(root: Path) -> dict[str, str]:
    files = {
        "DOC/DOC.xml": _manifest("DOC", ["DOC-0"]),
        "DOC/objects/DOC-0.xml": _obj("DOC-0", "The first object."),
        "notes.txt": "not a document file\n",
    }
    for rel, text in files.items():
        _write(root / rel, text)
    return files


ANN = ("Ann Example", "ann@example.invalid")


def _start_clean_merge(root: Path) -> None:
    """A merge stopped by `--no-commit`, with no conflicting file."""
    _git(root, "checkout", "-q", "-b", "side")
    _write(root / "PARENT/objects/PARENT-2.xml", _obj("PARENT-2", "Changed on side."))
    _commit(root, "side changes PARENT-2", tick=2)
    _git(root, "checkout", "-q", "main")
    _write(root / "PARENT/objects/PARENT-1.xml", _obj("PARENT-1", "Changed on main."))
    _commit(root, "main changes PARENT-1", tick=3)
    _git(root, "merge", "--no-commit", "--no-ff", "side")
    assert (root / ".git/MERGE_HEAD").exists(), "the merge didn't stop"
    assert _unmerged(root) == [], "the merge has a conflicting file"


GIT_OPERATIONS = [
    "rebase",
    "cherry_pick",
    "revert",
    "revert_no_commit",
    "cherry_pick_range_no_commit",
]


def _stop_git_operation(root: Path, op: str) -> None:
    """Leave `op` stopped, with its conflicting file marked resolved."""
    f = root / "CHILD/objects/CHILD-1.xml"

    def resolve():
        _write(f, _obj("CHILD-1", "Resolved by hand."))
        _git(root, "add", "CHILD/objects/CHILD-1.xml")

    if op in ("rebase", "cherry_pick", "cherry_pick_range_no_commit"):
        _git(root, "checkout", "-q", "-b", "side")
        _write(f, _obj("CHILD-1", "Side text one."))
        _commit(root, "side one", tick=2)
        _write(f, _obj("CHILD-1", "Side text two."))
        _commit(root, "side two", tick=3)
        _git(root, "checkout", "-q", "main")
        _write(f, _obj("CHILD-1", "Main text."))
        _commit(root, "main", tick=4)
        if op == "rebase":
            _git(root, "checkout", "-q", "side")
            _git(root, "rebase", "main", check=False)
        elif op == "cherry_pick":
            _git(root, "cherry-pick", "side", check=False)
        else:
            _git(root, "cherry-pick", "--no-commit", "side~1..side", check=False)
        assert _unmerged(root), f"{op} didn't stop on a conflict"
        resolve()
    elif op == "revert":
        _write(f, _obj("CHILD-1", "Text A."))
        a = _commit(root, "A", tick=2)
        _write(f, _obj("CHILD-1", "Text B."))
        _commit(root, "B", tick=3)
        _git(root, "revert", "--no-edit", a, check=False)
        assert _unmerged(root), "revert didn't stop on a conflict"
        resolve()
    elif op == "revert_no_commit":
        _write(f, _obj("CHILD-1", "Text A."))
        _commit(root, "A", tick=2)
        _git(root, "revert", "--no-commit", "HEAD")
    status = _git(root, "status").stdout
    assert re.search(r"in progress|currently (rebasing|cherry-picking|reverting)", status), (
        f"git reports no {op} in progress:\n{status}"
    )
    assert _unmerged(root) == []


def _install_hooks(root: Path, markers: Path, hooks: Path | None = None) -> None:
    markers.mkdir()
    hooks = hooks or root / ".git/hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name, code in (("pre-commit", 1), ("commit-msg", 0), ("post-commit", 0)):
        path = hooks / name
        _write(path, f'#!/bin/sh\necho ran > "{(markers / name).as_posix()}"\nexit {code}\n')
        path.chmod(0o755)


def _assert_hooks_live(root: Path, markers: Path) -> None:
    """Control: git itself runs the hooks, so the markers would show a run."""
    proc = _git(root, "commit", "--allow-empty", "-q", "-m", "control", check=False)
    assert proc.returncode != 0 and (markers / "pre-commit").exists(), "the test hooks don't run under git"


def _type_into(qtbot, editor, text: str) -> None:
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, text)


def _two_status_commits(root: Path) -> tuple[str, str]:
    first = _head(root)
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Approved text.", status="Approved"))
    second = _commit(root, "approve CHILD-0", tick=2)
    return first, second


# ==========================================================================
# What Mellon merges
# ==========================================================================


def _make_doc_repo(root: Path, objects: dict[str, str] | None = None, *, extra: dict[str, str] | None = None,
                   title: str = "Base title", parents: tuple[str, ...] = (), next_number: int | None = None) -> Path:
    _init(root)
    objects = objects or {
        "DOC-1": _obj("DOC-1", "One."),
        "DOC-2": _obj("DOC-2", "Two."),
        "DOC-3": _obj("DOC-3", "Three."),
    }
    _write(root / "DOC/DOC.xml", _manifest("DOC", list(objects), title=title, parents=parents, next_number=next_number))
    for oid, text in objects.items():
        _write(root / f"DOC/objects/{oid}.xml", text)
    for rel, text in (extra or {}).items():
        _write(root / rel, text)
    _commit(root, "seed", tick=1)
    return root


def _branch(root: Path, name: str, start: str = "main") -> None:
    _git(root, "checkout", "-q", "-b", name, start)


def _merge_stops(root: Path, theirs: str, *paths: str) -> None:
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", theirs, check=False)
    pend = merge.pending(root) or ()
    for p in paths:
        assert p in pend, f"{p} doesn't conflict; conflicting: {pend}"


def _merge_dialog(qtbot, root: Path) -> MergeDialog:
    dlg = MergeDialog(root, merge.plan(root))
    qtbot.addWidget(dlg)
    dlg.show()
    return dlg


# ==========================================================================
# Working with git clients
# ==========================================================================


def _remote(root: Path, tmp: Path) -> tuple[Path, Path]:
    """`origin` is a local bare repository that someone else has pushed to
    since the last fetch, so a fetch would move origin/main and a push would
    move the bare repository's main. A credential helper leaves a marker."""
    bare = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(root, "remote", "add", "origin", bare.as_posix())
    _git(root, "push", "-q", "origin", "main")
    _git(root, "fetch", "-q", "origin")
    (root / ".git/FETCH_HEAD").unlink(missing_ok=True)
    other = tmp / "other-clone"
    _git(tmp, "clone", "-q", bare.as_posix(), str(other))
    _git(other, "config", "user.name", "Other Person")
    _git(other, "config", "user.email", "other@example.invalid")
    _git(other, "config", "core.autocrlf", "false")
    _write(other / "PARENT/objects/PARENT-2.xml", _obj("PARENT-2", "Pushed by someone else."))
    _commit(other, "pushed elsewhere", tick=50)
    _git(other, "push", "-q", "origin", "main")
    marker = tmp / "credential-marker"
    _git(root, "config", "credential.helper", f"!f() {{ echo called > '{marker.as_posix()}'; }}; f")
    return bare, marker


def _remote_state(root: Path, bare: Path) -> dict:
    return {
        "remote refs": _refs(bare),
        "remote-tracking": {k: v for k, v in _refs(root).items() if k.startswith("refs/remotes/")},
        "FETCH_HEAD": (root / ".git/FETCH_HEAD").exists(),
    }


def _many_refs(root: Path, tmp: Path) -> None:
    """Branches, a remote-tracking branch, an annotated tag and a stash."""
    _write(root / "notes.txt", "notes\n")
    _commit(root, "notes", tick=2)
    _git(root, "tag", "-a", "v0", "-m", "an earlier milestone")
    _git(root, "branch", "feature")
    _remote(root, tmp)
    _write(root / "notes.txt", "stashed work\n")
    _git(root, "stash", "push", "-q", "-m", "work in progress")
    assert "refs/stash" in _refs(root)


def _assert_only_branch_and_tags_moved(before: dict, after: dict, commits: int, tags: set[str], root: Path) -> None:
    assert after["HEAD -> "] == "refs/heads/main", "HEAD no longer names the current branch"
    old, new = before["refs/heads/main"], after["refs/heads/main"]
    count = int(_git(root, "rev-list", "--count", f"{old}..{new}").stdout)
    assert count == commits and _git(root, "merge-base", "--is-ancestor", old, new, check=False).returncode == 0, (
        f"main moved by {count} commits, expected {commits} new commits on top of the old main"
    )
    kept = {k: v for k, v in before.items() if k not in ("refs/heads/main", "HEAD -> ")}
    assert {k: after.get(k) for k in kept} == kept, "a reference other than the current branch changed"
    assert set(after) - set(before) == {f"refs/tags/{t}" for t in tags}
