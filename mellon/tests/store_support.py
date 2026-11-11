"""Shared helpers for the MELLON-STORE-SRS blind tests (docs/specification.md, step 7).

Builders for documents on disk (USAGE.md, On-disk format), git helpers that keep
the user's git settings out, the programs and the MCP server run as processes
and over stdio, an offscreen GUI driver that answers message boxes, and a
fault injector for partial writes. Fixtures defined here are imported by name
into the test modules that use them, as compare_support.py's helpers are.
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import mellon.mcp.server as mcp_server
from mellon import editor
from mellon.loader import load_document
from mellon.repo import load_all
from mellon.schema import FilterRule, Table, TableCell, TableColumn, TableRow
from mellon.service import DocumentService


DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


DECL_LINE = b'<?xml version="1.0" encoding="UTF-8"?>'


SCRIPTS = Path(sys.executable).parent


_EXE = ".exe" if os.name == "nt" else ""


MCP_EXE = SCRIPTS / f"mellon-mcp{_EXE}"


CHECK_EXE = SCRIPTS / f"mellon-check{_EXE}"


CONFLICT_HINT = re.compile(r"merge conflict", re.IGNORECASE)


DEFAULT_ATTRS = (
    '    <attribute name="type" kind="enum" required="true">\n'
    "      <option>Heading</option>\n"
    "      <option>Requirement</option>\n"
    "    </attribute>\n"
    '    <attribute name="status" kind="enum" required="false">\n'
    "      <option>Draft</option>\n"
    "      <option>Approved</option>\n"
    "    </attribute>\n"
    '    <attribute name="rationale" kind="text" required="false"/>\n'
)


def manifest_xml(
    doc_id: str,
    outline_ids: tuple[str, ...] | list[str] = (),
    *,
    metadata: str | None = "<title>T</title><author>A</author><date>2026-04-21</date>",
    parents: tuple[str, ...] = (),
    attributes: str = DEFAULT_ATTRS,
    outline: str | None = None,
    root_attrs: str = "",
) -> str:
    parts = [DECL, f'<document id="{doc_id}"{root_attrs}>\n']
    if metadata is not None:
        parts.append(f"  <metadata>{metadata}</metadata>\n" if metadata else "  <metadata/>\n")
    if parents:
        parts.append("  <parents>\n")
        parts += [f'    <parent doc="{p}"/>\n' for p in parents]
        parts.append("  </parents>\n")
    else:
        parts.append("  <parents/>\n")
    parts.append(f"  <attributes>\n{attributes}  </attributes>\n")
    if outline is None:
        if outline_ids:
            parts.append("  <outline>\n")
            parts += [f'    <ref id="{i}"/>\n' for i in outline_ids]
            parts.append("  </outline>\n")
        else:
            parts.append("  <outline/>\n")
    else:
        parts.append(f"  <outline>{outline}</outline>\n")
    parts.append("</document>\n")
    return "".join(parts)


def object_xml(
    obj_id: str,
    *,
    attrs: tuple[tuple[str, str], ...] = (("type", "Requirement"),),
    description: str = "Some text",
    table: str = "",
) -> str:
    lines = [DECL, f'<object id="{obj_id}">\n']
    lines += [f'  <attr name="{n}">{v}</attr>\n' for n, v in attrs]
    lines.append(f"  <description>{description}</description>\n")
    if table:
        lines.append(f"  <table>{table}</table>\n")
    lines.append("</object>\n")
    return "".join(lines)


def links_xml(source: str, links: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'  <link target-doc="{d}" target-id="{i}" type="{t}"/>\n' for d, i, t in links
    )
    return f'{DECL}<links source="{source}">\n{body}</links>\n'


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def make_doc(
    root: Path,
    doc_id: str = "DOC",
    n: int = 2,
    *,
    parents: tuple[str, ...] = (),
    attributes: str = DEFAULT_ATTRS,
    metadata: str | None = "<title>T</title><author>A</author><date>2026-04-21</date>",
) -> Path:
    """A valid document DOC with objects DOC-1..DOC-n, each a Requirement."""
    folder = root / doc_id
    ids = [f"{doc_id}-{i}" for i in range(1, n + 1)]
    write(
        folder / f"{doc_id}.xml",
        manifest_xml(doc_id, ids, parents=parents, attributes=attributes, metadata=metadata),
    )
    for i, obj_id in enumerate(ids, start=1):
        write(folder / "objects" / f"{obj_id}.xml", object_xml(obj_id, description=f"Object {i}"))
    return folder


def load_with_errors(root: Path):
    errors: list = []
    docs = load_all(root, load_errors=errors)
    return docs, {e.doc_id: e for e in errors}


def validation_codes(root: Path, doc_id: str) -> list[str]:
    return [e.code for e in DocumentService(root).validate_document(doc_id)]


def snapshot(root: Path, *, include_git: bool = True) -> dict[str, str]:
    """Hash of every file under `root` (the working copy and, if asked, .git)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not include_git and (rel == ".git" or rel.startswith(".git/")):
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


@pytest.fixture(scope="session")
def git_env(tmp_path_factory):
    home = tmp_path_factory.mktemp("blind-store-git")
    cfg = home / "gitconfig"
    cfg.write_text("[core]\n\tautocrlf = false\n[init]\n\tdefaultBranch = main\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(cfg),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Blind Tester",
        GIT_AUTHOR_EMAIL="blind@example.invalid",
        GIT_COMMITTER_NAME="Blind Tester",
        GIT_COMMITTER_EMAIL="blind@example.invalid",
    )
    return env


def git(env: dict, cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


def git_init(env: dict, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(env, root, "init", "-q")
    return root


def git_commit_all(env: dict, root: Path, message: str) -> str:
    git(env, root, "add", "-A")
    git(env, root, "commit", "-q", "-m", message)
    return git(env, root, "rev-parse", "HEAD")


def not_in_work_tree(env: dict, folder: Path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "--is-inside-work-tree"],
        env=env, capture_output=True, text=True,
    )
    return r.returncode != 0


def no_identity_env(tmp_path: Path) -> dict:
    """An environment in which git has no user.name or user.email (T10)."""
    home = tmp_path / "_empty_home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home),
        HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive):],
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(home / ".gitconfig"),
    )
    r = subprocess.run(["git", "config", "user.name"], env=env, cwd=str(home),
                       capture_output=True, text=True)
    assert r.stdout.strip() == "", f"precondition: user.name must be unset, got {r.stdout!r}"
    return env


def run_program(exe: Path, root: Path, *, env: dict | None = None) -> tuple[int, str]:
    r = subprocess.run(
        [str(exe), "--repo-root", str(root)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=120, env=env,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _payload(result):
    assert not result.isError, f"tool error: {[getattr(c, 'text', c) for c in result.content]}"
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    texts = [getattr(c, "text", "") for c in result.content]
    values = [json.loads(t) for t in texts]
    return values[0] if len(values) == 1 else values


def mcp_stdio(root: Path, calls: list[tuple[str, dict]], *, env: dict | None = None):
    """Start the MCP server on `root` over stdio, initialise, and run `calls`."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(
            command=str(MCP_EXE), args=["--repo-root", str(root)], env=env, cwd=str(root)
        )
        async with stdio_client(params) as (read, write_):
            async with ClientSession(read, write_) as session:
                await session.initialize()
                out = []
                for name, args in calls:
                    out.append(_payload(await session.call_tool(name, args)))
                return out

    async def bounded():
        return await asyncio.wait_for(go(), timeout=120)

    return asyncio.run(bounded())


@pytest.fixture
def mcp_at(monkeypatch):
    """Point the in-process MCP tools at a repository root."""

    def point(root: Path):
        monkeypatch.setattr(mcp_server._State, "repo_root", Path(root))
        monkeypatch.setattr(mcp_server._State, "service", None)
        monkeypatch.setattr(mcp_server._State, "read_only", False)

    return point


def _norm(text: str) -> str:
    return text.replace("\\", "/").lower()


class Gui:
    def __init__(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        self.qtbot = qtbot
        self.monkeypatch = monkeypatch
        self.messages: list[tuple[str, str, str]] = []
        self.windows = []
        SB = QMessageBox.StandardButton

        def static(kind):
            def box(*a, **k):
                title = a[1] if len(a) > 1 else k.get("title", "")
                text = a[2] if len(a) > 2 else k.get("text", "")
                self.messages.append((kind, str(title), str(text)))
                buttons = a[3] if len(a) > 3 else k.get("buttons", SB.Ok)
                for want in (SB.Yes, SB.Ok, SB.Save):
                    if buttons & want:
                        return want
                return SB.Ok

            return staticmethod(box)

        for kind in ("critical", "warning", "information", "question"):
            monkeypatch.setattr(QMessageBox, kind, static(kind))

        def exec_(box):
            self.messages.append(("exec", box.windowTitle(), box.text()))
            for b in box.buttons():
                if box.buttonRole(b) in (
                    QMessageBox.ButtonRole.AcceptRole,
                    QMessageBox.ButtonRole.YesRole,
                    QMessageBox.ButtonRole.DestructiveRole,
                ):
                    b.click()
                    sb = box.standardButton(b)
                    return sb.value if hasattr(sb, "value") else int(sb)
            return 0

        monkeypatch.setattr(QMessageBox, "exec", exec_)

    def process(self):
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()

    def explorer(self, root: Path):
        from mellon.gui.project_explorer_window import ProjectExplorerWindow

        pe = ProjectExplorerWindow()
        self.windows.append(pe)
        pe.show()
        ok = pe.open_repo(Path(root))
        self.process()
        return pe, ok

    def tree_rows(self, pe) -> list[dict]:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTreeView

        rows = []
        for view in pe.findChildren(QTreeView):
            model = view.model()
            if model is None:
                continue

            def walk(parent):
                for r in range(model.rowCount(parent)):
                    idx = model.index(r, 0, parent)
                    rows.append({
                        "text": str(model.data(idx, Qt.ItemDataRole.DisplayRole) or ""),
                        "icon": model.data(idx, Qt.ItemDataRole.DecorationRole),
                        "tip": str(model.data(idx, Qt.ItemDataRole.ToolTipRole) or ""),
                    })
                    walk(idx)

            walk(view.rootIndex())
        return rows

    def editor(self, root: Path, doc_id: str):
        from mellon.gui.main_window import DocumentEditorWindow

        ed = DocumentEditorWindow(doc_id, DocumentService(root, cache=True))
        self.windows.append(ed)
        ed.show()
        self.process()
        return ed

    @staticmethod
    def action(win, name: str):
        from PySide6.QtGui import QAction

        def clean(t):
            return t.replace("&", "").replace("…", "").replace("...", "").strip().lower()

        found = [a for a in win.findChildren(QAction) if clean(a.text()) == name.lower()]
        assert found, f"no action {name!r}"
        return found[0]

    def trigger(self, win, name: str):
        self.action(win, name).trigger()
        self.process()

    def press_f5(self, win):
        from PySide6.QtGui import QAction

        found = [a for a in win.findChildren(QAction) if a.shortcut().toString() == "F5"]
        assert found, f"no F5 action in {type(win).__name__}"
        found[0].trigger()
        self.process()

    def read_only(self, ed) -> bool:
        return self.action(ed, "Read-only").isChecked()

    def set_read_only(self, ed, on: bool):
        if self.read_only(ed) != on:
            self.trigger(ed, "Read-only")
        assert self.read_only(ed) == on

    def grid(self, ed):
        from PySide6.QtWidgets import QTableView

        return ed.grid_pane.findChildren(QTableView)[0]

    def _row_col(self, ed, obj_id: str, header: str | None):
        from PySide6.QtCore import Qt

        view = self.grid(ed)
        m = view.model()
        heads = [str(m.headerData(c, Qt.Orientation.Horizontal) or "").strip().lower()
                 for c in range(m.columnCount())]
        id_col = heads.index("id")
        rows = [r for r in range(m.rowCount()) if m.data(m.index(r, id_col)) == obj_id]
        assert rows, f"{obj_id} not in grid"
        col = heads.index(header.lower()) if header else id_col
        return view, m.index(rows[0], col)

    def select(self, ed, obj_id: str):
        view, idx = self._row_col(ed, obj_id, None)
        view.setCurrentIndex(idx)
        view.selectRow(idx.row())
        self.process()

    def grid_edit(self, ed, obj_id: str, header: str, text: str) -> bool:
        from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QTextEdit, QWidget

        view, idx = self._row_col(ed, obj_id, header)
        view.setCurrentIndex(idx)
        view.edit(idx)
        self.process()
        editors = [w for w in view.viewport().findChildren(QWidget)
                   if isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit)) and w.isVisible()]
        if not editors:
            return False
        e = editors[0]
        if isinstance(e, QLineEdit):
            e.setText(text)
        else:
            e.setPlainText(text)
        ed.grid_pane.commit_open_editor()
        self.process()
        return True

    def inspector_description(self, ed, obj_id: str, text: str) -> bool:
        from PySide6.QtWidgets import QPlainTextEdit, QTextEdit

        self.select(ed, obj_id)
        edits = [w for w in ed.inspector.findChildren(QTextEdit) if w.isVisible()]
        edits += [w for w in ed.inspector.findChildren(QPlainTextEdit) if w.isVisible()]
        if not edits:
            return False
        edits[0].setFocus()
        edits[0].setPlainText(text)
        ed.inspector.commit_pending()
        self.process()
        return True

    def status(self, win) -> str:
        return win.statusBar().currentMessage()

    def stale_banner_shown(self, ed) -> bool:
        from mellon.gui.panels.grid_pane import StaleBanner

        return any(b.isVisibleTo(ed) for b in ed.findChildren(StaleBanner))

    def close_all(self):
        for w in reversed(self.windows):
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        self.process()


@pytest.fixture
def gui(qtbot, monkeypatch):
    h = Gui(qtbot, monkeypatch)
    yield h
    h.close_all()


CONFLICTED_DOC_1 = (
    DECL
    + '<object id="DOC-1">\n'
    + '  <attr name="type">Requirement</attr>\n'
    + "<<<<<<< HEAD\n"
    + "  <description>Ours</description>\n"
    + "=======\n"
    + "  <description>Theirs</description>\n"
    + ">>>>>>> other\n"
    + "</object>\n"
)


CONFLICT_LINE = 4  # the <<<<<<< line of CONFLICTED_DOC_1


@pytest.fixture
def clone_with_doc(tmp_path, git_env):
    root = git_init(git_env, tmp_path / "repo")
    make_doc(root)
    git_commit_all(git_env, root, "one")
    write(root / "DOC" / "objects" / "DOC-1.xml", object_xml("DOC-1", description="Changed"))
    git_commit_all(git_env, root, "two")
    return root


@pytest.fixture
def unopenable_repo(clone_with_doc, tmp_path, git_env):
    bad = tmp_path / "bad"
    git(git_env, tmp_path, "clone", "-q", str(clone_with_doc), str(bad))
    git(git_env, bad, "config", "core.repositoryformatversion", "99")
    return bad


@pytest.fixture
def code_only_repo(tmp_path, git_env):
    root = git_init(git_env, tmp_path / "code")
    write(root / "src" / "main.c", "int main(void) { return 0; }\n")
    write(root / "README.md", "# not a requirements repo\n")
    git_commit_all(git_env, root, "code only")
    return root


@pytest.fixture
def uncommitted_repo(tmp_path, git_env):
    root = git_init(git_env, tmp_path / "fresh")
    svc = DocumentService(root)
    svc.create_document("DOC", title="First document")
    svc.create_object("DOC", description="First object")
    assert git(git_env, root, "rev-list", "--all") == "", "precondition: no commit"
    return root


@pytest.fixture
def detached_repo(tmp_path, git_env):
    root = git_init(git_env, tmp_path / "detached")
    make_doc(root, metadata="<title>First title</title>")
    write(root / "DOC" / "objects" / "DOC-1.xml", object_xml("DOC-1", description="First text"))
    first = git_commit_all(git_env, root, "first")
    write(root / "DOC" / "DOC.xml",
          manifest_xml("DOC", ["DOC-1", "DOC-2"], metadata="<title>Second title</title>"))
    write(root / "DOC" / "objects" / "DOC-1.xml", object_xml("DOC-1", description="Second text"))
    git_commit_all(git_env, root, "second")
    git(git_env, root, "checkout", "-q", "--detach", first)
    assert git(git_env, root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "precondition"
    return root


def _break_doc_1(root: Path) -> bytes:
    f = root / "DOC" / "objects" / "DOC-1.xml"
    good = f.read_bytes()
    write(f, CONFLICTED_DOC_1)
    return good


def _shows_load_error(gui, ed) -> str:
    return " | ".join([t for _, _, t in gui.messages] + [gui.status(ed)])


def _svg_file(tmp_path: Path) -> Path:
    return write(
        tmp_path / "pic.svg",
        DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>\n',
    )


_ENUM_ATTRS = (
    '    <attribute name="type" kind="enum" required="true">\n'
    "      <option>Heading</option>\n      <option>Requirement</option>\n    </attribute>\n"
    '    <attribute name="status" kind="enum">\n'
    "      <option>Draft</option>\n      <option>In review</option>\n    </attribute>\n"
)


class _HalfWriter:
    """A file object that writes half of what it is given, then fails as a full
    disk does."""

    def __init__(self, fh):
        self._fh = fh

    def write(self, data):
        self._fh.write(data[: max(1, len(data) // 2)])
        self._fh.flush()
        raise OSError(errno.ENOSPC, "No space left on device (injected)")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, name):
        return getattr(self._fh, name)


class PartialWrites:
    """Make the n-th file opened for writing under `folder` fail partway."""

    def __init__(self, monkeypatch, folder: Path, nth: int = 1):
        self.folder = Path(folder).resolve()
        self.nth = nth
        self.armed = True
        self.count = 0
        self.fired: list[str] = []
        self.content_before: bytes | None = None
        real_open = io.open

        def fake_open(file, mode="r", *a, **k):
            target = None
            if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wax+"):
                p = Path(file).resolve()
                if self.folder in p.parents:
                    target = p
            if target is not None and self.armed:
                self.count += 1
                if self.count == self.nth:
                    self.fired.append(str(target))
                    self.content_before = target.read_bytes() if target.exists() else None
                    return _HalfWriter(real_open(file, mode, *a, **k))
            return real_open(file, mode, *a, **k)

        monkeypatch.setattr(io, "open", fake_open)
        monkeypatch.setattr(builtins, "open", fake_open)


_BAD_SLUGS = ["Main", "main-", "-main", "main--view", "", "main/view", "main\\view"]


@pytest.fixture
def slug_doc(tmp_path):
    folder = make_doc(tmp_path / "r")
    assert editor.create_view(folder, "main", ["status"]) == "main"
    assert editor.create_filter(folder, "main", rules=[FilterRule(kind="attr", attr="status", op="eq", value="Draft")]) == "main"
    return folder


@pytest.fixture
def read_only_repo(tmp_path, git_env):
    root = git_init(git_env, tmp_path / "ro")
    make_doc(root, "PARENT", n=2)
    write(root / "PARENT" / "figures" / "PARENT-1.svg", _svg_file(tmp_path).read_bytes())
    make_doc(root, "CHILD", n=2, parents=("PARENT",))
    write(root / "CHILD" / "links" / "CHILD-1.xml", links_xml("CHILD-1", [("PARENT", "PARENT-1", "satisfies")]))
    first = git_commit_all(git_env, root, "first")
    write(root / "PARENT" / "objects" / "PARENT-1.xml", object_xml("PARENT-1", description="Revised"))
    git_commit_all(git_env, root, "second")
    return root, first


def _write_text(folder: Path, where: str, text: str):
    if where == "description":
        editor.set_description(folder, "DOC-1", text)
    elif where == "attribute":
        editor.set_attribute(folder, "DOC-1", "rationale", text)
    elif where == "cell":
        editor.set_table(folder, "DOC-1", Table(columns=(TableColumn(),),
                                                rows=(TableRow(cells=(TableCell(text=text),)),)))
    elif where == "title":
        editor.set_metadata(folder, "title", text)
    else:  # pragma: no cover
        raise AssertionError(where)


def _read_text(folder: Path, where: str) -> str:
    doc = load_document(folder)
    obj = doc.objects["DOC-1"]
    return {
        "description": lambda: obj.description,
        "attribute": lambda: obj.attrs["rationale"],
        "cell": lambda: obj.table.rows[0].cells[0].text,
        "title": lambda: doc.metadata.title,
    }[where]()
